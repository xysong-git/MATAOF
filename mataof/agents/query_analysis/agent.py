"""Query Analysis Agent（查询分析智能体）。

唯一核心职责：
    对输入的时序数据库查询进行语义解析、结构分析和优化相关特征提取，
    为后续 Optimization Decision Agent 提供结构化查询上下文。

职责边界（严格遵守）：
    - 不选择优化策略、不修改 SQL、不生成执行计划、不执行查询；
    - 不虚构数据库统计信息 / 设备数量 / 数据量 / 历史经验；
    - 无法确定的信息一律 null / [] / "unknown"，并在 unknown_features 记录原因；
    - 优化相关性只陈述"属于相关候选优化维度"，不声称任何策略更优。

输入（结构化 JSON 字典或独立参数）：
    query          ：查询文本（必填）
    query_id       ：查询标识（可选，贯穿全流程的追踪键）
    database_state ：数据库提供的状态/统计信息（可选）。当前识别两个键：
                     - statistics.selectivity   ：{列引用: 选择率(0~1)}，用于 filter.selectivity
                     - scan_estimate            ：{"time_range": [start_ms, end_ms],
                                                   "estimated_points": 点数}，用于 scan 节
                     未提供的字段一律 unknown，绝不估计。

输出：见 schemas.query_analysis_output_template()，始终为 JSON 可序列化字典。

两条路径：
- `analyze()`：确定性纯函数（可选 LLM 语义增强），相同输入 → 相同确定性字段；
- `run()`：Agent 闭环（分析 → 检查证据缺口 → 按策略决定是否调用 LLM 标注 →
  终止）。工具调用失败会成为观察，确定性分析结果仍然可用。默认由 runner
  按 `agents.query_analysis.enabled` 开启；未开启时主链路完全不变。
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
from mataof.schemas import query_analysis_output_template
from mataof.agents.query_analysis.llm_enhance import build_llm_analysis
from mataof.agents.query_analysis.vectorize import encode_features
from mataof.agents.query_analysis.parser import parse_query, ParseContext
from mataof.agents.query_analysis.features import (
    time as time_features,
    device as device_features,
    filter as filter_features,
    aggregation as aggregation_features,
    scan as scan_features,
    query_type as query_type_features,
)
from mataof.agents.query_analysis.features.base import structural_flags
from mataof.agents.query_analysis.relevance import build_optimization_relevance
from mataof.agents.query_analysis.confidence import compute_confidence

_AGENT_DEFAULTS = {
    "enabled": False,
    "llm_policy": "on_gap",          # on_gap | always | never
    "confidence_threshold": 0.5,     # 低于该置信度视为证据缺口
    "deadline_ms": 10000.0,
}
_LLM_POLICIES = ("on_gap", "always", "never")


def _normalize_agent_config(raw: object) -> dict:
    """校验/钳制 Agent 配置（非法值回退默认，不抛异常）。"""
    cfg = dict(_AGENT_DEFAULTS)
    if not isinstance(raw, dict):
        return cfg
    if "enabled" in raw:
        cfg["enabled"] = bool(raw["enabled"])
    policy = raw.get("llm_policy")
    if policy in _LLM_POLICIES:
        cfg["llm_policy"] = policy
    threshold = raw.get("confidence_threshold")
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        cfg["confidence_threshold"] = max(0.0, min(1.0, float(threshold)))
    deadline = raw.get("deadline_ms")
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
        cfg["deadline_ms"] = max(1.0, float(deadline))
    return cfg


class QueryAnalysisAgent:
    """查询分析智能体。

    用法：
        agent = QueryAnalysisAgent()
        result = agent.analyze(query="SELECT ...", query_id="q1", database_state={...})
        agent_out = agent.run(query="SELECT ...", query_id="q1", llm=client)
    """

    name = "query_analysis"
    description = "对时序数据库查询进行语义解析与优化相关特征提取，产出结构化查询上下文。"

    # ------------------------------------------------------------------
    # 确定性分析（纯函数；analyze 与 Agent 工具共用）
    # ------------------------------------------------------------------
    def _deterministic_output(self, query: str, query_id: str = "",
                              database_state: Optional[dict] = None
                              ) -> tuple[dict, ParseContext]:
        """产出确定性分析结果（不含 LLM 调用），返回 (output, parse_ctx)。

        output 已包含 feature_vector；llm_analysis 为模板中的未启用占位，
        由调用方决定是否填充。
        """
        output = query_analysis_output_template()
        output["query_id"] = str(query_id or "")
        output["query"] = query

        notes: list[str] = []
        ctx = parse_query(query)
        if ctx.multi_statement:
            notes.append("输入包含多条语句，仅分析第一条")

        # ---- 特征提取 ----
        time_section = time_features.extract_time_features(ctx, notes)
        device_section = device_features.extract_device_features(ctx, notes)
        filter_section = filter_features.extract_filter_features(ctx, database_state, notes)
        structural = structural_flags(ctx.ast)

        non_time_count = sum(v for k, v in filter_section["type_counts"].items() if k != "time")
        aggregation_section = aggregation_features.extract_aggregation_features(
            ctx, device_section["device_paths"],
            (time_section["start_time"], time_section["end_time"]),
            has_non_time_filters=non_time_count > 0,
            notes=notes,
        )
        scan_section = scan_features.extract_scan_features(
            ctx, database_state, time_section, device_section,
            aggregation_section, filter_section, structural, notes,
        )

        # ---- 查询类型 ----
        query_type, type_note = query_type_features.classify_query_type(
            ctx, structural, time_section, filter_section, aggregation_section,
        )
        if type_note:
            notes.append(f"查询类型：unknown（{type_note}）")

        # ---- 组装 ----
        output["query_type"] = query_type
        output["query_features"] = {
            "time": time_section,
            "device": device_section,
            "filter": filter_section,
            "aggregation": aggregation_section,
            "scan": scan_section,
        }
        output["optimization_relevant_features"] = build_optimization_relevance(
            time_section, filter_section, aggregation_section,
        )
        output["unknown_features"] = notes
        output["analysis_confidence"] = compute_confidence(
            ctx, query_type, time_section, device_section,
            filter_section, scan_section, aggregation_section,
        )

        # ---- 特征向量化（开题报告 3.3：固定维度向量，只由确定性特征编码）----
        output["feature_vector"] = encode_features(output)
        return output, ctx

    def analyze(self, query: str, query_id: str = "",
                database_state: Optional[dict] = None,
                llm: Optional[object] = None) -> dict:
        """分析一条查询，返回结构化 JSON（字典）。任何输入都不抛异常。

        llm：可选的 LLMClient（mataof.llm）。混合增强模式：
        - 确定性提取字段始终权威，LLM 只填充 llm_analysis 节；
        - llm=None / 调用失败 / 输出非法 → llm_analysis.available=false，
          确定性输出与不启用 LLM 时完全一致（确定性兜底）。
        """
        output, _ = self._deterministic_output(query, query_id, database_state)
        # ---- LLM 语义增强（混合增强：只进 llm_analysis 节，失败不影响确定性结果）----
        output["llm_analysis"] = build_llm_analysis(query, output, llm)
        return output

    # ------------------------------------------------------------------
    # Agent 闭环：分析 → 缺口判断 →（按需）LLM 标注 → 终止
    # ------------------------------------------------------------------
    def run(self, query: str, query_id: str = "",
            database_state: Optional[dict] = None,
            llm: Optional[object] = None,
            agent_config: Optional[dict] = None) -> dict:
        """Agent 模式分析：确定性特征提取是固定第一步，LLM 是否调用由观察决定。

        返回 {"analysis": <QueryAnalysis 输出>, "agent": <运行元数据>}。
        与 `analyze()` 的确定性字段逐字段一致；llm_analysis 可能因
        `llm_policy` 跳过调用而保持 available=false（并记录原因）。
        """
        cfg = _normalize_agent_config(agent_config)
        state = {
            "query": query,
            "query_id": str(query_id or ""),
            "database_state": database_state,
            "llm": llm,
            "cfg": cfg,
            "analysis": None,
            "gap_signals": [],
            "gap_details": {},
            "llm_skip_reason": None,
            "llm_used": False,
            "analysis_failed": False,
            "error": None,
        }

        tools = ToolRegistry()

        def analyze_tool(query: str, query_id: str = "",
                         database_state: Optional[dict] = None) -> dict:
            output, parse_ctx = self._deterministic_output(
                query, query_id, database_state)
            return {
                "analysis": output,
                "signals": self._gap_signals(output, parse_ctx, cfg),
            }

        def llm_tool(query: str, analysis: dict) -> dict:
            return build_llm_analysis(query, analysis, llm)

        tools.register(
            "analyze_deterministic", analyze_tool,
            description="确定性解析 + 特征提取 + 置信度 + 特征向量（无外部调用）",
            parameters={"required": ["query"],
                        "properties": {"query": {"type": "string"},
                                       "query_id": {"type": "string"},
                                       "database_state": {"type": "object"}}},
            side_effect="read", cost="low",
        )
        tools.register(
            "llm_annotate", llm_tool,
            description="在确定性结果上补充语义标注（只写 llm_analysis 节）",
            parameters={"required": ["query", "analysis"],
                        "properties": {"query": {"type": "string"},
                                       "analysis": {"type": "object"}}},
            side_effect="read", cost="high",
        )

        runtime = AgentRuntime(self.name, tools, self._agent_policy)
        run_result = runtime.run(
            Goal(name="analyze_query", max_steps=3,
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

        # ---- 第一步：确定性分析（始终执行，是后续一切判断的事实基础）----
        if obs is None:
            return Act(
                "analyze_deterministic",
                {"query": state["query"], "query_id": state["query_id"],
                 "database_state": state["database_state"]},
                reason="确定性解析与特征提取",
            )

        if obs.tool == "analyze_deterministic":
            if not obs.ok:
                state["analysis_failed"] = True
                state["error"] = obs.error
                state["analysis"] = self._fallback_analysis(state, obs.error)
                return Finish(result=self._agent_payload(state),
                              reason="analysis_failed")
            value = obs.value if isinstance(obs.value, dict) else {}
            state["analysis"] = value.get("analysis") or self._fallback_analysis(
                state, "确定性分析工具未返回结果")
            state["gap_signals"] = list(value.get("signals", {}).get("gaps") or [])
            state["gap_details"] = dict(value.get("signals", {}).get("details") or {})
            return self._decide_llm(state)

        if obs.tool == "llm_annotate":
            analysis = state["analysis"]
            if obs.ok and isinstance(obs.value, dict):
                analysis["llm_analysis"] = obs.value
                state["llm_used"] = True
                return Finish(result=self._agent_payload(state),
                              reason="llm_annotated")
            analysis["llm_analysis"] = {
                "available": False, "semantic_summary": None,
                "query_type_hint": None, "condition_hints": {},
                "notes": [f"LLM 标注工具失败：{obs.error}"],
            }
            state["llm_skip_reason"] = "llm_tool_failed"
            return Finish(result=self._agent_payload(state),
                          reason="llm_failed")

        return Finish(result=self._agent_payload(state), reason="unexpected_tool")

    def _decide_llm(self, state: dict):
        """根据缺口信号与 llm_policy 决定是否调用 LLM 标注。"""
        cfg = state["cfg"]
        analysis = state["analysis"]
        policy = cfg["llm_policy"]
        llm = state["llm"]

        if llm is None:
            state["llm_skip_reason"] = "llm_unavailable"
            analysis["llm_analysis"] = build_llm_analysis(
                state["query"], analysis, None)
            return Finish(result=self._agent_payload(state), reason="analyzed")
        if policy == "never":
            state["llm_skip_reason"] = "llm_policy_never"
            analysis["llm_analysis"] = self._skipped_section(
                "llm_policy=never，跳过 LLM 语义增强")
            return Finish(result=self._agent_payload(state), reason="analyzed")
        if policy == "on_gap" and not state["gap_signals"]:
            state["llm_skip_reason"] = "no_gap"
            analysis["llm_analysis"] = self._skipped_section(
                "llm_policy=on_gap：未发现证据缺口，跳过 LLM 语义增强")
            return Finish(result=self._agent_payload(state), reason="analyzed")

        return Act(
            "llm_annotate",
            {"query": state["query"], "analysis": analysis},
            reason=("llm_policy=always" if policy == "always"
                    else f"证据缺口：{', '.join(state['gap_signals'])}"),
        )

    @staticmethod
    def _skipped_section(reason: str) -> dict:
        return {
            "available": False, "semantic_summary": None,
            "query_type_hint": None, "condition_hints": {},
            "notes": [reason],
        }

    @staticmethod
    def _gap_signals(output: dict, parse_ctx: ParseContext,
                     cfg: dict) -> dict:
        """从确定性结果识别 LLM 能补足的语义缺口（不把统计缺失算作缺口）。"""
        filter_section = (output.get("query_features") or {}).get("filter") or {}
        ambiguous = [
            c for c in filter_section.get("conditions") or []
            if c.get("type") == "tag_or_attribute"
        ]
        gaps: list[str] = []
        if parse_ctx.parse_error:
            gaps.append("parse_error")
        if output.get("query_type") == "unknown":
            gaps.append("query_type_unknown")
        if float(output.get("analysis_confidence") or 0.0) < cfg["confidence_threshold"]:
            gaps.append("low_confidence")
        if ambiguous:
            gaps.append("ambiguous_filter_types")
        return {
            "gaps": gaps,
            "details": {
                "parse_error": parse_ctx.parse_error,
                "multi_statement": bool(parse_ctx.multi_statement),
                "analysis_confidence": output.get("analysis_confidence"),
                "confidence_threshold": cfg["confidence_threshold"],
                "query_type": output.get("query_type"),
                "ambiguous_condition_count": len(ambiguous),
                "unknown_feature_count": len(output.get("unknown_features") or []),
            },
        }

    def _fallback_analysis(self, state: dict, error: Optional[str]) -> dict:
        """分析工具失败时的诚实兜底：不编造任何特征，只保留输入与错误。"""
        output = query_analysis_output_template()
        output["query_id"] = state["query_id"]
        output["query"] = state["query"]
        output["unknown_features"] = [
            f"确定性分析失败：{error or '未知错误'}；所有特征保持 unknown"
        ]
        output["llm_analysis"] = {
            "available": False, "semantic_summary": None,
            "query_type_hint": None, "condition_hints": {},
            "notes": ["确定性分析失败，跳过 LLM 语义增强"],
        }
        return output

    @staticmethod
    def _agent_payload(state: dict) -> dict:
        cfg = state.get("cfg") or _AGENT_DEFAULTS
        analysis = state.get("analysis")
        if not isinstance(analysis, dict):
            analysis = query_analysis_output_template()
        return {
            "analysis": analysis,
            "agent": {
                "enabled": True,
                "runtime": "mataof.runtime",
                "policy": "gap_aware_llm",
                "llm_policy": cfg.get("llm_policy"),
                "confidence_threshold": cfg.get("confidence_threshold"),
                "llm_used": bool(state.get("llm_used")),
                "llm_skip_reason": state.get("llm_skip_reason"),
                "gap_signals": list(state.get("gap_signals") or []),
                "gap_details": dict(state.get("gap_details") or {}),
                "analysis_failed": bool(state.get("analysis_failed")),
                "error": state.get("error"),
            },
        }


# 默认单例，便于直接调用
default_agent = QueryAnalysisAgent()


def analyze(query: str, query_id: str = "",
            database_state: Optional[dict] = None,
            llm: Optional[object] = None) -> dict:
    """Query Analysis Agent 的便捷入口。llm 为可选的 LLMClient（混合增强）。"""
    return default_agent.analyze(query=query, query_id=query_id,
                                 database_state=database_state, llm=llm)


def run_agent(query: str, query_id: str = "",
              database_state: Optional[dict] = None,
              llm: Optional[object] = None,
              agent_config: Optional[dict] = None) -> dict:
    """Agent 模式便捷入口：返回 {"analysis": ..., "agent": ...}。"""
    return default_agent.run(query=query, query_id=query_id,
                             database_state=database_state, llm=llm,
                             agent_config=agent_config)
