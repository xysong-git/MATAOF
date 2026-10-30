"""Execution Monitoring Agent（执行监控智能体）。

核心职责：
    监控当前查询的实际执行情况，采集真实运行指标，将实际执行结果与所采用的
    优化策略建立对应关系（Query + Selected Strategy + Actual Execution Result），
    并生成结构化执行反馈。

职责边界：
    - 事实采集 Agent：输出回答"实际执行发生了什么"，不回答"下一步该用什么策略"；
    - 不重新选择优化策略、不修改策略；
    - 所有指标必须来自真实执行环境或监控接口；无法获得 → null/unknown，
      绝不估计延迟、猜测 CPU/内存、虚构吞吐量与 P95/P99；
    - 无 baseline 时不生成比较结果；单次执行观察不推断策略永久有效；
    - 异常只记录，不处理。

输入：
    {
      "query_id": "...",
      "strategy_id": "...",            # 实际执行的策略组合标识
      "execution_status": "success",   # 可选：success/timeout/failed/cancelled/unknown
      "failure_reason": "...",         # failed 时的原因（可选）
      "metrics": { ... },              # 实测指标（全部可选；无效值 → null）
      "baseline_metrics": { ... },     # 可选：系统提供的 baseline 执行结果
      "historical_reference": {...},   # 可选：历史典型表现（偏离检测/效果判断参照）
      "thresholds": { ... }            # 可选：异常阈值覆盖
    }

输出：始终为 JSON 可序列化字典（见 schemas.execution_monitoring_output_template）。

两条路径：
- `monitor()`：确定性纯函数，按固定顺序完成全部事实处理；
- `run()`：Agent 闭环。每一步的事实由工具产出，策略根据观察决定是否继续
  （例如没有 baseline 就跳过比较）、以及是否调用 LLM 解读（默认只在异常/
  退化时调用）。确定性字段与 `monitor()` 一致；跳过比较/解读时 notes 记录原因。
"""

from __future__ import annotations

from typing import Optional

from mataof.runtime import (
    Act,
    AgentContext,
    AgentRuntime,
    Finish,
    Goal,
    ToolRegistry,
)
from mataof.schemas import execution_monitoring_output_template
from mataof.agents.execution_monitoring.normalize import (
    normalize_metrics,
    normalize_status,
    resolve_thresholds,
)
from mataof.agents.execution_monitoring.comparison import (
    compare_with_baseline,
    empty_comparison,
)
from mataof.agents.execution_monitoring.assessment import (
    assess,
    strategy_effective_mapping,
)
from mataof.agents.execution_monitoring.anomalies import detect_anomalies
from mataof.agents.execution_monitoring.llm_enhance import build_llm_analysis

_AGENT_DEFAULTS = {
    "enabled": False,
    "llm_policy": "on_anomaly",   # on_anomaly | on_anomaly_or_degraded | always | never
    "deadline_ms": 5000.0,
}
_LLM_POLICIES = ("on_anomaly", "on_anomaly_or_degraded", "always", "never")


def llm_interpretation_needed(monitoring_output: dict, policy: str) -> bool:
    """执行监控 LLM 解读的触发条件（Agent 与 Runner 合并调用共用同一口径）。

    - never：从不触发；
    - always：总是触发；
    - on_anomaly（默认）：存在异常，或执行状态为 failed/timeout 时触发；
      degraded 本身不触发（退化幅度普遍很小，逐条解读代价高、收益低）；
    - on_anomaly_or_degraded（消融）：有异常、评估为 degraded/failed，
      或执行状态为 failed/timeout 时触发。
    未知策略按默认策略（on_anomaly）处理，安全、有界。
    """
    out = monitoring_output if isinstance(monitoring_output, dict) else {}
    if policy == "never":
        return False
    if policy == "always":
        return True
    failed = out.get("execution_status") in ("failed", "timeout")
    if policy == "on_anomaly_or_degraded":
        return (bool(out.get("anomalies"))
                or out.get("performance_assessment") in ("degraded", "failed")
                or failed)
    return bool(out.get("anomalies")) or failed


def _normalize_agent_config(raw: object) -> dict:
    cfg = dict(_AGENT_DEFAULTS)
    if not isinstance(raw, dict):
        return cfg
    if "enabled" in raw:
        cfg["enabled"] = bool(raw["enabled"])
    policy = raw.get("llm_policy")
    if policy in _LLM_POLICIES:
        cfg["llm_policy"] = policy
    deadline = raw.get("deadline_ms")
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
        cfg["deadline_ms"] = max(1.0, float(deadline))
    return cfg


class ExecutionMonitoringAgent:
    """执行监控智能体。

    用法：
        agent = ExecutionMonitoringAgent()
        result = agent.monitor({
            "query_id": "q1",
            "strategy_id": "time_pruning=partition_pruning|...",
            "execution_status": "success",
            "metrics": {"response_time_ms": 12.3, ...},
            "baseline_metrics": {...},   # 可选
        })
        agent_out = agent.run({...}, agent_config={"enabled": True})
    """

    name = "execution_monitoring"
    description = "采集查询的真实执行指标，关联执行策略，生成结构化执行反馈。"

    # ------------------------------------------------------------------
    # 确定性路径（原有行为完全保留）
    # ------------------------------------------------------------------
    def monitor(self, monitoring_input: dict,
                llm: Optional[object] = None) -> dict:
        """事实采集入口。llm：可选 LLMClient（混合增强模式）。

        - LLM 只填充 llm_analysis 节（事实复述 + 异常关联解读），
          数字级校验拒绝任何给定数据之外的数字；
        - llm=None / 失败 / 非法输出 → 事实字段完全不变（确定性兜底）。
        """
        output = execution_monitoring_output_template()
        inp = monitoring_input if isinstance(monitoring_input, dict) else {}
        notes: list[str] = []

        output["query_id"] = str(inp.get("query_id") or "")
        output["strategy_id"] = str(inp.get("strategy_id") or "")

        # ---- 事实采集：状态与指标 ----
        status, failure_reason = normalize_status(
            inp.get("execution_status"), inp.get("failure_reason"), notes
        )
        output["execution_status"] = status
        output["failure_reason"] = failure_reason

        thresholds = resolve_thresholds(inp.get("thresholds"), notes)
        metrics = normalize_metrics(inp.get("metrics"), notes)
        output["metrics"] = metrics

        # ---- Baseline 比较（无 baseline 不生成比较）----
        comparison, labels = compare_with_baseline(
            metrics, inp.get("baseline_metrics"), thresholds, notes
        )
        output["baseline_comparison"] = comparison
        output["performance_change"] = labels

        # ---- 策略效果判断（基于实测，单次观察不夸大）----
        assessment, conf, basis = assess(
            status, metrics, comparison, inp.get("historical_reference"),
            thresholds, notes,
        )
        output["performance_assessment"] = assessment
        output["feedback"]["strategy_effective"] = strategy_effective_mapping(assessment)
        output["feedback"]["confidence"] = conf
        output["feedback"]["assessment_basis"] = basis

        # ---- 异常识别（只记录）----
        baseline = normalize_metrics(inp.get("baseline_metrics"), notes, prefix="baseline.")
        output["anomalies"] = detect_anomalies(
            status, failure_reason, metrics, baseline,
            inp.get("historical_reference"), thresholds,
        )

        output["notes"] = notes

        # ---- LLM 语义增强（只进 llm_analysis 节；失败不影响事实）----
        output["llm_analysis"] = build_llm_analysis(llm, output)
        return output

    # ------------------------------------------------------------------
    # Agent 闭环：事实管线 → 按观察分支 →（异常/退化时）LLM 解读 → 终止
    # ------------------------------------------------------------------
    def run(self, monitoring_input: dict, llm: Optional[object] = None,
            agent_config: Optional[dict] = None,
            external_llm: bool = False) -> dict:
        """Agent 模式监控。返回 {"monitoring": <监控输出>, "agent": <元数据>}。

        external_llm=True 表示调用方（runner）会统一管理 LLM 解读，本 Agent
        不重复调用（避免同一事实两次 LLM 调用）。
        """
        cfg = _normalize_agent_config(agent_config)
        inp = monitoring_input if isinstance(monitoring_input, dict) else {}
        state = {
            "input": inp,
            "cfg": cfg,
            "llm": llm,
            "external_llm": bool(external_llm),
            "notes": [],
            "output": execution_monitoring_output_template(),
            "facts": None,
            "comparison_performed": False,
            "comparison_skip_reason": None,
            "llm_skip_reason": None,
            "llm_used": False,
            "error": None,
        }

        tools = ToolRegistry()

        def normalize_facts(monitoring_input: dict) -> dict:
            raw = monitoring_input if isinstance(monitoring_input, dict) else {}
            notes: list[str] = []
            status, failure_reason = normalize_status(
                raw.get("execution_status"), raw.get("failure_reason"), notes)
            thresholds = resolve_thresholds(raw.get("thresholds"), notes)
            metrics = normalize_metrics(raw.get("metrics"), notes)
            baseline_raw = raw.get("baseline_metrics")
            return {
                "status": status,
                "failure_reason": failure_reason,
                "thresholds": thresholds,
                "metrics": metrics,
                "notes": notes,
                "has_metrics": any(v is not None for v in metrics.values()),
                "has_baseline": isinstance(baseline_raw, dict) and bool(baseline_raw),
            }

        def compare_baseline(metrics: dict, baseline_metrics: Optional[dict],
                             thresholds: dict) -> dict:
            notes: list[str] = []
            comparison, labels = compare_with_baseline(
                metrics, baseline_metrics, thresholds, notes)
            return {"comparison": comparison, "labels": labels, "notes": notes}

        def assess_effect(execution_status: str, metrics: dict,
                          baseline_comparison: dict,
                          historical_reference: Optional[dict],
                          thresholds: dict) -> dict:
            notes: list[str] = []
            assessment, confidence, basis = assess(
                execution_status, metrics, baseline_comparison,
                historical_reference, thresholds, notes)
            return {"assessment": assessment, "confidence": confidence,
                    "basis": basis, "notes": notes}

        def anomalies_tool(execution_status: str,
                           failure_reason: Optional[str], metrics: dict,
                           baseline_metrics: Optional[dict],
                           historical_reference: Optional[dict],
                           thresholds: dict) -> dict:
            notes: list[str] = []
            baseline = normalize_metrics(
                baseline_metrics, notes, prefix="baseline.")
            anomalies = detect_anomalies(
                execution_status, failure_reason, metrics, baseline,
                historical_reference, thresholds)
            return {"anomalies": anomalies, "notes": notes}

        def llm_tool(monitoring_output: dict) -> dict:
            return build_llm_analysis(llm, monitoring_output)

        tools.register(
            "normalize_facts", normalize_facts,
            description="校验执行状态/指标/阈值（无效值置 null，不估计）",
            parameters={"required": ["monitoring_input"],
                        "properties": {"monitoring_input": {"type": "object"}}},
            side_effect="none", cost="low",
        )
        tools.register(
            "compare_baseline", compare_baseline,
            description="仅在实测指标与 baseline 均可用时计算变化百分比",
            parameters={"required": ["metrics", "baseline_metrics", "thresholds"],
                        "properties": {"metrics": {"type": "object"},
                                       "baseline_metrics": {"type": "object"},
                                       "thresholds": {"type": "object"}}},
            side_effect="none", cost="low",
        )
        tools.register(
            "assess_effect", assess_effect,
            description="基于实测与 baseline/历史参照判断策略效果",
            parameters={"required": ["execution_status", "metrics",
                                     "baseline_comparison", "historical_reference",
                                     "thresholds"],
                        "properties": {"execution_status": {"type": "string"},
                                       "metrics": {"type": "object"},
                                       "baseline_comparison": {"type": "object"},
                                       "historical_reference": {"type": "object"},
                                       "thresholds": {"type": "object"}}},
            side_effect="none", cost="low",
        )
        tools.register(
            "detect_anomalies", anomalies_tool,
            description="识别超时/失败/延迟/资源/历史偏离异常（只记录）",
            parameters={"required": ["execution_status", "failure_reason",
                                     "metrics", "baseline_metrics",
                                     "historical_reference", "thresholds"],
                        "properties": {"execution_status": {"type": "string"},
                                       "failure_reason": {"type": "string"},
                                       "metrics": {"type": "object"},
                                       "baseline_metrics": {"type": "object"},
                                       "historical_reference": {"type": "object"},
                                       "thresholds": {"type": "object"}}},
            side_effect="none", cost="low",
        )
        tools.register(
            "llm_annotate", llm_tool,
            description="LLM 事实复述与异常关联解读（数字级校验；只写 llm_analysis）",
            parameters={"required": ["monitoring_output"],
                        "properties": {"monitoring_output": {"type": "object"}}},
            side_effect="none", cost="high",
        )

        runtime = AgentRuntime(self.name, tools, self._agent_policy)
        run_result = runtime.run(
            Goal(name="monitor_execution", max_steps=6,
                 deadline_ms=cfg["deadline_ms"]),
            AgentContext(data=state),
        )
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

    # ------------------------------------------------------------------
    # Agent 策略
    # ------------------------------------------------------------------
    def _agent_policy(self, ctx: AgentContext, tools: ToolRegistry):
        state = ctx.data
        obs = ctx.last_observation

        if obs is None:
            return Act("normalize_facts",
                       {"monitoring_input": state["input"]},
                       reason="校验执行状态与实测指标")

        if obs.tool == "normalize_facts":
            if not obs.ok:
                state["error"] = obs.error
                state["output"] = self._fallback_output(state, obs.error)
                return self._finish(state, "normalize_failed")
            facts = obs.value if isinstance(obs.value, dict) else {}
            state["facts"] = facts
            out = state["output"]
            inp = state["input"]
            out["query_id"] = str(inp.get("query_id") or "")
            out["strategy_id"] = str(inp.get("strategy_id") or "")
            out["execution_status"] = facts.get("status", "unknown")
            out["failure_reason"] = facts.get("failure_reason")
            out["metrics"] = facts.get("metrics") or {}
            state["notes"].extend(facts.get("notes") or [])
            out["notes"] = list(state["notes"])

            if facts.get("has_metrics") and facts.get("has_baseline"):
                state["comparison_performed"] = True
                return Act(
                    "compare_baseline",
                    {"metrics": out["metrics"],
                     "baseline_metrics": inp.get("baseline_metrics"),
                     "thresholds": facts.get("thresholds") or {}},
                    reason="实测指标与 baseline 均可用，计算变化",
                )

            # 没有 baseline 或没有有效实测指标：比较没有事实基础，跳过。
            state["comparison_performed"] = False
            if not facts.get("has_baseline"):
                # 与确定性路径同一条 note（无 baseline 时不生成比较结果）
                state["comparison_skip_reason"] = "no_baseline"
                state["notes"].append("未提供 baseline 执行结果，不生成比较结果")
            else:
                state["comparison_skip_reason"] = "no_metrics"
                state["notes"].append("无有效实测指标，跳过 baseline 比较")
            out["baseline_comparison"] = empty_comparison()
            out["performance_change"] = []
            out["notes"] = list(state["notes"])
            return self._act_assess(state)

        if obs.tool == "compare_baseline":
            out = state["output"]
            if obs.ok and isinstance(obs.value, dict):
                value = obs.value
                out["baseline_comparison"] = value.get("comparison") or empty_comparison()
                out["performance_change"] = list(value.get("labels") or [])
                state["notes"].extend(value.get("notes") or [])
            else:
                out["baseline_comparison"] = empty_comparison()
                out["performance_change"] = []
                state["notes"].append(f"baseline 比较工具失败：{obs.error}")
                state["comparison_skip_reason"] = "tool_error"
            out["notes"] = list(state["notes"])
            return self._act_assess(state)

        if obs.tool == "assess_effect":
            out = state["output"]
            facts = state["facts"] or {}
            if obs.ok and isinstance(obs.value, dict):
                value = obs.value
                assessment = value.get("assessment") or "insufficient_evidence"
                out["performance_assessment"] = assessment
                out["feedback"]["strategy_effective"] = strategy_effective_mapping(assessment)
                out["feedback"]["confidence"] = value.get("confidence") or 0.0
                out["feedback"]["assessment_basis"] = list(value.get("basis") or [])
                state["notes"].extend(value.get("notes") or [])
            else:
                out["performance_assessment"] = "insufficient_evidence"
                out["feedback"]["strategy_effective"] = None
                out["feedback"]["confidence"] = 0.0
                out["feedback"]["assessment_basis"] = [
                    f"效果判断工具失败：{obs.error}"]
                state["notes"].append("效果判断工具失败，结论置为 insufficient_evidence")
            out["notes"] = list(state["notes"])
            return Act(
                "detect_anomalies",
                {"execution_status": facts.get("status", "unknown"),
                 "failure_reason": facts.get("failure_reason"),
                 "metrics": out["metrics"],
                 "baseline_metrics": state["input"].get("baseline_metrics"),
                 "historical_reference": state["input"].get("historical_reference"),
                 "thresholds": facts.get("thresholds") or {}},
                reason="识别执行异常（只记录，不改变策略）",
            )

        if obs.tool == "detect_anomalies":
            out = state["output"]
            if obs.ok and isinstance(obs.value, dict):
                out["anomalies"] = list(obs.value.get("anomalies") or [])
                state["notes"].extend(obs.value.get("notes") or [])
            else:
                out["anomalies"] = []
                state["notes"].append(f"异常识别工具失败：{obs.error}")
            out["notes"] = list(state["notes"])
            return self._decide_llm(state)

        if obs.tool == "llm_annotate":
            out = state["output"]
            if obs.ok and isinstance(obs.value, dict):
                out["llm_analysis"] = obs.value
                state["llm_used"] = True
                return self._finish(state, "llm_annotated")
            out["llm_analysis"] = {
                "available": False, "performance_summary": None,
                "anomaly_summary": None,
                "notes": [f"LLM 解读工具失败：{obs.error}"],
            }
            state["llm_skip_reason"] = "llm_tool_failed"
            return self._finish(state, "llm_failed")

        return self._finish(state, "unexpected_tool")

    def _act_assess(self, state: dict):
        facts = state["facts"] or {}
        return Act(
            "assess_effect",
            {"execution_status": facts.get("status", "unknown"),
             "metrics": state["output"].get("metrics") or {},
             "baseline_comparison": state["output"].get("baseline_comparison")
             or empty_comparison(),
             "historical_reference": state["input"].get("historical_reference"),
             "thresholds": facts.get("thresholds") or {}},
            reason="基于实测参照判断策略效果",
        )

    def _decide_llm(self, state: dict):
        """LLM 只在需要解读时调用（默认异常/退化），避免每条查询固定开销。"""
        cfg = state["cfg"]
        out = state["output"]
        llm = state["llm"]
        out["notes"] = list(state["notes"])

        if state.get("external_llm"):
            state["llm_skip_reason"] = "runner_manages_llm"
            out["llm_analysis"] = {
                "available": False, "performance_summary": None,
                "anomaly_summary": None,
                "notes": ["LLM 解读由 runner 合并调用管理，监控 Agent 不重复调用"],
            }
            return self._finish(state, "monitored")

        if llm is None:
            state["llm_skip_reason"] = "llm_unavailable"
            out["llm_analysis"] = build_llm_analysis(None, out)
            return self._finish(state, "monitored")

        policy = cfg["llm_policy"]
        if policy == "never":
            state["llm_skip_reason"] = "llm_policy_never"
            out["llm_analysis"] = self._skipped_section(
                "llm_policy=never，跳过 LLM 解读")
            return self._finish(state, "monitored")

        if not llm_interpretation_needed(out, policy):
            if policy == "on_anomaly":
                state["llm_skip_reason"] = "no_anomaly"
                reason = "无异常且未失败，按 llm_policy=on_anomaly 跳过 LLM 解读"
            else:
                state["llm_skip_reason"] = "no_anomaly_or_degradation"
                reason = (f"无异常且未退化，按 llm_policy={policy} "
                          "跳过 LLM 解读")
            out["llm_analysis"] = self._skipped_section(reason)
            return self._finish(state, "monitored")

        return Act("llm_annotate", {"monitoring_output": out},
                   reason="异常/执行失败需要语义解读")

    @staticmethod
    def _skipped_section(reason: str) -> dict:
        return {
            "available": False, "performance_summary": None,
            "anomaly_summary": None, "notes": [reason],
        }

    @staticmethod
    def _fallback_output(state: dict, error: Optional[str]) -> dict:
        """事实采集失败时的诚实兜底：状态 unknown、指标全 null，不猜测。"""
        inp = state.get("input") or {}
        out = execution_monitoring_output_template()
        out["query_id"] = str(inp.get("query_id") or "")
        out["strategy_id"] = str(inp.get("strategy_id") or "")
        out["execution_status"] = "unknown"
        note = f"事实采集失败：{error or '未知错误'}；执行事实保持 unknown"
        out["notes"] = [note]
        state.setdefault("notes", []).append(note)   # _finish 以 state["notes"] 为准
        out["llm_analysis"] = {
            "available": False, "performance_summary": None,
            "anomaly_summary": None,
            "notes": ["事实采集失败，跳过 LLM 解读"],
        }
        return out

    def _finish(self, state: dict, reason: str):
        state["output"]["notes"] = list(state["notes"])
        return Finish(result=self._agent_payload(state), reason=reason)

    @staticmethod
    def _agent_payload(state: dict) -> dict:
        cfg = state.get("cfg") or _AGENT_DEFAULTS
        out = state.get("output")
        if not isinstance(out, dict):
            out = execution_monitoring_output_template()
        return {
            "monitoring": out,
            "agent": {
                "enabled": True,
                "runtime": "mataof.runtime",
                "policy": "facts_pipeline",
                "llm_policy": cfg.get("llm_policy"),
                "external_llm": bool(state.get("external_llm")),
                "llm_used": bool(state.get("llm_used")),
                "llm_skip_reason": state.get("llm_skip_reason"),
                "comparison_performed": bool(state.get("comparison_performed")),
                "comparison_skip_reason": state.get("comparison_skip_reason"),
                "assessment": out.get("performance_assessment"),
                "anomaly_count": len(out.get("anomalies") or []),
                "error": state.get("error"),
            },
        }


# 默认单例，便于直接调用
default_agent = ExecutionMonitoringAgent()


def monitor(monitoring_input: dict, llm: Optional[object] = None) -> dict:
    """Execution Monitoring Agent 的便捷入口。llm 为可选 LLMClient（混合增强）。"""
    return default_agent.monitor(monitoring_input, llm=llm)


def run_agent(monitoring_input: dict, llm: Optional[object] = None,
              agent_config: Optional[dict] = None,
              external_llm: bool = False) -> dict:
    """Agent 模式便捷入口：返回 {"monitoring": ..., "agent": ...}。"""
    return default_agent.run(monitoring_input, llm=llm,
                             agent_config=agent_config,
                             external_llm=external_llm)
