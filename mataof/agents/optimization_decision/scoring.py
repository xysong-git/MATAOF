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
    alignment_applies,
    check_requirement,
)
from mataof.agents.optimization_decision.context import DecisionContext
from mataof.agents.optimization_decision.similarity import HistoryEvidence

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

BASE_SCORES = {
    # alignment：默认对齐为数据库默认且实测占优（宽设备扫描 15~25×），给结构性先验
    "time_aligned": 0.55,
    "align_by_device": 0.45,
    # fetch_size：两者等基分，由上下文加分与执行反馈（评价表）决定
    "fetch_default": 0.50,
    "fetch_large": 0.50,
}

# 系统负载调整（load_level=high 时；low/normal/unknown 不调整）
# 新策略空间暂无实测的负载交互 → 空（诚实：不编造调整依据）
_SYSTEM_BONUS_HIGH_LOAD = {}


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
    llm_bonus: float = 0.0                                # LLM 偏好加分（不参与门控）
    eval_prior: float = 0.0                               # 策略评价表先验（D1，不参与门控）
    factor_labels: list = field(default_factory=list)     # 用于 reason 的可追踪说明
    equivalent_sql: Optional[str] = None                  # 候选生成器给出的等价 SQL
    parameters: Optional[dict] = None                     # 候选提供的执行级参数
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


def _context_bonus(ctx: DecisionContext, dim: str, name: str, labels: list) -> float:
    """各策略的上下文加分（只使用已提供的数据；实测依据见各策略注释）。"""
    bonus = 0.0
    dev = (ctx.features.get("device") or {}).get("device_count")
    dev_ok = isinstance(dev, (int, float)) and not isinstance(dev, bool)

    if dim == "alignment":
        if name == "time_aligned" and dev_ok and dev >= 10:
            # 实测：宽设备扫描下默认对齐比 ALIGN BY DEVICE 快 15~25×（结果行数爆炸）
            bonus += 0.10
            labels.append(f"设备数 {int(dev)}（≥10，实测默认对齐显著占优，+0.10）")
        elif name == "align_by_device" and dev_ok and dev <= 5:
            # 实测：少量设备下两者接近（~12%），按设备对齐略优
            bonus += 0.05
            labels.append(f"设备数 {int(dev)}（≤5，实测按设备对齐略优，+0.05）")

    elif dim == "fetch_size":
        if name == "fetch_large":
            # 实测：大结果批次减少分页往返，~15-20% 收益（大小结果集一致方向）
            bonus += 0.05
            labels.append("实测大批次 ~15-20% 收益（+0.05）")

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


# D1：策略评价表先验（开题报告 2.2.3：依据执行反馈动态调整策略评价权重）
# 权重是反馈的确定性函数：success_ratio → [-strength, +strength]；
# 样本不足 min_count 时不产生先验（证据不足不虚增）；不参与风险门控。
DEFAULT_EVAL_PRIOR_STRENGTH = 0.10
DEFAULT_EVAL_MIN_COUNT = 3


def _evaluation_prior(ctx: DecisionContext, evaluations: Optional[dict],
                      dim: str, name: str, labels: list,
                      strength: float, min_count: int) -> float:
    if not evaluations:
        return 0.0
    key = f"{ctx.query_type or 'unknown'}|{dim}|{name}"
    e = evaluations.get(key)
    if not isinstance(e, dict):
        return 0.0
    # 成功率分母只含方向明确的执行（improved + degraded + failed）；
    # unchanged 是中性结果，不参与分母。旧条目依次回退 evaluated_count/count。
    decisive = e.get("decisive_count")
    if not isinstance(decisive, int) or isinstance(decisive, bool):
        decisive = e.get("evaluated_count")
    if not isinstance(decisive, int) or isinstance(decisive, bool):
        decisive = e.get("count", 0)
    if not isinstance(decisive, int) or decisive < min_count:
        return 0.0
    ratio = e.get("success_ratio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return 0.0
    ratio = float(ratio)
    prior = round(strength * (ratio - 0.5) * 2.0, 4)
    labels.append(
        f"策略评价表先验（同类上下文 {decisive} 次方向明确，成功率 {ratio}，{prior:+.2f}）"
    )
    return prior


def evaluate_dimension(dim: str, ctx: DecisionContext,
                       ev: HistoryEvidence,
                       llm_preferences: Optional[dict] = None,
                       evaluations: Optional[dict] = None,
                       eval_prior_strength: float = DEFAULT_EVAL_PRIOR_STRENGTH,
                       eval_min_count: int = DEFAULT_EVAL_MIN_COUNT) -> DimensionResult:
    """评估一个维度：候选过滤 → 评分 → 门控 → 选择/回退。"""
    result = DimensionResult(dimension=dim, applicable=False)

    # ---- 适用性 ----
    if dim == "alignment":
        if not alignment_applies(ctx.features):
            result.not_applicable_reason = "单设备查询（对齐方式无差异）"
            return result
    elif dim == "fetch_size":
        pass   # 恒适用：结果批次大小对所有查询都成立
    result.applicable = True

    catalog = STRATEGY_CATALOG[dim]
    candidates = [c for c in ctx.candidates.get(dim, []) if c["name"] in catalog]

    for cand in candidates:
        name = cand["name"]
        meta = catalog[name]
        labels: list = []

        # 需求评估
        unmet = [r for r in meta["requirements"] if not check_requirement(r, _requirement_context(ctx))]
        evl = CandidateEvaluation(name=name, dimension=dim, risk=meta["risk"],
                                  score=0.0, base=BASE_SCORES[name],
                                  context_bonus=0.0, history_bonus=0.0,
                                  system_bonus=0.0, factor_labels=labels,
                                  equivalent_sql=cand.get("equivalent_sql"),
                                  parameters=cand.get("parameters"))
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
        # 策略评价表先验（D1）：反馈的确定性函数，有界，不参与风险门控
        evl.eval_prior = _evaluation_prior(ctx, evaluations, dim, name, labels,
                                           eval_prior_strength, eval_min_count)
        # LLM 偏好加分（实验性）：只影响总分，不参与风险门控——LLM 不能解锁策略
        prefs = (llm_preferences or {}).get(dim) or []
        if prefs and name == prefs[0]:
            evl.llm_bonus = 0.05
            labels.append("LLM 偏好加分（+0.05，实验性，不参与门控）")
        evl.score = round(evl.base + evl.context_bonus + evl.history_bonus
                          + evl.system_bonus + evl.llm_bonus + evl.eval_prior, 4)
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
