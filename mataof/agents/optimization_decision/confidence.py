"""决策置信度（确定性公式；证据不足时不得人为提高置信度）。

维度置信度（0~1，保留 2 位）：
    conf_d = 0.35 * data_factor + 0.35 * evidence_factor + 0.30 * margin_factor
- data_factor   ：1.0 有数据库上下文数据支撑；0.5 仅需求满足（结构依据）；0.2 回退（仅安全依据）
- evidence_factor：min(1.0, 该策略历史证据权重 × 2)；无历史证据 → 0
- margin_factor ：与第二名分差 ≥0.15 → 1.0；≥0.05 → 0.7；>0 → 0.5；唯一候选 → 0.6；平分 → 0.4
- 上限：回退选择 → 0.3；高风险策略 → 0.7

整体置信度：
    overall = mean(适用维度置信度) × (0.7 + 0.3 × 分析置信度)
    无任何数据库状态且无历史证据时上限 0.6；输入无效 → 0.0。
"""

from __future__ import annotations

from typing import Optional

from mataof.agents.optimization_decision.context import DecisionContext
from mataof.agents.optimization_decision.scoring import CandidateEvaluation, DimensionResult
from mataof.agents.optimization_decision.similarity import HistoryEvidence


def _margin_factor(chosen: CandidateEvaluation, selectable: list) -> float:
    if len(selectable) <= 1:
        return 0.6
    second = selectable[1].score
    margin = chosen.score - second
    if margin >= 0.15:
        return 1.0
    if margin >= 0.05:
        return 0.7
    if margin > 0:
        return 0.5
    return 0.4


def dimension_confidence(result: DimensionResult, ctx: DecisionContext,
                         ev: HistoryEvidence) -> float:
    """单个维度的置信度；维度不适用 → 0.0（不参与整体均值）。"""
    if not result.applicable:
        return 0.0
    if result.is_fallback:
        return 0.3  # 回退：证据不足，置信度上限 0.3（不虚高）

    chosen = result.chosen
    assert chosen is not None

    if chosen.context_bonus > 0:
        data_factor = 1.0
    else:
        data_factor = 0.5

    evidence_factor = 0.0
    if ev.count_for(result.dimension, chosen.name) >= 1:
        evidence_factor = min(1.0, ev.weight_for(result.dimension, chosen.name) * 2)

    margin = _margin_factor(chosen, result.selectable)

    conf = 0.35 * data_factor + 0.35 * evidence_factor + 0.30 * margin
    if chosen.risk == "high":
        conf = min(conf, 0.7)
    return round(conf, 2)


def overall_confidence(results: dict, ctx: DecisionContext,
                       dim_confidences: dict, history_used: int) -> float:
    """整体置信度。

    results          ：维度名 → DimensionResult（用于适用性判断）
    dim_confidences  ：维度名 → 维度置信度（dimension_confidence 的产物）
    history_used     ：参与决策的历史记录条数
    """
    if ctx.errors:
        return 0.0
    applicable = [d for d, r in results.items() if r.applicable]
    if not applicable:
        return 0.0
    mean_conf = sum(dim_confidences[d] for d in applicable) / len(applicable)

    ac = ctx.analysis_confidence
    factor = 0.7 + 0.3 * ac if ac is not None else 0.7
    overall = mean_conf * factor
    if not ctx.database_state and history_used == 0:
        overall = min(overall, 0.6)
    return round(overall, 2)
