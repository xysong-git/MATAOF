"""事实采集层：执行指标与执行状态的校验、归一化。

核心原则：所有指标必须来自真实执行环境/监控接口；无法获得或无效的值一律 null，
并在 notes 中记录原因——绝不估计、猜测或编造。

归一化规则（确定性）：
- 时间类/吞吐类指标：必须为非负数值，原样保留（单位由监控接口定义，不做换算假设）；
- 利用率指标（cpu_utilization / memory_utilization）：接受 0~1 小数或 0~100 百分数，
  统一归一化为 0~1 小数（>1 视为百分数 ÷100）；负值或 >100 无效；
- 布尔值视为无效（True/False 不是测量值）。
"""

from __future__ import annotations

from typing import Any, Optional

EXECUTION_STATUSES = ("success", "timeout", "failed", "cancelled", "unknown")

# 异常/判断阈值默认值（输入 thresholds 可覆盖）
DEFAULT_THRESHOLDS: dict[str, float] = {
    "response_time_timeout_ms": 10000.0,  # 绝对延迟上限（超过即异常）
    "latency_deviation_factor": 2.0,      # 相对 baseline 的延迟偏离倍数
    "history_deviation_factor": 2.0,      # 相对历史典型值的偏离倍数
    "cpu_threshold": 0.9,                 # CPU 利用率异常阈值
    "memory_threshold": 0.9,              # 内存利用率异常阈值
    "io_deviation_factor": 1.5,           # 相对 baseline 的 IO 偏离倍数
    "change_threshold_percent": 5.0,      # 效果判断/变化标签的相对变化阈值（%）
}

# 指标 → 校验类型
METRIC_SPECS: dict[str, dict] = {
    "response_time_ms": {"kind": "nonneg"},
    "p50_latency_ms": {"kind": "nonneg"},
    "p95_latency_ms": {"kind": "nonneg"},
    "p99_latency_ms": {"kind": "nonneg"},
    "throughput": {"kind": "nonneg"},
    "cpu_utilization": {"kind": "utilization"},
    "memory_utilization": {"kind": "utilization"},
    "io_throughput": {"kind": "nonneg"},
}


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def normalize_metrics(raw: Optional[dict], notes: list[str], prefix: str = "") -> dict:
    """校验并归一化一组指标。无效值 → null + 说明。prefix 用于区分 baseline 的说明文案。"""
    out: dict = {}
    raw = raw if isinstance(raw, dict) else {}
    if not raw:
        notes.append(f"{prefix}未提供执行指标（视为 unknown）" if prefix else "未提供执行指标（视为 unknown）")
    for key, spec in METRIC_SPECS.items():
        v = raw.get(key)
        if v is None:
            out[key] = None
            continue
        if not _is_number(v):
            notes.append(f"指标 {prefix}{key} 无效（非数值），置为 null")
            out[key] = None
            continue
        if spec["kind"] == "nonneg":
            if v < 0:
                notes.append(f"指标 {prefix}{key} 无效（负值），置为 null")
                out[key] = None
            else:
                out[key] = float(v)
        else:  # utilization：0~1 或 0~100 → 0~1
            if v < 0:
                notes.append(f"指标 {prefix}{key} 无效（负值），置为 null")
                out[key] = None
            elif v > 100:
                notes.append(f"指标 {prefix}{key} 无效（超过 100），置为 null")
                out[key] = None
            else:
                out[key] = round(v / 100.0 if v > 1 else v, 4)
    return out


def normalize_status(raw_status: Any, failure_reason: Any, notes: list[str]) -> tuple[str, Optional[str]]:
    """校验执行状态与失败原因。状态缺失/非法 → unknown（不推断）。"""
    if raw_status is None:
        notes.append("未提供执行状态，视为 unknown")
        return "unknown", None
    if raw_status not in EXECUTION_STATUSES:
        notes.append(f"执行状态非法（{raw_status}），视为 unknown")
        return "unknown", None
    if raw_status == "failed" and not failure_reason:
        notes.append("执行失败但未提供失败原因")
    return raw_status, (failure_reason if failure_reason else None)


def resolve_thresholds(raw: Any, notes: list[str]) -> dict:
    """合并阈值：默认值 + 输入覆盖（只接受数值覆盖项）。"""
    out = dict(DEFAULT_THRESHOLDS)
    if raw is None:
        return out
    if not isinstance(raw, dict):
        notes.append("thresholds 不是对象，使用默认阈值")
        return out
    for k, v in raw.items():
        if k in out and _is_number(v) and v >= 0:
            out[k] = float(v)
        elif k in out:
            notes.append(f"阈值 {k} 无效（{v}），使用默认值 {out[k]}")
    return out
