"""计划成本/收益模型与选择规则（确定性、可解释、无编造统计）。

目标：在**生成器已经保证可执行**的候选计划中，预测每个计划的执行耗时，
按"相对单条原查询的净收益"选择；证据不足时退回 baseline。

预测口径（全部来自输入或固定先验，不虚构测量值）：

1. 单条基线耗时 single_ms
   - 优先：相似历史中 baseline 计划的加权平均延迟，**但仅当样本充分**
     （≥3 次且 ≥ 同查询形状计划证据的 10%）。baseline 是安全动作、
     选择性执行，样本可能稀少且偏冷启动，不能代表查询形状常态；
   - baseline 样本不充分或无 baseline：同查询形状下其他计划的加权实测
     典型值，至少 3 次执行（计划族语义等价；来源标注
     history_plan_typical）；
   - 其次：调用方显式提供的 database_state 延迟字段；
   - 否则：固定先验 DEFAULT_SINGLE_MS（明确标注 prior，不声称实测）。
2. 计划预测延迟
   - baseline/exec_mode/sql_rewrite：single / prior_speedup（先验来自固定
     策略目录与实测方向，见 generator 的计划 cost_prior）；
   - partition/partial_aggregation：
         formula = 1 + eff*(parallel-1)（并行加速）
         overhead = (每段开销*段数 + 合并开销) / single
         formula_speedup = formula / (1 + overhead)
         speedup = min(prior_speedup, formula_speedup)
     经验先验是**上限**：理论并行公式只能把预期调低，不能凭空超过实测先验。
     这一条在 q12 上复现了实测的 device_split_4=1.40×、time_split_8=0.75×。
3. 历史修正：相似历史中该计划的加权延迟按证据量混合（最多 60%）。
4. 风险/反馈修正：medium/high 风险与评价表低成功率产生有界惩罚；评价表
   高成功率（≥0.90，且样本数达标）给出 0.5ms 有界奖励；合并开销含列宽项
   （结构事实）并在高负载时按固定系数放大。
5. 选择：净收益 = single - 预测延迟 - 惩罚；必须超过
   max(MIN_SAVING_MS, MIN_SAVING_RATIO*single)；高风险需 ≥2 条历史证据；
   baseline 永远可选，作为无收益/证据不足时的安全动作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..context import DecisionContext
from ..similarity import (HistoryEvidence, MIN_BASELINE_SAMPLES,
                          baseline_sample_adequate)

DEFAULT_SINGLE_MS = 30.0
# 无历史基线时的结构化先验（明确是模型先验，不是实测统计）：
# 点查/短查询与长区间宽扫描的客户端耗时量级不同，统一用 30ms 会高估
# 点查收益、诱导无意义的分区。仅用于"是否值得尝试"的保守判断。
PRIOR_POINT_MS = 8.0
PRIOR_SHORT_MS = 12.0
PRIOR_LARGE_SCAN_MS = 45.0
PRIOR_WIDE_DEVICE_SCAN_MS = 50.0
SHORT_QUERY_SPAN_MS = 60_000
MIN_SAVING_MS = 1.0
MIN_SAVING_RATIO = 0.03
MEDIUM_EXTRA_RATIO = 0.15
HIGH_EXTRA_RATIO = 0.30
HIGH_LOAD_EXTRA_MS = 1.0
# 合并开销的结构项：DataFrame 合并成本随列数增长（横向拼接每列都要对齐
# 索引），先验只给有界增量，不虚构行数（行数未知）。
MERGE_COLUMN_MS = 0.05
MAX_MERGE_COLUMN_MS = 3.0
# 高负载下客户端并行/合并受 CPU 竞争影响：开销按此系数放大（有界）。
HIGH_LOAD_OVERHEAD_FACTOR = 1.25
# 评价表反馈：低成功率惩罚之外，高成功率给出有界奖励（需满足最小样本数）。
EVAL_SUCCESS_RATIO = 0.90
EVAL_SUCCESS_BONUS_MS = 0.5
RISK_PENALTY_MS = {"low": 0.0, "medium": 0.5, "high": 2.0}
PLAN_DIMENSION = "plan"
DEFAULT_PRIOR_SPEEDUP = 0.90
BASELINE_PLAN_ID = "baseline_single_df"

_FAMILY_ORDER = {
    "baseline": 0, "exec_mode": 1, "sql_rewrite": 2,
    "partition": 3, "partial_aggregation": 4,
}
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass
class PlanEvaluation:
    plan_id: str
    family: str
    risk: str
    predicted_ms: float
    single_ms: float
    saving_ms: float
    required_saving_ms: float
    risk_penalty_ms: float
    eval_penalty_ms: float
    history_count: int
    history_latency_ms: Optional[float]
    eval_count: int
    eval_success_ratio: Optional[float]
    selectable: bool
    excluded_reason: Optional[str] = None
    factors: list = field(default_factory=list)

    @property
    def utility_ms(self) -> float:
        return round(self.saving_ms, 3)

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "family": self.family,
            "risk": self.risk,
            "predicted_ms": round(self.predicted_ms, 3),
            "single_ms": round(self.single_ms, 3),
            "saving_ms": round(self.saving_ms, 3),
            "required_saving_ms": round(self.required_saving_ms, 3),
            "risk_penalty_ms": round(self.risk_penalty_ms, 3),
            "eval_penalty_ms": round(self.eval_penalty_ms, 3),
            "history_count": self.history_count,
            "history_latency_ms": (None if self.history_latency_ms is None
                                   else round(self.history_latency_ms, 3)),
            "eval_count": self.eval_count,
            "eval_success_ratio": self.eval_success_ratio,
            "selectable": self.selectable,
            "excluded_reason": self.excluded_reason,
            "factors": list(self.factors),
        }


@dataclass
class PlanChoice:
    chosen: Optional[dict] = None
    evaluations: list = field(default_factory=list)   # [PlanEvaluation]
    baseline_plan_id: Optional[str] = None
    single_ms: float = DEFAULT_SINGLE_MS
    single_source: str = "prior"
    single_count: int = 0
    fallback: bool = True
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "chosen_plan_id": self.chosen.get("plan_id") if self.chosen else None,
            "baseline_plan_id": self.baseline_plan_id,
            "single_ms": round(self.single_ms, 3),
            "single_source": self.single_source,
            "single_count": self.single_count,
            "fallback": self.fallback,
            "notes": list(self.notes),
            "evaluations": [e.to_dict() for e in self.evaluations],
        }


# ---------------------------------------------------------------------------
# 基线估计
# ---------------------------------------------------------------------------

_DB_LATENCY_KEYS = ("baseline_latency_ms", "avg_query_latency_ms", "avg_latency_ms")


def estimate_single_ms(ctx: DecisionContext, evidence: Optional[HistoryEvidence],
                       baseline_plan_id: str = BASELINE_PLAN_ID) -> tuple[float, str, Optional[int]]:
    """估计单条原查询耗时。返回 (ms, 来源, 历史样本数)。

    baseline 计划是原查询的直接实测，但它是"安全动作"、只在没有更优计划时
    执行，样本可能稀少且偏冷启动（例如 q1：1 次 1542ms，而常态约 12ms）。
    因此仅在样本充分时采用 baseline；否则改用同查询形状下其他计划的加权
    实测典型值（至少 3 次执行，单次结果不当规律）——计划族语义等价、
    实测方向以护栏/中性为主，该典型值比稀疏冷启动 baseline 更能代表当前
    查询成本。绝不把稀疏 baseline 当常态：那会虚增预测收益、诱导激进计划。
    """
    if evidence is not None:
        b_lat = evidence.weighted_latency(PLAN_DIMENSION, baseline_plan_id)
        b_count = evidence.count_for(PLAN_DIMENSION, baseline_plan_id)
        total = evidence.total_count(PLAN_DIMENSION)
        if (b_lat is not None and b_lat > 0 and b_count > 0
                and baseline_sample_adequate(b_count, total)):
            return round(float(b_lat), 3), "history_baseline", int(b_count)
        alt_lat = evidence.weighted_latency_all(PLAN_DIMENSION,
                                                exclude=baseline_plan_id)
        alt_count = max(0, total - b_count)
        # 与 baseline 同样要求最小样本：单次执行不当规律。样本不足时
        # 退回数据库状态/结构化先验（明确标注非实测）。
        if (alt_lat is not None and alt_lat > 0
                and alt_count >= MIN_BASELINE_SAMPLES):
            return (round(float(alt_lat), 3), "history_plan_typical",
                    int(alt_count))
    for key in _DB_LATENCY_KEYS:
        v = (ctx.database_state or {}).get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return round(float(v), 3), f"database_state.{key}", 0

    # 结构化先验：只用查询分析给出的形状/跨度/设备数，不虚构测量值。
    t = ctx.features.get("time") or {}
    agg = ctx.features.get("aggregation") or {}
    scan = ctx.features.get("scan") or {}
    dev = (ctx.features.get("device") or {}).get("device_count")
    span = t.get("time_span")
    if not (isinstance(span, (int, float)) and not isinstance(span, bool)):
        window = ((agg.get("group_by_details") or {}).get("window") or {})
        w0, w1 = window.get("start_time"), window.get("end_time")
        span = (w1 - w0) if isinstance(w0, (int, float)) and isinstance(w1, (int, float)) else None
    if ctx.query_type == "point_query" or span == 0:
        return PRIOR_POINT_MS, "prior_point_query", 0
    if isinstance(span, (int, float)) and not isinstance(span, bool) \
            and span < SHORT_QUERY_SPAN_MS:
        return PRIOR_SHORT_MS, "prior_short_query", 0
    if scan.get("possible_large_scan") is True:
        return PRIOR_LARGE_SCAN_MS, "prior_large_scan", 0
    if (isinstance(span, (int, float)) and not isinstance(span, bool)
            and span >= SHORT_QUERY_SPAN_MS
            and isinstance(dev, (int, float)) and not isinstance(dev, bool)
            and dev >= 10):
        return PRIOR_WIDE_DEVICE_SCAN_MS, "prior_wide_device_scan", 0
    return DEFAULT_SINGLE_MS, "prior", 0


# ---------------------------------------------------------------------------
# 单计划评估
# ---------------------------------------------------------------------------

def _cost_prior(plan: dict) -> dict:
    cp = plan.get("cost_prior")
    return cp if isinstance(cp, dict) else {}


def _num(value, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _formula_speedup(plan: dict, single_ms: float, ctx: DecisionContext,
                     factors: list) -> float:
    """并行执行的理论加速比（含每段/合并开销；不包含经验上限）。"""
    subqueries = plan.get("subqueries") or []
    parts = len(subqueries)
    ep = plan.get("exec_params") or {}
    parallel = ep.get("parallelism") if isinstance(ep.get("parallelism"), int) else 1
    if parts <= 1 or parallel <= 1 or single_ms <= 0:
        return 1.0
    cp = _cost_prior(plan)
    eff = min(0.9, max(0.0, _num(cp.get("parallel_efficiency"), 0.30)))
    load_factor = {"high": 0.5, "low": 1.05}.get(ctx.load_level, 1.0)
    per_part = max(0.0, _num(cp.get("per_part_overhead_ms"), 1.5))
    merge_ms = max(0.0, _num(cp.get("merge_overhead_ms"), 1.0))
    # 合并列宽：列契约是计划结构事实（不是统计估计）
    merge_spec = plan.get("merge") or {}
    ncols = len(merge_spec.get("expected_columns") or [])
    if not ncols and isinstance(merge_spec.get("expected_group_columns"), list):
        ncols = sum(len(g or []) for g in merge_spec["expected_group_columns"])
    column_ms = min(MAX_MERGE_COLUMN_MS, MERGE_COLUMN_MS * ncols)
    merge_ms += column_ms
    overhead_factor = HIGH_LOAD_OVERHEAD_FACTOR if ctx.load_level == "high" else 1.0
    parallel_gain = 1.0 + eff * load_factor * (parallel - 1)
    overhead = (per_part * parts + merge_ms) / single_ms * overhead_factor
    speedup = parallel_gain / (1.0 + overhead)
    factors.append(
        f"理论并行：{parts} 段/并行度 {parallel}（效率 {eff:.2f}，负载系数 {load_factor:.2f}），"
        f"开销 {per_part * parts + merge_ms:.1f}ms"
        + (f"（含 {ncols} 列合并 +{column_ms:.1f}ms）" if column_ms else "")
        + (f"，高负载放大 {HIGH_LOAD_OVERHEAD_FACTOR:.2f}×" if overhead_factor != 1.0 else "")
        + f" → 加速 {speedup:.2f}×"
    )
    return max(0.05, speedup)


def _prior_speedup(plan: dict) -> float:
    return max(0.05, _num(_cost_prior(plan).get("prior_speedup"), DEFAULT_PRIOR_SPEEDUP))


def _history_adjust(plan_id: str, predicted_ms: float,
                    evidence: Optional[HistoryEvidence], factors: list
                    ) -> tuple[float, int, Optional[float]]:
    if evidence is None:
        return predicted_ms, 0, None
    count = evidence.count_for(PLAN_DIMENSION, plan_id)
    lat = evidence.weighted_latency(PLAN_DIMENSION, plan_id)
    if count <= 0 or lat is None or lat <= 0:
        if evidence.total_weight(PLAN_DIMENSION) > 0:
            factors.append("历史中无该计划证据（不调整预测，仅记录）")
        return predicted_ms, 0, None
    if plan_id == BASELINE_PLAN_ID:
        total = evidence.total_count(PLAN_DIMENSION)
        if not baseline_sample_adequate(count, total):
            # 稀疏/冷启动 baseline 不能代表常态：保留证据条数供追踪，
            # 但不让它把 baseline 预测拉向一次冷启动观测值。
            factors.append(
                f"baseline 历史仅 {count}/{total} 次，样本不足，不修正预测"
            )
            return predicted_ms, count, float(lat)
    alpha = min(0.6, 0.15 + 0.15 * count)
    adjusted = (1.0 - alpha) * predicted_ms + alpha * float(lat)
    factors.append(
        f"历史证据 {count} 条：加权延迟 {lat:.1f}ms，按 {alpha:.2f} 权重修正"
        f"（{predicted_ms:.1f}→{adjusted:.1f}ms）"
    )
    return adjusted, count, float(lat)


def _evaluation_feedback(ctx: DecisionContext, evaluations: Optional[dict],
                         plan_id: str, min_count: int, factors: list
                         ) -> tuple[float, int, Optional[float]]:
    if not evaluations:
        return 0.0, 0, None
    key = f"{ctx.query_type or 'unknown'}|{PLAN_DIMENSION}|{plan_id}"
    e = evaluations.get(key)
    if not isinstance(e, dict):
        return 0.0, 0, None
    # 成功率分母只含方向明确的执行（improved + degraded + failed）；
    # unchanged 是中性结果，不参与分母。旧条目依次回退 evaluated_count/count。
    count = e.get("decisive_count")
    if not isinstance(count, int) or isinstance(count, bool):
        count = e.get("evaluated_count")
    if not isinstance(count, int) or isinstance(count, bool):
        count = int(e.get("count") or 0)
    ratio = e.get("success_ratio")
    ratio = float(ratio) if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) else None
    if count < min_count or ratio is None:
        return 0.0, count, ratio
    penalty = 0.0
    if ratio < 0.5:
        penalty = min(3.0, 1.0 + (0.5 - ratio) * 4.0)
    elif ratio >= EVAL_SUCCESS_RATIO:
        penalty = -EVAL_SUCCESS_BONUS_MS   # 有界奖励（负惩罚）
    factors.append(
        f"评价表反馈：同类 {count} 次方向明确，成功率 {ratio:.2f}"
        + (f"，惩罚 {penalty:.1f}ms" if penalty > 0 else
           (f"，奖励 {-penalty:.1f}ms" if penalty < 0 else "，无惩罚"))
    )
    return penalty, count, ratio


def evaluate_plan(plan: dict, single_ms: float, ctx: DecisionContext,
                  evidence: Optional[HistoryEvidence],
                  evaluations: Optional[dict],
                  min_saving_ms: float = MIN_SAVING_MS,
                  min_saving_ratio: float = MIN_SAVING_RATIO,
                  eval_min_count: int = 3) -> PlanEvaluation:
    family = str(plan.get("family") or "baseline")
    plan_id = str(plan.get("plan_id") or "")
    risk = str(plan.get("risk") or "low")
    if risk not in RISK_PENALTY_MS:
        risk = "high"
    factors: list[str] = []

    # ---- 预测延迟 ----
    prior = _prior_speedup(plan)
    if family in ("partition", "partial_aggregation"):
        formula = _formula_speedup(plan, single_ms, ctx, factors)
        speedup = min(prior, formula)
        factors.append(
            f"预测加速取经验先验 {prior:.2f}× 与理论 {formula:.2f}× 的较小者 → {speedup:.2f}×"
        )
    else:
        speedup = prior
        factors.append(f"计划族先验加速 {speedup:.2f}×")
    predicted = single_ms / max(speedup, 0.05)

    # ---- 历史修正（学习闭环）----
    predicted, hist_count, hist_lat = _history_adjust(plan_id, predicted, evidence, factors)

    # ---- 反馈惩罚与风险 ----
    eval_penalty, eval_count, eval_ratio = _evaluation_feedback(
        ctx, evaluations, plan_id, eval_min_count, factors)
    risk_penalty = RISK_PENALTY_MS[risk]
    if risk_penalty:
        factors.append(f"风险惩罚 {risk_penalty:.1f}ms（{risk}）")
    effective = predicted + eval_penalty + risk_penalty

    # ---- 选择门槛 ----
    required = max(min_saving_ms, min_saving_ratio * single_ms)
    if risk == "medium" and hist_count < 1:
        required *= (1.0 + MEDIUM_EXTRA_RATIO)
        factors.append(f"medium 风险且无历史证据：门槛提高 {MEDIUM_EXTRA_RATIO:.0%}")
    elif risk == "high":
        required *= (1.0 + HIGH_EXTRA_RATIO)
    if ctx.load_level == "high" and len(plan.get("subqueries") or []) > 1:
        required += HIGH_LOAD_EXTRA_MS
        factors.append(f"系统高负载：并行计划门槛 +{HIGH_LOAD_EXTRA_MS:.1f}ms")
    saving = single_ms - effective

    selectable = True
    excluded: Optional[str] = None
    if family != "baseline":
        if risk == "high" and hist_count < 2:
            selectable = False
            excluded = "high 风险计划需要至少 2 条相似历史证据"
        elif saving < required:
            selectable = False
            excluded = (f"预测净收益 {saving:.1f}ms 未达到门槛 {required:.1f}ms"
                        f"（预测 {effective:.1f}ms vs 单条 {single_ms:.1f}ms）")
    if excluded:
        factors.append(f"不可选：{excluded}")

    return PlanEvaluation(
        plan_id=plan_id, family=family, risk=risk,
        predicted_ms=effective, single_ms=single_ms, saving_ms=saving,
        required_saving_ms=required, risk_penalty_ms=risk_penalty,
        eval_penalty_ms=eval_penalty, history_count=hist_count,
        history_latency_ms=hist_lat, eval_count=eval_count,
        eval_success_ratio=eval_ratio, selectable=selectable,
        excluded_reason=excluded, factors=factors,
    )


# ---------------------------------------------------------------------------
# 选择
# ---------------------------------------------------------------------------

def _pick_baseline(plans: list[dict]) -> Optional[dict]:
    for p in plans:
        if p.get("plan_id") == BASELINE_PLAN_ID:
            return p
    for p in plans:
        if p.get("family") == "baseline":
            return p
    return None


def select_plan(plans: list[dict], ctx: DecisionContext,
                evidence: Optional[HistoryEvidence] = None,
                evaluations: Optional[dict] = None,
                excluded_plan_ids: Optional[Iterable[str]] = None,
                **kwargs) -> PlanChoice:
    """在候选计划中选择；无正收益/证据不足 → baseline（安全动作）。

    excluded_plan_ids：Agent 闭环重规划时排除已实测失败/退化的计划。
    baseline 永不排除——它是安全动作，也是重规划后的兜底。
    """
    choice = PlanChoice()
    if not plans:
        choice.notes.append("没有可执行候选计划")
        return choice

    # ---- 重规划排除（Agent 动作后的反馈驱动；baseline 保留）----
    excluded = {str(x) for x in (excluded_plan_ids or ()) if x is not None}
    if excluded:
        hit = [str(p.get("plan_id")) for p in plans
               if str(p.get("plan_id")) in excluded]
        kept = [p for p in plans
                if str(p.get("plan_id")) not in excluded
                or str(p.get("family") or "") == "baseline"]
        if hit and len(kept) != len(plans):
            choice.notes.append("排除计划（Agent 重规划）：" + ",".join(sorted(set(hit))))
        plans = kept

    baseline = _pick_baseline(plans)
    baseline_id = (baseline or plans[0]).get("plan_id")
    single_ms, source, count = estimate_single_ms(ctx, evidence, baseline_id)
    choice.single_ms = single_ms
    choice.single_source = source
    choice.single_count = int(count or 0)
    choice.baseline_plan_id = baseline_id
    note = f"单条基线估计 {single_ms:.1f}ms（来源 {source}"
    if choice.single_count > 0:
        note += f"，历史样本 {choice.single_count} 次"
    note += "）"
    if source == "prior":
        note += "；固定先验，非实测"
    choice.notes.append(note)

    for p in plans:
        choice.evaluations.append(
            evaluate_plan(p, single_ms, ctx, evidence, evaluations, **kwargs))

    selectable = [e for e in choice.evaluations if e.selectable and e.family != "baseline"]
    selectable.sort(key=lambda e: (
        -e.saving_ms,
        _RISK_ORDER.get(e.risk, 9),
        _FAMILY_ORDER.get(e.family, 9),
        e.plan_id,
    ))

    if selectable and selectable[0].saving_ms > 0:
        chosen_id = selectable[0].plan_id
        choice.chosen = next((p for p in plans if p.get("plan_id") == chosen_id), None)
        choice.fallback = False
        choice.notes.append(
            f"选择 {chosen_id}：预测净收益 {selectable[0].saving_ms:.1f}ms"
            f"（预测 {selectable[0].predicted_ms:.1f}ms）"
        )
    else:
        choice.chosen = baseline or plans[0]
        choice.fallback = True
        if selectable:
            choice.notes.append("最优候选净收益不为正，退回 baseline")
        else:
            choice.notes.append("没有候选超过收益/风险门槛，退回 baseline")
    return choice
