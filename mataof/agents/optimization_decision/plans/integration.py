"""计划与决策 Agent 的集成辅助（纯函数，确定性）。

职责边界：
- 计划由 Agent 内置生成器（generator）编译，成本模型（cost）选择；
- 本模块只把"选中的计划"翻译为决策输出的可追踪字段：
  计划决策条目、维度决策的显式对齐/批次取值、执行摘要、置信度；
- 不生成/改写 SQL，不改变计划的执行语义。

置信度口径（不是成功概率，而是"当前证据强度"）：
- baseline：安全动作基线 0.60；单条延迟来自历史/数据库状态 +0.10；
  有该计划历史证据按条数最多 +0.10；
- 非 baseline：0.55 起；预测节省比例最多 +0.15；历史证据最多 +0.15；
  查询分析置信度最多 +0.05；medium/high 风险分别 -0.05/-0.15；
- 单条基线仅来自固定先验时，置信度封顶 0.75（不把先验当实测）。
"""

from __future__ import annotations

from typing import Optional

from .cost import PlanChoice, PlanEvaluation

DEFAULT_FETCH_SIZE = 1024
PLAN_DIMENSION = "plan"


def chosen_evaluation(choice: Optional[PlanChoice]) -> Optional[PlanEvaluation]:
    """返回选中计划的评估结果（含预测/节省/历史证据）。"""
    if choice is None or not choice.chosen:
        return None
    pid = choice.chosen.get("plan_id")
    return next((e for e in choice.evaluations if e.plan_id == pid), None)


def plan_dimensions(plan: dict) -> dict:
    """计划隐含的维度取值（执行器实际使用的对齐方式与批次大小）。"""
    rewrite = plan.get("rewrite") or {}
    alignment = str(rewrite.get("alignment") or "default")
    if alignment != "align_by_device":
        alignment = "time_aligned"
    ep = plan.get("exec_params") or {}
    fetch_size = ep.get("fetch_size")
    if not isinstance(fetch_size, int) or isinstance(fetch_size, bool) or fetch_size <= 0:
        fetch_size = DEFAULT_FETCH_SIZE
    fetch = "fetch_large" if fetch_size > DEFAULT_FETCH_SIZE else "fetch_default"
    return {"alignment": alignment, "fetch_size": fetch, "fetch_size_value": fetch_size}


def plan_equivalent_sql(plan: dict) -> Optional[str]:
    """单条 SQL 计划返回该 SQL；多子查询计划返回 None（由 execution_plan 承载）。"""
    subqueries = plan.get("subqueries") or []
    merge = plan.get("merge") or {}
    if len(subqueries) == 1 and merge.get("mode") == "none":
        return subqueries[0]
    return None


def plan_summary(plan: dict) -> dict:
    """执行层参数摘要（写入 strategy_parameters.plan，便于追踪与知识沉淀）。"""
    ep = plan.get("exec_params") or {}
    merge = plan.get("merge") or {}
    partition = plan.get("partition") or {}
    return {
        "plan_id": plan.get("plan_id"),
        "family": plan.get("family"),
        "query_shape": plan.get("query_shape"),
        "parts": len(plan.get("subqueries") or []),
        "parallelism": ep.get("parallelism"),
        "fetch_size": ep.get("fetch_size"),
        "merge_mode": merge.get("mode"),
        "partition_key": partition.get("key"),
        "risk": plan.get("risk"),
        "equivalence": plan.get("equivalence"),
    }


def plan_confidence(choice: Optional[PlanChoice], ctx) -> float:
    """计划决策的置信度（证据强度，非成功概率；公式见模块 docstring）。"""
    ev = chosen_evaluation(choice)
    if ev is None:
        return 0.0
    if ev.family == "baseline":
        base = 0.60
        if choice.single_source != "prior":
            base += 0.10
        if ev.history_count:
            base += min(0.10, 0.03 * ev.history_count)
        return round(min(0.90, max(0.30, base)), 4)

    base = 0.55
    if ev.single_ms > 0:
        ratio = max(0.0, ev.saving_ms / ev.single_ms)
        base += min(0.15, ratio * 0.5)
    if ev.history_count:
        base += min(0.15, 0.05 * ev.history_count)
    if ctx is not None and ctx.analysis_confidence is not None:
        base += 0.05 * float(ctx.analysis_confidence)
    base -= {"medium": 0.05, "high": 0.15}.get(ev.risk, 0.0)
    cap = 0.75 if choice.single_source == "prior" else 0.90
    return round(min(cap, max(0.30, base)), 4)


def _source_label(source: str) -> str:
    return {
        "history_baseline": "相似历史 baseline 实测",
        "history_plan_typical": "同查询形状计划实测典型",
        "prior": "固定先验（非实测）",
    }.get(source, source)


def plan_reason(choice: Optional[PlanChoice], ctx) -> str:
    """可追踪的选择理由（引用预测值、证据来源与计划结构，不声称全局最优）。"""
    ev = chosen_evaluation(choice)
    if choice is None or not choice.chosen or ev is None:
        return "没有可执行的候选计划，保持确定性维度决策。"
    plan = choice.chosen
    pid = plan.get("plan_id")
    if choice.fallback or ev.family == "baseline":
        return (
            f"候选计划均未超过净收益/风险门槛（单条估计 {choice.single_ms:.1f}ms，"
            f"来源：{_source_label(choice.single_source)}），"
            f"选择安全 baseline {pid}；这是当前信息条件下的保守动作，不声称更优。"
        )
    merge = plan.get("merge") or {}
    return (
        f"{pid}（{ev.family}）预测 {ev.predicted_ms:.1f}ms，"
        f"单条估计 {choice.single_ms:.1f}ms（{_source_label(choice.single_source)}），"
        f"预测净收益 {ev.saving_ms:.1f}ms；风险 {ev.risk}；"
        f"编译为 {len(plan.get('subqueries') or [])} 条子查询，"
        f"按 {merge.get('mode')} 合并；当前信息条件下的选择，不声称全局最优。"
    )


def plan_decision_entry(choice: Optional[PlanChoice], ctx) -> Optional[dict]:
    """构造 decision["plan"] 条目（与维度决策条目同构 + 计划扩展字段）。"""
    ev = chosen_evaluation(choice)
    if choice is None or not choice.chosen or ev is None:
        return None
    plan = choice.chosen
    return {
        "strategy": plan.get("plan_id"),
        "reason": plan_reason(choice, ctx),
        "confidence": plan_confidence(choice, ctx),
        "equivalent_sql": plan_equivalent_sql(plan),
        "family": ev.family,
        "risk": ev.risk,
        "predicted_ms": round(ev.predicted_ms, 3),
        "single_ms": round(ev.single_ms, 3),
        "saving_ms": round(ev.saving_ms, 3),
        "fallback": bool(choice.fallback),
    }


def dimension_overrides(plan: dict, alignment_applicable: bool,
                        confidence: float) -> dict[str, dict]:
    """把计划隐含的维度取值映射为决策条目（只覆盖计划真正决定的维度）。

    - alignment：计划追加 ALIGN BY DEVICE → align_by_device（即使分析侧无法
      确认多设备，设备清单已由计划生成器展开）；否则仅在维度适用时写
      time_aligned；
    - fetch_size：恒覆盖为计划实际使用值。
    """
    dims = plan_dimensions(plan)
    pid = plan.get("plan_id")
    out: dict[str, dict] = {}
    if dims["alignment"] == "align_by_device" or alignment_applicable:
        out["alignment"] = {
            "strategy": dims["alignment"],
            "reason": (f"计划 {pid} 在 SQL 层使用 {'ALIGN BY DEVICE' if dims['alignment'] == 'align_by_device' else '默认时间对齐'}；"
                       f"以执行计划的 rewrite 规格为准。"),
            "confidence": confidence,
            "equivalent_sql": None,
        }
    out["fetch_size"] = {
        "strategy": dims["fetch_size"],
        "reason": f"计划 {pid} 使用 fetch_size={dims['fetch_size_value']}（执行参数）。",
        "confidence": confidence,
        "equivalent_sql": None,
    }
    return out
