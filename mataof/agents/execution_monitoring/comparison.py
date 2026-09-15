"""Baseline 比较与 performance_change 方向标签。

规则：
- 仅当系统提供 baseline 执行结果时才比较；没有 baseline 绝不生成比较结果；
- 变化百分比 = (实测 - baseline) / baseline × 100，保留 2 位；baseline 为 0 → null + 说明；
- performance_change 标签仅在 |变化| ≥ change_threshold_percent（默认 5%）时生成：
    response_time 下降 → latency_reduction；上升 → latency_increase；
    p95/p99 同理（p95_improvement / p95_degradation…）；
    throughput 上升 → throughput_improvement；下降 → throughput_decrease；
    cpu/memory 下降 → cpu_decrease / memory_decrease；上升 → cpu_increase / memory_increase；
    io 下降/上升 → io_decrease / io_increase。
"""

from __future__ import annotations

from typing import Optional

from mataof.agents.execution_monitoring.normalize import normalize_metrics

# (当前指标键, baseline 指标键, 输出变化字段, 上升标签, 下降标签)
PAIR_MAP = [
    ("response_time_ms", "latency_change_percent", "latency_increase", "latency_reduction"),
    ("p95_latency_ms", "p95_change_percent", "p95_degradation", "p95_improvement"),
    ("p99_latency_ms", "p99_change_percent", "p99_degradation", "p99_improvement"),
    ("throughput", "throughput_change_percent", "throughput_improvement", "throughput_decrease"),
    ("cpu_utilization", "cpu_change_percent", "cpu_increase", "cpu_decrease"),
    ("memory_utilization", "memory_change_percent", "memory_increase", "memory_decrease"),
    ("io_throughput", "io_change_percent", "io_increase", "io_decrease"),
]


def percent_change(cur: Optional[float], base: Optional[float]) -> Optional[float]:
    if cur is None or base is None:
        return None
    if base == 0:
        return None
    return round((cur - base) / base * 100.0, 2)


def compare_with_baseline(cur_metrics: dict, baseline_raw: Optional[dict],
                          thresholds: dict, notes: list[str]) -> tuple[dict, list[str]]:
    """返回 (baseline_comparison 节, performance_change 标签列表)。"""
    comparison = {
        "available": False,
        "latency_change_percent": None,
        "p95_change_percent": None,
        "p99_change_percent": None,
        "throughput_change_percent": None,
        "cpu_change_percent": None,
        "memory_change_percent": None,
        "io_change_percent": None,
    }
    if not isinstance(baseline_raw, dict) or not baseline_raw:
        notes.append("未提供 baseline 执行结果，不生成比较结果")
        return comparison, []

    baseline = normalize_metrics(baseline_raw, notes, prefix="baseline.")
    threshold = thresholds["change_threshold_percent"]
    labels: list[str] = []
    computed = 0

    for metric_key, out_key, up_label, down_label in PAIR_MAP:
        pct = percent_change(cur_metrics.get(metric_key), baseline.get(metric_key))
        if pct is None:
            if cur_metrics.get(metric_key) is not None and baseline.get(metric_key) is None:
                notes.append(f"baseline 未提供有效 {metric_key}，该对比项跳过")
            elif baseline.get(metric_key) == 0:
                notes.append(f"baseline {metric_key} 为 0，无法计算变化百分比")
            continue
        comparison[out_key] = pct
        computed += 1
        if abs(pct) >= threshold:
            labels.append(down_label if pct < 0 else up_label)

    comparison["available"] = computed > 0
    if computed == 0:
        notes.append("baseline 与实测指标无可比较的有效数据对")
    return comparison, labels
