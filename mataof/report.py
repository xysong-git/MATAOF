"""批次执行汇总报告（纯函数，数据全部来自本批次真实执行样本）。

分位数（P50/P95/P99）是多样本统计：单次执行不产生（不编造），
在**一批执行完成后**对成功执行的实测延迟样本做统计。

统计口径（全部可验证）：
- 延迟统计：本批次 execution_status=success 且 response_time_ms 有效的样本；
  样本不足 2 条时百分位如实置 null；
- 吞吐量：查询总数 / 批次墙钟时间（查询/秒）；
- 执行成功率：success / 总数（failed/timeout/unknown 分别计数）；
- 效果分布：improved/unchanged/degraded/failed/insufficient_evidence 计数；
- baseline 延迟：本批次真实采集的 baseline 样本（若有）同样统计。
"""

from __future__ import annotations

import math
from typing import Optional


def percentile_nearest_rank(sorted_values: list, p: float) -> Optional[float]:
    """最近秩法百分位（确定性）。空列表 → None。"""
    if not sorted_values:
        return None
    idx = max(0, math.ceil(p / 100.0 * len(sorted_values)) - 1)
    return sorted_values[idx]


def _latency_stats(samples: list) -> dict:
    """对一组延迟样本（毫秒）做统计。样本 <2 时百分位为 null（不编造）。"""
    if not samples:
        return {"samples": 0, "avg_ms": None, "min_ms": None, "max_ms": None,
                "p50_ms": None, "p95_ms": None, "p99_ms": None}
    s = sorted(samples)
    return {
        "samples": len(s),
        "avg_ms": round(sum(s) / len(s), 2),
        "min_ms": s[0],
        "max_ms": s[-1],
        "p50_ms": percentile_nearest_rank(s, 50) if len(s) >= 2 else None,
        "p95_ms": percentile_nearest_rank(s, 95) if len(s) >= 2 else None,
        "p99_ms": percentile_nearest_rank(s, 99) if len(s) >= 2 else None,
    }


def batch_summary(traces: list, total_time_s: Optional[float] = None) -> dict:
    """对本批次追踪记录做执行汇总。

    traces        ：PipelineRunner.run_query / run_file 返回的追踪列表
    total_time_s  ：批次墙钟总用时（秒，由调用方计时；None 时用执行时间戳跨度）
    """
    total = len(traces)
    if total == 0:
        return {
            "total_queries": 0, "total_time_s": None, "throughput_qps": None,
            "execution": {"success": 0, "failed": 0, "timeout": 0, "unknown": 0,
                          "success_rate": None},
            "latency_ms": _latency_stats([]),
            "baseline_latency_ms": _latency_stats([]),
            "assessment_distribution": {},
        }

    # 执行状态分布（真实状态，不推断）
    status_counts = {"success": 0, "failed": 0, "timeout": 0, "unknown": 0}
    latency_samples: list = []
    baseline_samples: list = []
    assessments: dict = {}

    for t in traces:
        mon = t.get("monitoring") or {}
        st = mon.get("execution_status") or "unknown"
        status_counts[st] = status_counts.get(st, 0) + 1
        lat = (t.get("execution") or {}).get("metrics", {}).get("response_time_ms")
        if st == "success" and isinstance(lat, (int, float)) and not isinstance(lat, bool):
            latency_samples.append(float(lat))
        bl = (t.get("execution") or {}).get("baseline_metrics") or {}
        b_lat = bl.get("response_time_ms")
        if isinstance(b_lat, (int, float)) and not isinstance(b_lat, bool):
            baseline_samples.append(float(b_lat))
        a = mon.get("performance_assessment") or "insufficient_evidence"
        assessments[a] = assessments.get(a, 0) + 1

    # 总用时：调用方计时优先；否则用执行时间戳跨度（真实执行时间）
    if total_time_s is None:
        stamps = [t.get("execution", {}).get("timestamp") for t in traces
                  if (t.get("execution") or {}).get("timestamp")]
        stamps = [s for s in stamps if isinstance(s, (int, float))]
        if len(stamps) >= 2:
            total_time_s = (max(stamps) - min(stamps)) / 1000.0

    success = status_counts.get("success", 0)
    return {
        "total_queries": total,
        "total_time_s": round(total_time_s, 2) if total_time_s is not None else None,
        "throughput_qps": round(total / total_time_s, 4)
                          if total_time_s and total_time_s > 0 else None,
        "execution": {
            "success": success,
            "failed": status_counts.get("failed", 0),
            "timeout": status_counts.get("timeout", 0),
            "unknown": status_counts.get("unknown", 0),
            "success_rate": round(success / total, 4),
        },
        "latency_ms": _latency_stats(latency_samples),
        "baseline_latency_ms": _latency_stats(baseline_samples),
        "assessment_distribution": assessments,
    }


def format_batch_summary(s: dict) -> str:
    """人类可读的汇总文本（终端打印用）。"""
    ex = s["execution"]
    lines = ["== 执行汇总（本批次）=="]
    lines.append(
        f"查询数: {s['total_queries']} | 总用时: {s['total_time_s']}s | "
        f"吞吐: {s['throughput_qps']} 查询/秒"
    )
    lines.append(
        f"执行: 成功 {ex['success']} / 失败 {ex['failed']} / 超时 {ex['timeout']} / "
        f"未知 {ex['unknown']}（成功率 {ex['success_rate']}）"
    )
    dist = " | ".join(f"{k} {v}" for k, v in sorted(s["assessment_distribution"].items()))
    lines.append(f"效果分布: {dist or '（无）'}")
    l = s["latency_ms"]
    lines.append(
        f"延迟（成功执行，样本 {l['samples']}）: avg {l['avg_ms']}ms | min {l['min_ms']}ms | "
        f"max {l['max_ms']}ms | P50 {l['p50_ms']}ms | P95 {l['p95_ms']}ms | P99 {l['p99_ms']}ms"
    )
    bl = s["baseline_latency_ms"]
    if bl["samples"]:
        lines.append(
            f"baseline 延迟（样本 {bl['samples']}）: avg {bl['avg_ms']}ms | "
            f"P50 {bl['p50_ms']}ms | P95 {bl['p95_ms']}ms | P99 {bl['p99_ms']}ms"
        )
    return "\n".join(lines)
