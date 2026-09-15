"""策略效果判断（基于实际测量结果，不基于猜测）。

判断值：improved | degraded | unchanged | failed | insufficient_evidence

规则（确定性）：
- 执行状态 failed / timeout → failed（执行未完成；归因不确定，置信度 0.7）；
- 执行状态 cancelled → insufficient_evidence（无有效执行结果）；
- 执行状态 unknown → insufficient_evidence；
- success：用主指标（response_time_ms，缺失时退到 p95_latency_ms）对比
  baseline（优先）或历史典型值（historical_reference）：
    变化 ≤ -change_threshold_percent → improved；
    变化 ≥ +change_threshold_percent → degraded；
    其余 → unchanged；
  两者都不可比 → insufficient_evidence。

置信度（0~1，保留 2 位；单次实验不夸大）：
- insufficient_evidence → 0.0；
- failed → 0.7（执行失败被观测到，但原因归属不确定）；
- improved/degraded/unchanged：基础 0.6；对比来源为 baseline（而非历史典型）→ +0.1；
  p95 与 p99 变化方向与主指标一致（佐证）→ +0.1；上限 0.8。

单次执行观察不代表策略永久有效（feedback.scope = single_execution）。
"""

from __future__ import annotations

from typing import Optional

from mataof.agents.execution_monitoring.comparison import percent_change

ASSESSMENT_VALUES = ("improved", "degraded", "unchanged", "failed", "insufficient_evidence")


def _direction(v: Optional[float]) -> int:
    """变化方向：-1 下降 / 0 持平 / +1 上升；None → 0（不参与佐证）。"""
    if v is None:
        return 0
    if v < 0:
        return -1
    if v > 0:
        return 1
    return 0


def assess(status: str, metrics: dict, comparison: dict,
           historical_reference: Optional[dict], thresholds: dict,
           notes: list[str]) -> tuple[str, float, list[str]]:
    """返回 (performance_assessment, 反馈置信度, assessment_basis 事实列表)。"""
    basis: list[str] = []

    if status in ("failed", "timeout"):
        basis.append(f"执行状态：{status}（未获得有效执行结果）")
        return "failed", 0.7, basis
    if status == "cancelled":
        basis.append("执行被取消，未获得有效执行结果")
        return "insufficient_evidence", 0.0, basis
    if status != "success":
        basis.append("执行状态未知，无法判断策略效果")
        return "insufficient_evidence", 0.0, basis

    threshold = thresholds["change_threshold_percent"]

    # ---- 选择主指标与对比来源 ----
    primary_key = "response_time_ms"
    if metrics.get("response_time_ms") is None and metrics.get("p95_latency_ms") is not None:
        primary_key = "p95_latency_ms"
        basis.append("response_time_ms 缺失，使用 p95_latency_ms 作为主指标")

    cur = metrics.get(primary_key)
    if cur is None:
        basis.append("无有效延迟测量值（response_time_ms / p95_latency_ms 均缺失）")
        return "insufficient_evidence", 0.0, basis

    source: Optional[str] = None
    base: Optional[float] = None
    # baseline 优先（comparison 中对应变化字段已计算）
    change_key = ("latency_change_percent" if primary_key == "response_time_ms"
                  else "p95_change_percent")
    if comparison.get("available") and comparison.get(change_key) is not None:
        source = "baseline"
        base_pct = comparison[change_key]
    elif isinstance(historical_reference, dict) and historical_reference.get(primary_key) is not None:
        ref = historical_reference.get(primary_key)
        if isinstance(ref, (int, float)) and not isinstance(ref, bool) and ref >= 0:
            source = "historical"
            base = float(ref)
            base_pct = percent_change(cur, base)
        else:
            base_pct = None
            notes.append("historical_reference 的延迟参考值无效，不用于效果判断")
    else:
        base_pct = None

    if source is None or base_pct is None:
        basis.append("无 baseline 且无有效历史典型值，无法比较，效果判断不足")
        return "insufficient_evidence", 0.0, basis

    src_text = "baseline" if source == "baseline" else "历史典型值"
    basis.append(f"{primary_key}: 实测 {cur} ms vs {src_text}（变化 {base_pct}%）")

    if base_pct <= -threshold:
        assessment = "improved"
    elif base_pct >= threshold:
        assessment = "degraded"
    else:
        assessment = "unchanged"

    # ---- 置信度 ----
    conf = 0.6
    if source == "baseline":
        conf += 0.1
    if comparison.get("p95_change_percent") is not None and comparison.get("p99_change_percent") is not None:
        if (_direction(comparison["p95_change_percent"]) == _direction(base_pct)
                and _direction(comparison["p99_change_percent"]) == _direction(base_pct)
                and _direction(base_pct) != 0):
            conf += 0.1
            basis.append("p95/p99 变化方向与主指标一致（佐证）")
    conf = min(conf, 0.8)

    return assessment, round(conf, 2), basis


def strategy_effective_mapping(assessment: str) -> Optional[bool]:
    """feedback.strategy_effective 映射：improved → true；degraded/failed → false；其余 → null。"""
    if assessment == "improved":
        return True
    if assessment in ("degraded", "failed"):
        return False
    return None
