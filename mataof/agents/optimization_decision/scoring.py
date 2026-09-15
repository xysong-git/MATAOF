"""候选策略评分、风险门控与维度内选择。

评分模型（确定性、可追踪；所有加分只来自已提供的输入数据）：
    score(c) = base(c) + context_bonus(c) + history_bonus(c) + system_bonus(c)

- base          ：策略在目录中的基础分（结构性先验，见 catalog.py 说明）；
- context_bonus ：由数据库状态/统计信息等上下文数据支撑的加分；无数据 → 无加分；
- history_bonus ：由相似历史记录（similarity.py 产出）支撑的加分；无证据 → 无加分，
                  且当其他策略有历史证据时，无证据策略会被轻微扣分；
- system_bonus  ：由系统负载状态支撑的调整；system_state 未提供 → 不调整。

风险门控（安全机制）：
- low   ：需求满足即可选；
- medium：需求满足 且（上下文加分 > 0 或 历史证据 ≥ 1 条）才可选；
- high  ：需求满足 且 历史证据 ≥ 2 条才可选。
门控未通过的策略被排除并记录原因；某维度全部被排除 → 回退该维度 baseline。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from mataof.agents.optimization_decision.catalog import (
    NOT_APPLICABLE,
    STRATEGY_CATALOG,
    check_requirement,
)
from mataof.agents.optimization_decision.context import DecisionContext
from mataof.agents.optimization_decision.similarity import HistoryEvidence

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

BASE_SCORES = {
    "full_scan": 0.30,
    "partition_pruning": 0.55,
    "chunk_level_filtering": 0.70,
    "time_first": 0.60,
    "device_tag_first": 0.35,
    "final_level_aggregation": 0.50,
    "intermediate_level_aggregation": 0.55,
    "scan_level_aggregation": 0.60,
}

# 系统负载调整（load_level=high 时；low/normal/unknown 不调整）
_SYSTEM_BONUS_HIGH_LOAD = {
    "full_scan": -0.05,
    "partition_pruning": 0.05,
    "chunk_level_filtering": 0.05,
    "time_first": 0.05,
    "scan_level_aggregation": 0.05,
    "final_level_aggregation": -0.05,
}


@dataclass
class CandidateEvaluation:
    name: str
    dimension: str
    risk: str
    score: float
    base: float
    context_bonus: float
    history_bonus: float
    system_bonus: float
    factor_labels: list = field(default_factory=list)     # 用于 reason 的可追踪说明
    requirements_met: bool = True
    gate_passed: bool = True
    excluded_reason: Optional[str] = None


@dataclass
class DimensionResult:
    dimension: str
    applicable: bool
    not_applicable_reason: Optional[str] = None
    evaluations: list = field(default_factory=list)       # 全部候选评估（含被排除的）
    selectable: list = field(default_factory=list)        # 门控通过的可选集合
    chosen: Optional[CandidateEvaluation] = None
    is_fallback: bool = False
    fallback_strategy: Optional[str] = None
    exclusion_notes: list = field(default_factory=list)   # 排除说明（输出 notes）


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _selectivity_map(ctx: DecisionContext) -> dict:
    """选择率数据：分析输出的（已验证）选择率 ∪ 决策输入的 statistics.selectivity（校验后）。"""
    out: dict = {}
    sel = (ctx.features.get("filter") or {}).get("selectivity") or {}
    for k, v in sel.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1:
            out[k] = float(v)
    db_sel = ((ctx.database_state or {}).get("statistics") or {}).get("selectivity") or {}
    for k, v in db_sel.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1:
            out[k] = float(v)
    return out


def _avg_selectivity_by_type(ctx: DecisionContext) -> dict:
    """按条件类型聚合的平均选择率（只有数据库提供了选择率的条件参与）。"""
    sel_map = _selectivity_map(ctx)
    out: dict[str, list[float]] = {}
    for cond in (ctx.features.get("filter") or {}).get("conditions") or []:
        ctype, col = cond.get("type"), cond.get("column")
        if not ctype or col is None or col not in sel_map:
            continue
        out.setdefault(ctype, []).append(sel_map[col])
    return {t: sum(v) / len(v) for t, v in out.items()}


def _partition_coverage(ctx: DecisionContext) -> Optional[float]:
    pi = (ctx.database_state or {}).get("partition_info")
    if not isinstance(pi, dict):
        return None
    total, covered = pi.get("total_partitions"), pi.get("covered_partitions")
    if (isinstance(total, (int, float)) and not isinstance(total, bool)
            and isinstance(covered, (int, float)) and not isinstance(covered, bool)
            and total > 0 and 0 <= covered <= total):
        return covered / total
    return None


def _chunk_coverage(ctx: DecisionContext) -> Optional[float]:
    cl = ((ctx.database_state or {}).get("physical_organization") or {}).get("chunk_level")
    if not isinstance(cl, dict):
        return None
    total, covered = cl.get("total_chunks"), cl.get("covered_chunks")
    if (isinstance(total, (int, float)) and not isinstance(total, bool)
            and isinstance(covered, (int, float)) and not isinstance(covered, bool)
            and total > 0 and 0 <= covered <= total):
        return covered / total
    return None


def _history_bonus(ev: HistoryEvidence, dim: str, name: str, labels: list) -> float:
    count = ev.count_for(dim, name)
    if count == 0:
        total = ev.total_weight(dim)
        if total > 0:
            labels.append(f"历史记录中存在其他策略的证据（该策略无记录，-{0.10 * total:.2f}）")
            return -0.10 * total
        return 0.0
    lat = ev.weighted_latency(dim, name)
    best = ev.best_latency(dim)
    w = ev.weight_for(dim, name)
    factor = 1.0
    if lat is not None and best is not None and lat > 0:
        factor = min(1.0, best / lat)
    bonus = round(0.25 * w * factor, 4)
    labels.append(
        f"历史证据 {count} 条（加权 {w:.2f}，加权平均延迟 {lat if lat is None else round(lat, 1)} ms，"
        f"相对最优比例 {factor:.2f}，+{bonus:.2f}）"
    )
    return bonus


def _system_bonus(ctx: DecisionContext, name: str, labels: list) -> float:
    if ctx.load_level == "high" and name in _SYSTEM_BONUS_HIGH_LOAD:
        b = _SYSTEM_BONUS_HIGH_LOAD[name]
        labels.append(f"系统高负载（{b:+.2f}）")
        return b
    return 0.0


def _range_narrow(ctx: DecisionContext) -> bool:
    return (ctx.features.get("time") or {}).get("range_level") == "narrow"


def _scan_small(ctx: DecisionContext) -> bool:
    return (ctx.features.get("scan") or {}).get("scan_level") == "narrow"


def _context_bonus(ctx: DecisionContext, dim: str, name: str, labels: list) -> float:
    """各策略的上下文加分（只使用已提供的数据）。"""
    bonus = 0.0

    if dim == "time_pruning":
        if name == "partition_pruning":
            if _range_narrow(ctx):
                bonus += 0.10
                labels.append("时间范围 narrow（+0.10）")
            cov = _partition_coverage(ctx)
            if cov is not None and cov <= 0.25:
                bonus += 0.10
                labels.append(f"分区覆盖 {cov:.1%}（≤25%，+0.10）")
        elif name == "chunk_level_filtering":
            if _range_narrow(ctx):
                bonus += 0.10
                labels.append("时间范围 narrow（+0.10）")
            cov = _chunk_coverage(ctx)
            if cov is not None and cov <= 0.10:
                bonus += 0.10
                labels.append(f"chunk 覆盖 {cov:.1%}（≤10%，+0.10）")

    elif dim == "filter_order":
        if name == "time_first":
            if _range_narrow(ctx):
                bonus += 0.10
                labels.append("时间范围 narrow（+0.10）")
            ts = _avg_selectivity_by_type(ctx).get("time")
            if ts is not None:
                if ts <= 0.1:
                    bonus += 0.10
                    labels.append(f"时间条件平均选择率 {ts:.3f}（≤0.1，+0.10）")
                elif ts >= 0.5:
                    bonus -= 0.10
                    labels.append(f"时间条件平均选择率 {ts:.3f}（≥0.5，-0.10）")
        elif name == "device_tag_first":
            sel_by_type = _avg_selectivity_by_type(ctx)
            nt = next((v for t, v in sel_by_type.items() if t != "time"), None)
            ts = sel_by_type.get("time")
            if nt is not None and nt <= 0.1 and (ts is None or nt < ts):
                bonus += 0.40
                labels.append(f"非时间条件平均选择率 {nt:.3f}（≤0.1 且优于时间条件，+0.40）")
            dev = (ctx.features.get("device") or {}).get("device_count")
            scale = (ctx.database_state or {}).get("device_scale")
            if (isinstance(dev, (int, float)) and not isinstance(dev, bool)
                    and isinstance(scale, (int, float)) and not isinstance(scale, bool)
                    and scale > 0 and dev / scale <= 0.1):
                bonus += 0.15
                labels.append(f"设备过滤覆盖 {dev}/{scale}（≤10%，+0.15）")

    elif dim == "aggregation_placement":
        if name == "scan_level_aggregation":
            if _range_narrow(ctx):
                bonus += 0.15
                labels.append("时间范围 narrow（+0.15）")
            elif _scan_small(ctx):
                bonus += 0.15
                labels.append("数据库扫描估计为小范围（scan_level=narrow，+0.15）")
            if (ctx.features.get("aggregation") or {}).get("has_window"):
                bonus += 0.10
                labels.append("窗口聚合（+0.10）")
        elif name == "intermediate_level_aggregation":
            if (ctx.features.get("aggregation") or {}).get("has_group_by"):
                bonus += 0.10
                labels.append("存在 GROUP BY（+0.10）")
            if (ctx.features.get("scan") or {}).get("possible_large_intermediate_result") is True:
                bonus += 0.10
                labels.append("可能产生较大中间结果（+0.10）")

    return bonus


# ---------------------------------------------------------------------------
# 评估与选择
# ---------------------------------------------------------------------------

def _gate_passed(eval_: CandidateEvaluation, ctx: DecisionContext,
                 ev: HistoryEvidence) -> bool:
    if eval_.risk == "low":
        return True
    if not eval_.requirements_met:
        return False
    if eval_.risk == "medium":
        return eval_.context_bonus > 0 or ev.count_for(eval_.dimension, eval_.name) >= 1
    # high
    return ev.count_for(eval_.dimension, eval_.name) >= 2


def evaluate_dimension(dim: str, ctx: DecisionContext,
                       ev: HistoryEvidence) -> DimensionResult:
    """评估一个维度：候选过滤 → 评分 → 门控 → 选择/回退。"""
    result = DimensionResult(dimension=dim, applicable=False)

    # ---- 适用性 ----
    if dim == "time_pruning":
        if not ctx.has_time_filter and not ctx.time_window_group_by:
            result.not_applicable_reason = "查询无时间过滤条件，也无时间窗口分组"
            return result
    elif dim == "filter_order":
        if ctx.non_time_filter_count < 2:
            result.not_applicable_reason = (
                f"非时间过滤条件数为 {ctx.non_time_filter_count}（<2），过滤顺序无意义"
            )
            return result
    elif dim == "aggregation_placement":
        if not (ctx.features.get("aggregation") or {}).get("has_aggregation"):
            result.not_applicable_reason = "查询不含聚合"
            return result
    result.applicable = True

    catalog = STRATEGY_CATALOG[dim]
    candidates = [c for c in ctx.candidates.get(dim, []) if c in catalog]

    for name in candidates:
        meta = catalog[name]
        labels: list = []

        # 需求评估
        unmet = [r for r in meta["requirements"] if not check_requirement(r, _requirement_context(ctx))]
        evl = CandidateEvaluation(name=name, dimension=dim, risk=meta["risk"],
                                  score=0.0, base=BASE_SCORES[name],
                                  context_bonus=0.0, history_bonus=0.0,
                                  system_bonus=0.0, factor_labels=labels)
        if unmet:
            evl.requirements_met = False
            evl.excluded_reason = "需求不满足：" + "；".join(
                f"缺少/不满足 {r['path']}（{r['op']}）" for r in unmet
            )
            result.evaluations.append(evl)
            result.exclusion_notes.append(f"排除 {name}：{evl.excluded_reason}")
            continue

        evl.context_bonus = _context_bonus(ctx, dim, name, labels)
        evl.history_bonus = _history_bonus(ev, dim, name, labels)
        evl.system_bonus = _system_bonus(ctx, name, labels)
        evl.score = round(evl.base + evl.context_bonus + evl.history_bonus + evl.system_bonus, 4)
        evl.gate_passed = _gate_passed(evl, ctx, ev)
        if not evl.gate_passed:
            evl.excluded_reason = "风险门控未通过（缺少足够的支撑信号/历史证据）"
            result.exclusion_notes.append(f"排除 {name}：{evl.excluded_reason}")
        result.evaluations.append(evl)

    # 选择：门控通过者按 (score 降序, 风险升序, 目录顺序) 排序
    result.selectable = [e for e in result.evaluations if e.requirements_met and e.gate_passed]
    result.selectable.sort(
        key=lambda e: (-e.score, RISK_ORDER[e.risk], list(catalog.keys()).index(e.name))
    )

    if result.selectable:
        result.chosen = result.selectable[0]
        return result

    # 全部被排除 → 回退 baseline（安全机制）
    result.is_fallback = True
    result.fallback_strategy = ctx.baseline.get(dim)
    return result


def _requirement_context(ctx: DecisionContext) -> dict:
    """把 DecisionContext 展开为 requirement 路径可寻址的字典。

    filter.non_time_filter_count 为派生字段（分析输出中无此键），在此补入。
    """
    features = dict(ctx.features)
    filter_feat = dict(features.get("filter") or {})
    filter_feat["non_time_filter_count"] = ctx.non_time_filter_count
    features["filter"] = filter_feat
    return {
        "query_features": features,
        "database_state": ctx.database_state,
        "system_state": ctx.system_state,
    }
