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

from typing import Optional

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
)
from mataof.agents.optimization_decision.scoring import (
    DimensionResult,
    evaluate_dimension,
)
from mataof.agents.optimization_decision.confidence import (
    dimension_confidence,
    overall_confidence,
)


class OptimizationDecisionAgent:
    """优化决策智能体。

    用法：
        agent = OptimizationDecisionAgent()
        result = agent.decide({
            "query_id": "q1",
            "query_analysis": <Query Analysis Agent 的输出>,
            "database_state": {...},      # 可选
            "system_state": {...},        # 可选
            "historical_records": [...],  # 可选
            "candidate_strategies": {...},# 可选（缺失 → 目录全集）
            "baseline_strategy": {...},   # 可选（缺失 → 内置安全默认）
        })
    """

    name = "optimization_decision"
    description = "综合查询特征、数据库状态、系统状态与历史经验，在候选策略空间中"
    description += "选择当前查询的优化策略，并给出可追踪的依据与置信度。"

    def decide(self, decision_input: dict) -> dict:
        output = optimization_decision_output_template()

        ctx = normalize_input(decision_input)
        output["query_id"] = ctx.query_id
        if ctx.errors:
            output["decision_status"] = "invalid_input"
            output["fallback_strategy"] = ""
            return output

        # ---- 历史证据（相似度过滤 + 加权）----
        ev = build_history_evidence(
            ctx.historical_records, ctx.compact_features,
            ctx.database_state, ctx.system_state, ctx.reference_time,
        )

        # ---- 各维度评估与选择 ----
        results: dict[str, DimensionResult] = {}
        for dim in DIMENSIONS:
            results[dim] = evaluate_dimension(dim, ctx, ev)

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
                }
            elif r.is_fallback:
                output["decision"][dim] = {
                    "strategy": r.fallback_strategy or "",
                    "reason": _fallback_reason(dim, r, ctx),
                    "confidence": dim_conf[dim],
                }
            else:
                chosen = r.chosen
                output["decision"][dim] = {
                    "strategy": chosen.name,
                    "reason": _selection_reason(dim, r, ctx),
                    "confidence": dim_conf[dim],
                }

        # ---- selected_strategy 与 fallback_strategy ----
        parts, params = [], {}
        fallback_parts = []
        for dim in DIMENSIONS:
            r = results[dim]
            if not r.applicable:
                parts.append(f"{dim}={NOT_APPLICABLE}")
            elif r.is_fallback:
                parts.append(f"{dim}={r.fallback_strategy}")
                fallback_parts.append(f"{dim}={r.fallback_strategy}")
            else:
                parts.append(f"{dim}={r.chosen.name}")
        params = _strategy_parameters(results, ctx)
        output["selected_strategy"]["strategy_id"] = "|".join(parts)
        output["selected_strategy"]["strategy_parameters"] = params
        output["fallback_strategy"] = "|".join(fallback_parts)

        # ---- evidence ----
        output["evidence"] = _collect_evidence(ctx, ev, results)

        # ---- 整体置信度与状态 ----
        output["overall_confidence"] = overall_confidence(
            results, ctx, dim_conf, len(ev.used)
        )
        n_fallback = sum(1 for r in results.values() if r.applicable and r.is_fallback)
        n_applicable = sum(1 for r in results.values() if r.applicable)
        if n_fallback == 0:
            output["decision_status"] = "success"
        elif n_fallback >= n_applicable:
            output["decision_status"] = "fallback"
        else:
            output["decision_status"] = "partial_fallback"

        # ---- 可追踪说明（扩展字段）----
        notes = list(ctx.notes)
        for dim in DIMENSIONS:
            notes.extend(results[dim].exclusion_notes)
        if ev.invalid:
            notes.append(f"{ev.invalid} 条历史记录结构不合法，未参与决策")
        if ev.below_threshold:
            notes.append(
                f"{ev.below_threshold} 条历史记录与当前上下文相似度低于阈值 "
                f"（{SIMILARITY_THRESHOLD}），未参与决策"
            )
        output["notes"] = notes
        return output


# ---------------------------------------------------------------------------
# reason 生成（可追踪：引用具体证据，不做全局最优声明）
# ---------------------------------------------------------------------------

def _time_desc(ctx: DecisionContext) -> str:
    t = ctx.features.get("time") or {}
    if t.get("has_time_filter"):
        s, e = t.get("start_time"), t.get("end_time")
        if s is not None and e is not None:
            return (f"时间边界已知（[{s}, {e}]，跨度 {t.get('time_span')} ms，"
                    f"{t.get('range_level')}）")
        return f"存在时间过滤（形式 {t.get('time_condition_form')}），起止未知"
    return "无时间过滤（窗口范围来自 GROUP BY）"


def _fallback_reason(dim: str, r: DimensionResult, ctx: DecisionContext) -> str:
    reasons = "；".join(e.excluded_reason for e in r.evaluations if e.excluded_reason)
    return (f"候选策略均不满足安全选择条件（{reasons or '无可用候选'}），"
            f"证据不足，回退安全 baseline {r.fallback_strategy}。")


def _selection_reason(dim: str, r: DimensionResult, ctx: DecisionContext) -> str:
    chosen = r.chosen
    parts: list[str] = []
    if dim == "time_pruning":
        parts.append(_time_desc(ctx))
    elif dim == "filter_order":
        parts.append(f"非时间过滤条件 {ctx.non_time_filter_count} 个")
    elif dim == "aggregation_placement":
        agg = ctx.features.get("aggregation") or {}
        names = sorted({f.get("function") for f in agg.get("aggregation_functions") or []})
        parts.append(f"聚合函数：{', '.join(names)}")

    for label in chosen.factor_labels:
        parts.append(label)

    excluded = [e.name for e in r.evaluations if e.excluded_reason]
    if excluded:
        parts.append("排除：" + "、".join(excluded))

    parts.append(f"在当前信息条件下选择 {chosen.name}（score {chosen.score}）。")
    return "；".join(parts)


def _strategy_parameters(results: dict, ctx: DecisionContext) -> dict:
    """传递给执行层的参数：只包含事实性取值。"""
    params: dict = {}
    r = results["time_pruning"]
    if r.applicable and not r.is_fallback and r.chosen.name in (
        "partition_pruning", "chunk_level_filtering",
    ):
        t = ctx.features.get("time") or {}
        params["time_pruning"] = {
            "start_time": t.get("start_time"),
            "end_time": t.get("end_time"),
        }
    return params


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
        for dim in ("time_pruning", "filter_order", "aggregation_placement"):
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


def decide(decision_input: dict) -> dict:
    """Optimization Decision Agent 的便捷入口。"""
    return default_agent.decide(decision_input)
