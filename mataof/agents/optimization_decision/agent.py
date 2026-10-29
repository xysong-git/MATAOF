"""Optimization Decision Agent（优化决策智能体）。

核心职责：
    根据 Query Analysis Agent 提供的查询特征、当前数据库状态、系统状态以及
    Knowledge Memory Agent 提供的历史执行经验，在预定义的候选策略空间中选择
    当前查询更合适的优化策略。负责"策略选择"，不负责查询解析和实际执行。

严格界限：
    - 有界策略空间：只从"输入候选策略 ∩ 策略目录（catalog.py）"中选择，
      禁止自行创造候选策略；目录之外的策略名一律拒绝；
    - 上下文感知：决策必须综合 Query Features + Database State + System State +
      Historical Knowledge；仅凭 SQL 文本（即缺少 query_analysis）视为输入无效；
    - 历史知识是"证据"而非"规则"：只有相似度达标（similarity.py）的记录参与决策，
      并按相似度/时效加权；
    - 风险控制：证据不足时优先安全 baseline/fallback；高风险策略需要更强证据；
    - 不声称全局最优：输出的是"当前信息条件下选择的策略"；
    - 确定性纯函数：相同输入 → 相同输出（便于实验与错误分析）。

输入：decision_input（见 context.py 的契约说明）。
输出：始终为 JSON 可序列化字典（schema 见 schemas.optimization_decision_output_template）。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from mataof.runtime import (
    Abort,
    Act,
    AgentContext,
    AgentRuntime,
    Finish,
    Goal,
    ToolRegistry,
)
from mataof.schemas import optimization_decision_output_template
from mataof.agents.optimization_decision.catalog import (
    DIMENSIONS,
    NOT_APPLICABLE,
    STRATEGY_CATALOG,
)
from mataof.agents.optimization_decision.context import DecisionContext, normalize_input
from mataof.agents.optimization_decision.similarity import (
    SIMILARITY_THRESHOLD,
    build_history_evidence,
    same_shape_evidence,
)
from mataof.agents.optimization_decision.scoring import (
    DimensionResult,
    evaluate_dimension,
)
from mataof.agents.optimization_decision.confidence import (
    dimension_confidence,
    overall_confidence,
)
from mataof.agents.optimization_decision.llm_enhance import (
    build_llm_analysis,
    request_llm_decision,
    request_preferences,
)
from .plans.cost import BASELINE_PLAN_ID, select_plan
from .plans.integration import (
    dimension_overrides,
    plan_decision_entry,
    plan_dimensions,
    plan_equivalent_sql,
    plan_summary,
)
from .provider import build_plans, plan_generation_notes


# ---------------------------------------------------------------------------
# Agent 闭环配置（默认保守：最多 2 次尝试 = 初始 + 1 次重规划）
# ---------------------------------------------------------------------------

_AGENT_DEFAULTS = {
    "max_attempts": 2,
    "deadline_ms": 30_000.0,
    "replan_on": ("degraded", "failed"),
}


def _normalize_agent_config(raw: Any) -> dict:
    """归一化 Agent 运行配置；非法值回退默认（不影响确定性决策）。"""
    cfg = dict(_AGENT_DEFAULTS)
    if not isinstance(raw, dict):
        return cfg
    if raw.get("max_attempts") is not None:
        try:
            cfg["max_attempts"] = max(1, min(4, int(raw["max_attempts"])))
        except (TypeError, ValueError):
            pass
    if raw.get("deadline_ms") is not None:
        try:
            cfg["deadline_ms"] = max(1.0, float(raw["deadline_ms"]))
        except (TypeError, ValueError):
            pass
    if isinstance(raw.get("replan_on"), (list, tuple, set)):
        values = tuple(str(x) for x in raw["replan_on"] if x is not None)
        if values:
            cfg["replan_on"] = values
    return cfg


def _plan_id(decision: Any) -> Optional[str]:
    """从决策输出中取最终执行计划 id（无计划 → None）。"""
    if not isinstance(decision, dict):
        return None
    ps = decision.get("plan_selection")
    if isinstance(ps, dict) and ps.get("chosen_plan_id"):
        return str(ps["chosen_plan_id"])
    entry = (decision.get("decision") or {}).get("plan")
    if isinstance(entry, dict) and entry.get("strategy"):
        return str(entry["strategy"])
    return None


def _public_attempt(attempt: dict) -> dict:
    """对外暴露的尝试记录（去掉下划线私有字段）。"""
    return {k: v for k, v in attempt.items() if not k.startswith("_")}


class OptimizationDecisionAgent:
    """优化决策智能体。

    用法：
        agent = OptimizationDecisionAgent()
        result = agent.decide({
            "query_id": "q1",
            "query": "SELECT ...",         # 可选：候选策略/计划由 Agent 生成
            "query_analysis": <Query Analysis Agent 的输出>,
            "database_state": {...},      # 可选
            "system_state": {...},        # 可选
            "historical_records": [...],  # 可选
            "device_inventory": [...],    # 可选：通配设备展开清单
            "planning": {"enabled": True},# 可选：计划生成配置（默认开启）
            "candidate_strategies": {...},# 可选（非空 → 覆盖内置候选生成）
            "baseline_strategy": {...},   # 可选（缺失 → 内置安全默认）
        })
    """

    name = "optimization_decision"
    description = "综合查询特征、数据库状态、系统状态与历史经验，在候选策略空间中"
    description += "选择当前查询的优化策略，并给出可追踪的依据与置信度。"

    def _prepare_plan_candidates(self, decision_input: dict) -> dict:
        """生成可执行候选计划（决策 Agent 的内置能力，不是外挂计划层）。

        有原始 SQL + 有效 query_analysis 时，Agent 自己把查询编译为候选计划；
        外部显式提供 candidate_plans（含空列表）则视为覆盖，不再生成。
        planning.enabled=false 可关闭自动生成（用于消融/对照），显式注入的计划
        仍然有效。
        """
        if not isinstance(decision_input, dict):
            return decision_input
        prepared = dict(decision_input)
        if prepared.get("candidate_plans") is not None:
            return prepared
        planning = prepared.get("planning")
        if not isinstance(planning, dict):
            planning = {}
        if planning.get("enabled") is False or prepared.get("planning_enabled") is False:
            return prepared
        query = prepared.get("query")
        analysis = prepared.get("query_analysis")
        if not (isinstance(query, str) and query.strip()
                and isinstance(analysis, dict) and analysis.get("query_features")):
            return prepared
        inventory = planning.get("device_inventory",
                                 prepared.get("device_inventory"))
        raw_parallelism = planning.get("max_parallelism",
                                       prepared.get("plan_max_parallelism", 8))
        try:
            max_parallelism = max(1, int(raw_parallelism))
        except (TypeError, ValueError):
            max_parallelism = 8
        try:
            prepared["candidate_plans"] = build_plans(
                query, analysis, inventory, max_parallelism)
            prepared["plan_generation_notes"] = plan_generation_notes(
                query, analysis, inventory)
        except Exception as exc:  # 生成失败绝不拖垮决策；退回维度策略路径
            prepared["candidate_plans"] = []
            prepared["plan_generation_notes"] = [
                f"计划生成失败：{type(exc).__name__}；本轮使用维度策略路径"
            ]
        return prepared

    def decide(self, decision_input: dict, llm: Optional[object] = None,
               llm_decide: bool = False) -> dict:
        """决策入口。llm：可选 LLMClient（混合增强模式）。

        - llm=None / 调用失败 → 确定性决策完全不变（兜底）；
        - 开启 decision_input["llm_preference_bonus"] 时，LLM 第一偏好 +0.05 分
          （实验性，不参与风险门控）；
        - llm_decide=True（decision_mode="llm_primary"）：LLM 为每个适用维度
          从候选中选择策略，**只做合法性校验**（候选 ∩ 目录、维度适用、需求
          满足；equivalent_sql 一律取自候选生成器，LLM 不生成 SQL）；
          LLM 失败/非法 → 该维度保持确定性决策（兜底）。evidence/置信度
          始终来自确定性链路，LLM 不给证据（防编造）。
        """
        output = optimization_decision_output_template()

        # 计划候选生成是决策 Agent 的固有步骤：有 query 即编译计划，
        # 外部 candidate_plans 仅作为覆盖/测试注入。
        decision_input = self._prepare_plan_candidates(decision_input)
        ctx = normalize_input(decision_input)
        output["query_id"] = ctx.query_id
        if ctx.errors:
            output["decision_status"] = "invalid_input"
            output["fallback_strategy"] = ""
            return output

        # ---- LLM 候选偏好（仅 bonus 开启时参与评分；失败 → 空）----
        llm_preferences: dict = {}
        if llm is not None and decision_input.get("llm_preference_bonus"):
            llm_preferences = request_preferences(llm, decision_input, ctx.candidates,
                                                  ctx.notes)

        # ---- 历史证据（相似度过滤 + 加权）----
        ev = build_history_evidence(
            ctx.historical_records, ctx.compact_features,
            ctx.database_state, ctx.system_state, ctx.reference_time,
        )
        # 计划成本预测优先使用"同查询形状 + 同数据规模"的实测证据：
        # 近邻形状（q3/q10 测点不同、q7/q11 窗口不同）的绝对延迟不能
        # 混入当前形状的成本预测；样本不足（<3 次）→ 退回全部相似证据。
        plan_ev = same_shape_evidence(
            ev, ctx.compact_features, ctx.database_state) or ev

        # ---- 各维度评估与选择（含策略评价表先验，D1）----
        evaluations = decision_input.get("strategy_evaluations")
        prior_strength = float(decision_input.get("eval_prior_strength")
                               or 0.10)
        min_count = int(decision_input.get("eval_min_count") or 3)
        results: dict[str, DimensionResult] = {}
        for dim in DIMENSIONS:
            results[dim] = evaluate_dimension(
                dim, ctx, ev, llm_preferences,
                evaluations=evaluations if isinstance(evaluations, dict) else None,
                eval_prior_strength=prior_strength, eval_min_count=min_count,
            )

        # ---- 维度置信度 ----
        dim_conf = {dim: dimension_confidence(results[dim], ctx, ev) for dim in DIMENSIONS}

        # ---- 组装 decision 与 reason ----
        for dim in DIMENSIONS:
            r = results[dim]
            if not r.applicable:
                output["decision"][dim] = {
                    "strategy": NOT_APPLICABLE,
                    "reason": r.not_applicable_reason or "维度不适用",
                    "confidence": 0.0,
                    "equivalent_sql": None,
                }
            elif r.is_fallback:
                output["decision"][dim] = {
                    "strategy": r.fallback_strategy or "",
                    "reason": _fallback_reason(dim, r, ctx),
                    "confidence": dim_conf[dim],
                    "equivalent_sql": None,
                }
            else:
                chosen = r.chosen
                output["decision"][dim] = {
                    "strategy": chosen.name,
                    "reason": _selection_reason(dim, r, ctx),
                    "confidence": dim_conf[dim],
                    "equivalent_sql": chosen.equivalent_sql,
                }

        # ---- LLM 参与决策（llm_decide：LLM 选择 + 合法性校验；确定性为兜底）----
        llm_chosen: dict = {}
        if llm_decide and llm is not None:
            applicable = {dim: results[dim].applicable for dim in DIMENSIONS}
            req_met = {
                dim: {e.name: e.requirements_met for e in results[dim].evaluations}
                for dim in DIMENSIONS
            }
            llm_res = request_llm_decision(llm, decision_input, ctx.candidates,
                                           applicable, req_met, ctx.notes)
            for dim, name in llm_res.get("decisions", {}).items():
                cand = next((c for c in ctx.candidates.get(dim, [])
                             if (c["name"] if isinstance(c, dict) else c) == name),
                            None)
                if cand is None:
                    continue   # 候选条目缺失（理论不可达：校验已保证在候选内）
                if isinstance(cand, dict):
                    llm_chosen[dim] = {
                        "name": name,
                        "reason": (llm_res.get("reasons") or {}).get(dim),
                        "equivalent_sql": cand.get("equivalent_sql"),
                        "parameters": cand.get("parameters"),
                    }
                else:
                    llm_chosen[dim] = {"name": name, "reason": None,
                                       "equivalent_sql": None, "parameters": None}
            if llm_chosen:
                ctx.notes.append("llm_selected: " + ",".join(sorted(llm_chosen)))

        # ---- 决策条目：LLM 覆盖（理由标注 llm_selected，置信度保持确定性值）----
        for dim, ov in llm_chosen.items():
            reason = (ov.get("reason") or "").strip() or "LLM 依据当前上下文选择该策略"
            output["decision"][dim] = {
                "strategy": ov["name"],
                "reason": f"{reason}（llm_selected；当前信息条件下的选择）",
                "confidence": output["decision"][dim]["confidence"],
                "equivalent_sql": ov.get("equivalent_sql"),
            }

        # ---- 执行计划选择：Agent 自己生成的可执行计划中做成本/收益选择 ----
        # 计划是权威执行动作：维度决策被显式对齐/批次取值覆盖，保证
        # strategy_id/决策条目与真正送到执行器的计划一致。
        plan_choice = None
        plan_entry = None
        plan_overrides: dict[str, dict] = {}
        if ctx.candidate_plans:
            planning = decision_input.get("planning")
            if not isinstance(planning, dict):
                planning = {}
            plan_kwargs = {"eval_min_count": min_count}
            v = decision_input.get("plan_min_saving_ms")
            if v is None:
                v = planning.get("min_saving_ms")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                plan_kwargs["min_saving_ms"] = float(v)
            v = decision_input.get("plan_min_saving_ratio")
            if v is None:
                v = planning.get("min_saving_ratio")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                plan_kwargs["min_saving_ratio"] = float(v)
            excluded_ids = decision_input.get("excluded_plan_ids")
            if isinstance(excluded_ids, (list, tuple, set)):
                excluded_ids = [str(x) for x in excluded_ids
                                if isinstance(x, (str, int))]
            else:
                excluded_ids = None
            plan_choice = select_plan(
                ctx.candidate_plans, ctx, plan_ev,
                evaluations=evaluations if isinstance(evaluations, dict) else None,
                excluded_plan_ids=excluded_ids,
                **plan_kwargs,
            )
            if plan_ev is not ev:
                plan_choice.notes.append(
                    "成本预测使用同查询形状且同数据规模的实测证据"
                    f"（{plan_ev.total_count('plan')} 次；近邻形状不计入）"
                )
            plan_entry = plan_decision_entry(plan_choice, ctx)
            if plan_entry is not None:
                output["decision"]["plan"] = plan_entry
                output["selected_strategy"]["execution_plan"] = plan_choice.chosen
                output["plan_selection"] = plan_choice.to_dict()
                pconf = plan_entry["confidence"]
                plan_overrides = dimension_overrides(
                    plan_choice.chosen, results["alignment"].applicable, pconf)
                for dim, entry in plan_overrides.items():
                    if dim in llm_chosen and llm_chosen[dim]["name"] != entry["strategy"]:
                        ctx.notes.append(
                            f"计划覆盖 LLM 维度选择：{dim}={llm_chosen[dim]['name']}"
                            f" → {entry['strategy']}（以执行计划为准）"
                        )
                    output["decision"][dim] = dict(entry)

        # ---- selected_strategy 与 fallback_strategy ----
        parts, params = [], {}
        fallback_parts = []
        for dim in DIMENSIONS:
            r = results[dim]
            if dim in plan_overrides:
                parts.append(f"{dim}={plan_overrides[dim]['strategy']}")
            elif dim in llm_chosen:
                parts.append(f"{dim}={llm_chosen[dim]['name']}")
            elif not r.applicable:
                parts.append(f"{dim}={NOT_APPLICABLE}")
            elif r.is_fallback:
                parts.append(f"{dim}={r.fallback_strategy}")
                fallback_parts.append(f"{dim}={r.fallback_strategy}")
            else:
                parts.append(f"{dim}={r.chosen.name}")
        params = _strategy_parameters(results, ctx, llm_chosen)
        if plan_choice is not None and plan_choice.chosen is not None:
            # 计划权威：移除被覆盖维度的旧参数，写入计划实际执行参数
            for dim in plan_overrides:
                params.pop(dim, None)
            dims = plan_dimensions(plan_choice.chosen)
            params["fetch_size"] = {"fetch_size": dims["fetch_size_value"]}
            params["plan"] = plan_summary(plan_choice.chosen)
            parts.append(f"plan={plan_choice.chosen.get('plan_id')}")
        output["selected_strategy"]["strategy_id"] = "|".join(parts)
        output["selected_strategy"]["strategy_parameters"] = params
        if plan_choice is not None and plan_choice.chosen is not None:
            output["selected_strategy"]["equivalent_sql"] = plan_equivalent_sql(
                plan_choice.chosen)
            output["fallback_strategy"] = ""
        else:
            output["selected_strategy"]["equivalent_sql"] = _compose_equivalent_sql(
                ctx, results, llm_chosen)
            output["fallback_strategy"] = "|".join(fallback_parts)

        # ---- evidence ----
        output["evidence"] = _collect_evidence(ctx, ev, results)

        # ---- 整体置信度与状态 ----
        if plan_entry is not None:
            # 计划决定实际执行动作：置信度取计划证据强度
            output["overall_confidence"] = plan_entry["confidence"]
            output["decision_status"] = "success"
        else:
            output["overall_confidence"] = overall_confidence(
                results, ctx, dim_conf, len(ev.used)
            )
            n_fallback = n_applicable = 0
            for dim in DIMENSIONS:
                r = results[dim]
                if dim in llm_chosen:
                    n_applicable += 1        # LLM 选择的维度视为已决策（不属回退）
                elif r.applicable:
                    n_applicable += 1
                    if r.is_fallback:
                        n_fallback += 1
            if n_fallback == 0:
                output["decision_status"] = "success"
            elif n_fallback >= n_applicable:
                output["decision_status"] = "fallback"
            else:
                output["decision_status"] = "partial_fallback"

        # ---- 可追踪说明（扩展字段）----
        notes = list(ctx.notes) + list(ctx.plan_notes)
        for dim in DIMENSIONS:
            notes.extend(results[dim].exclusion_notes)
        if plan_choice is not None:
            notes.extend(f"计划选择：{n}" for n in plan_choice.notes)
        if ev.invalid:
            notes.append(f"{ev.invalid} 条历史记录结构不合法，未参与决策")
        if ev.below_threshold:
            notes.append(
                f"{ev.below_threshold} 条历史记录与当前上下文相似度低于阈值 "
                f"（{SIMILARITY_THRESHOLD}），未参与决策"
            )
        output["notes"] = notes

        # ---- LLM 语义增强（只进 llm_analysis 节；失败不影响确定性决策）----
        # llm_decide 模式下跳过解读调用（LLM 仅用于决策本身，每条查询 1 次调用）
        output["llm_analysis"] = build_llm_analysis(
            None if llm_decide else llm, decision_input, output, ctx.candidates)
        return output

    # ------------------------------------------------------------------
    # Agent 闭环：决策 → 执行 → 观察 →（退化时）重规划 → 终止
    # ------------------------------------------------------------------
    def run(self, decision_input: dict, *,
            execute: Optional[Callable[[dict], dict]] = None,
            agent_config: Optional[dict] = None,
            llm: Optional[object] = None,
            llm_decide: bool = False) -> dict:
        """在真实执行反馈上闭合决策环（与 `decide` 的纯函数路径并存）。

        execute：由调用方注入的执行工具，签名 `execute(decision) -> dict`，
        必须返回：
            {"execution": {...}, "monitoring": {...},
             "monitoring_reference": {...}|None, "monitoring_input": {...},
             "execution_ms": float, "monitoring_ms": float}
        典型实现是 runner 的"执行 + 监控"组合（执行器 + 监控 Agent）。

        闭环语义（有界）：
        1. 确定性/LLM 决策选出一个可执行计划；
        2. 通过 execute 工具执行并观察监控反馈；
        3. 若实测 degraded/failed 且仍有尝试预算 → 排除该计划后重规划；
        4. 预算耗尽 / 无更优替代 / baseline 失败 → 终止并返回最后一次结果。

        与 `decide` 的差异仅在于"是否行动并观察"：决策规则、候选生成、
        证据链与确定性兜底完全复用，不引入新的策略来源。
        """
        cfg = _normalize_agent_config(agent_config)
        state = {
            "input": decision_input if isinstance(decision_input, dict) else {},
            "decision": None,
            "pending": None,
            "attempts": [],
            "excluded": [],
            "timings": {"decision_ms": 0.0, "execution_ms": 0.0,
                        "monitoring_ms": 0.0},
            "final_monitoring_ms": 0.0,
            "cfg": cfg,
            "llm": llm,
            "llm_decide": bool(llm_decide),
        }

        tools = ToolRegistry()
        if execute is not None:
            tools.register(
                "execute_decision", execute,
                description="执行当前决策选中的计划，返回执行事实、监控反馈与参照",
                parameters={"required": ["decision"],
                            "properties": {"decision": {"type": "object"}}},
                side_effect="execute", cost="high",
            )

        runtime = AgentRuntime(self.name, tools, self._agent_policy)
        # 每次尝试最多消耗 2 步（执行 + 观察决策）；+2 用于首次决策与收尾。
        goal = Goal(name="optimize_query",
                    max_steps=2 * int(cfg["max_attempts"]) + 2,
                    deadline_ms=cfg["deadline_ms"])
        run_result = runtime.run(goal, AgentContext(data=state))

        payload = run_result.result
        if not isinstance(payload, dict):
            payload = self._agent_payload(state)
        meta = payload.setdefault("agent", {})
        meta.update({
            "steps": run_result.steps,
            "terminated_by": run_result.terminated_by,
            "elapsed_ms": round(run_result.elapsed_ms, 3),
            "trace": run_result.trace,
        })
        return payload

    def _agent_policy(self, ctx: AgentContext, tools: ToolRegistry):
        """OD 的策略函数：根据上一步观察决定执行、重规划或终止。"""
        state = ctx.data
        cfg = state["cfg"]
        obs = ctx.last_observation

        # ---- 第一步：先做决策（不执行任何外部动作）----
        if obs is None:
            decision, ms = self._timed_decide(
                state["input"], state["llm"], state["llm_decide"])
            state["timings"]["decision_ms"] += ms
            state["decision"] = decision
            if "execute_decision" not in tools:
                return Abort(result=self._agent_payload(state),
                             reason="no_executor")
            state["pending"] = decision
            return Act("execute_decision", {"decision": decision},
                       reason="执行当前决策选中的计划")

        # ---- 观察上一轮执行结果 ----
        decision = state.get("pending") or state.get("decision")
        if decision is None:
            return Abort(result=self._agent_payload(state),
                         reason="no_decision")
        if obs.ok:
            value = obs.value if isinstance(obs.value, dict) else {}
            attempt = self._attempt_from(decision, value,
                                         len(state["attempts"]) + 1)
            state["timings"]["execution_ms"] += \
                float(value.get("execution_ms") or 0.0)
            mon_ms = float(value.get("monitoring_ms") or 0.0)
            state["timings"]["monitoring_ms"] += mon_ms
            state["final_monitoring_ms"] = mon_ms
        else:
            attempt = self._failed_attempt(decision, obs.error,
                                           len(state["attempts"]) + 1)
            state["final_monitoring_ms"] = 0.0
        state["attempts"].append(attempt)

        return self._decide_next(state, cfg, attempt)

    def _decide_next(self, state: dict, cfg: dict, attempt: dict):
        """退化为失败/退化且仍有预算 → 排除该计划重规划；否则终止。"""
        assessment = attempt.get("performance_assessment")
        plan_id = attempt.get("plan_id")
        attempts = state["attempts"]

        if assessment not in cfg["replan_on"]:
            return Finish(result=self._agent_payload(state),
                          reason=f"accepted:{assessment or 'unknown'}")
        if len(attempts) >= cfg["max_attempts"]:
            return Finish(result=self._agent_payload(state),
                          reason="max_attempts")
        # 无计划可排除（如 planning 关闭/输入无效）：重规划没有可作用的计划 id。
        if not plan_id:
            return Finish(result=self._agent_payload(state),
                          reason="no_plan")
        # baseline 是安全动作：它失败说明问题不在策略选择，重试其他计划
        # 只会增加不确定性，因此不再重规划。
        if plan_id == BASELINE_PLAN_ID:
            return Finish(result=self._agent_payload(state),
                          reason="baseline_not_retried")

        excluded = list(dict.fromkeys([*state["excluded"], plan_id]))
        replan_input = dict(state["input"])
        replan_input["excluded_plan_ids"] = excluded
        # 重规划走确定性路径：LLM 已参与过首次决策，重试不再消耗额外调用。
        decision, ms = self._timed_decide(replan_input, None, False)
        state["timings"]["decision_ms"] += ms
        new_plan_id = _plan_id(decision)
        if (new_plan_id is None or new_plan_id == plan_id
                or new_plan_id in excluded):
            return Finish(result=self._agent_payload(state),
                          reason="no_alternative")

        attempt["replan"] = True
        state["decision"] = decision
        state["excluded"] = excluded
        state["pending"] = decision
        return Act("execute_decision", {"decision": decision},
                   reason=(f"实测 {assessment}（{plan_id}）→ 排除后重规划为 "
                           f"{new_plan_id}"))

    def _timed_decide(self, decision_input: dict, llm: Optional[object],
                      llm_decide: bool) -> tuple[dict, float]:
        t0 = time.perf_counter()
        output = self.decide(decision_input, llm=llm, llm_decide=llm_decide)
        return output, (time.perf_counter() - t0) * 1000.0

    @staticmethod
    def _attempt_from(decision: dict, value: dict, index: int) -> dict:
        execution = value.get("execution") or {}
        monitoring = value.get("monitoring") or {}
        metrics = execution.get("metrics") or {}
        return {
            "attempt": index,
            "plan_id": _plan_id(decision),
            "strategy_id": (decision.get("selected_strategy") or {}).get("strategy_id"),
            "decision_status": decision.get("decision_status"),
            "confidence": decision.get("overall_confidence"),
            "execution_status": execution.get("execution_status"),
            "response_time_ms": metrics.get("response_time_ms"),
            "performance_assessment": monitoring.get("performance_assessment"),
            "replan": False,
            "_execution": execution,
            "_monitoring": monitoring,
            "_reference": value.get("monitoring_reference"),
            "_monitoring_input": value.get("monitoring_input"),
        }

    @staticmethod
    def _failed_attempt(decision: dict, error: Optional[str], index: int) -> dict:
        return {
            "attempt": index,
            "plan_id": _plan_id(decision),
            "strategy_id": (decision.get("selected_strategy") or {}).get("strategy_id"),
            "decision_status": decision.get("decision_status"),
            "confidence": decision.get("overall_confidence"),
            "execution_status": "failed",
            "response_time_ms": None,
            "performance_assessment": "failed",
            "error": error,
            "replan": False,
            "_execution": None,
            "_monitoring": None,
            "_reference": None,
            "_monitoring_input": None,
        }

    @staticmethod
    def _agent_payload(state: dict) -> dict:
        attempts = state.get("attempts") or []
        final = attempts[-1] if attempts else {}
        cfg = state.get("cfg") or _AGENT_DEFAULTS
        return {
            "decision": state.get("decision")
            or optimization_decision_output_template(),
            "execution": final.get("_execution"),
            "monitoring": final.get("_monitoring"),
            "monitoring_reference": final.get("_reference"),
            "monitoring_input": final.get("_monitoring_input"),
            "agent": {
                "enabled": True,
                "runtime": "mataof.runtime",
                "policy": "deterministic_replan",
                "max_attempts": int(cfg.get("max_attempts") or 2),
                "replan_on": list(cfg.get("replan_on") or ()),
                "attempts": [_public_attempt(a) for a in attempts],
                "timings": {k: round(float(v), 3)
                            for k, v in (state.get("timings") or {}).items()},
                "final_monitoring_ms": round(
                    float(state.get("final_monitoring_ms") or 0.0), 3),
            },
        }


# ---------------------------------------------------------------------------
# reason 生成（可追踪：引用具体证据，不做全局最优声明）
# ---------------------------------------------------------------------------

def _fallback_reason(dim: str, r: DimensionResult, ctx: DecisionContext) -> str:
    reasons = "；".join(e.excluded_reason for e in r.evaluations if e.excluded_reason)
    return (f"候选策略均不满足安全选择条件（{reasons or '无可用候选'}），"
            f"证据不足，回退安全 baseline {r.fallback_strategy}。")


def _selection_reason(dim: str, r: DimensionResult, ctx: DecisionContext) -> str:
    chosen = r.chosen
    parts: list[str] = []
    if dim == "alignment":
        dev = (ctx.features.get("device") or {}).get("device_count")
        if dev is not None:
            parts.append(f"设备数 {dev}")
    elif dim == "fetch_size":
        pass   # 上下文加分标签已说明依据

    for label in chosen.factor_labels:
        parts.append(label)

    excluded = [e.name for e in r.evaluations if e.excluded_reason]
    if excluded:
        parts.append("排除：" + "、".join(excluded))

    parts.append(f"在当前信息条件下选择 {chosen.name}（score {chosen.score}）。")
    return "；".join(parts)


def _strategy_parameters(results: dict, ctx: DecisionContext,
                         llm_chosen: Optional[dict] = None) -> dict:
    """传递给执行层的参数：只包含事实性取值 + 候选提供的执行级参数。

    llm_chosen：LLM 选择的维度覆盖（{dim: {name, parameters, ...}}）。
    """
    llm_chosen = llm_chosen or {}
    params: dict = {}
    # 候选携带的执行级参数（候选生成器给出，原样透传——如 fetch_size）
    for dim in DIMENSIONS:
        r = results[dim]
        ov = llm_chosen.get(dim)
        if ov is not None:
            if ov.get("parameters"):
                entry = dict(params.get(dim) or {})
                entry.update(ov["parameters"])
                params[dim] = entry
        elif r.applicable and not r.is_fallback and r.chosen.parameters:
            entry = dict(params.get(dim) or {})
            entry.update(r.chosen.parameters)
            params[dim] = entry
    return params


def _compose_equivalent_sql(ctx: DecisionContext, results: dict,
                            llm_chosen: Optional[dict] = None) -> Optional[str]:
    """组合可直接执行的等价 SQL（混合模型）。

    组合规则（保守、确定性）：
    - 0 个维度提供非平凡等价 SQL → 原查询文本（策略为执行级参数，SQL 不变）；
    - 恰好 1 个维度提供 → 该 SQL（候选按维度独立生成，典型为 Filter Order 重排）；
    - ≥2 个维度提供 → null（无法可靠组合独立生成的 SQL），执行层按 strategy_id
      与各维度参数执行，绝不输出可能错误的 SQL。
    llm_chosen：LLM 选择的维度（SQL 同样取自候选生成器字段）。
    """
    llm_chosen = llm_chosen or {}
    sqls = []
    for dim in DIMENSIONS:
        ov = llm_chosen.get(dim)
        if ov is not None:
            if ov.get("equivalent_sql"):
                sqls.append((dim, ov["equivalent_sql"]))
            continue
        r = results[dim]
        if r.applicable and not r.is_fallback and r.chosen.equivalent_sql:
            sqls.append((dim, r.chosen.equivalent_sql))
    if len(sqls) == 1:
        return sqls[0][1]
    if len(sqls) > 1:
        return None
    original = (ctx.analysis or {}).get("query") if ctx.analysis else None
    return original if isinstance(original, str) and original.strip() else None


# ---------------------------------------------------------------------------
# evidence 收集（只列出实际用于决策的条目）
# ---------------------------------------------------------------------------

def _collect_evidence(ctx: DecisionContext, ev, results: dict) -> dict:
    feat_items: list[str] = []
    t = ctx.features.get("time") or {}
    f = ctx.features.get("filter") or {}
    agg = ctx.features.get("aggregation") or {}
    scan = ctx.features.get("scan") or {}

    if ctx.has_time_filter:
        feat_items.append("time.has_time_filter=true")
        if t.get("time_span") is not None:
            feat_items.append(f"time.time_span={t['time_span']}ms")
        feat_items.append(f"time.range_level={t.get('range_level')}")
    if ctx.time_window_group_by:
        feat_items.append("aggregation.group_by_details.type=time_window")
    if ctx.non_time_filter_count:
        feat_items.append(f"filter.non_time_filter_count={ctx.non_time_filter_count}")
    if agg.get("has_aggregation"):
        feat_items.append("aggregation.has_aggregation=true")
        if agg.get("has_window"):
            feat_items.append("aggregation.has_window=true")
        if agg.get("has_group_by"):
            feat_items.append("aggregation.has_group_by=true")
    if scan.get("estimated_scan_range") is not None:
        feat_items.append(f"scan.estimated_scan_range={scan['estimated_scan_range']}")
    if not feat_items:
        feat_items.append("（无可用于决策的已确认特征）")

    db_items: list[str] = []
    if ctx.database_state:
        for k in sorted(ctx.database_state.keys()):
            db_items.append(f"database_state.{k}={ctx.database_state[k]}")
    else:
        db_items.append("database_state 未提供（视为 unknown）")

    sys_items: list[str] = []
    if ctx.system_state:
        sys_items.append(f"load_level={ctx.load_level}（由 system_state 推导）")
        for k in sorted(ctx.system_state.keys()):
            sys_items.append(f"system_state.{k}={ctx.system_state[k]}")
    else:
        sys_items.append("system_state 未提供（视为 unknown）")

    hist_items: list[dict] = []
    used_dims = {u["record_id"]: [] for u in ev.used}
    for u in ev.used:
        rec = u["record"]
        for dim in ("alignment", "fetch_size", "plan"):
            if rec["strategy"].get(dim) is not None:
                used_dims[u["record_id"]].append(dim)
    for u in ev.used:
        hist_items.append({
            "record_id": u["record_id"],
            "similarity": u["similarity"],
            "used_for": used_dims[u["record_id"]],
        })
    hist_items.append({
        "record_id": None,
        "summary": {
            "considered": len(ctx.historical_records),
            "used": len(ev.used),
            "below_similarity_threshold": ev.below_threshold,
            "invalid": ev.invalid,
        },
    })

    return {
        "query_features": feat_items,
        "database_state": db_items,
        "system_state": sys_items,
        "historical_records": hist_items,
    }


# 默认单例，便于直接调用
default_agent = OptimizationDecisionAgent()


def decide(decision_input: dict, llm: Optional[object] = None,
           llm_decide: bool = False) -> dict:
    """Optimization Decision Agent 的便捷入口。

    llm 为可选 LLMClient（混合增强）；llm_decide=True 时 LLM 参与策略选择
    （合法性校验 + 确定性兜底，见 OptimizationDecisionAgent.decide）。
    """
    return default_agent.decide(decision_input, llm=llm, llm_decide=llm_decide)


def run_agent(decision_input: dict, *, execute: Callable[[dict], dict],
              agent_config: Optional[dict] = None,
              llm: Optional[object] = None,
              llm_decide: bool = False) -> dict:
    """Optimization Decision Agent 的闭环入口（带执行/观察/重规划）。"""
    return default_agent.run(decision_input, execute=execute,
                             agent_config=agent_config,
                             llm=llm, llm_decide=llm_decide)
