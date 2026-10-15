"""批次执行汇总报告（纯函数，数据全部来自本批次真实执行样本）。

分位数（P50/P95/P99）是多样本统计：单次执行不产生（不编造），
在**一批执行完成后**对成功执行的实测延迟样本做统计。

统计口径（全部可验证）：
- 延迟统计：本批次 execution_status=success 且 response_time_ms 有效的样本；
  样本不足 2 条时百分位如实置 null；
- 决策开销（D8）：trace["overhead"] 各阶段成本（分析/检索/决策/监控/入库）按
  "每条平均" 归因（成本口径，不出分位数）；端到端（进入→结果）为延迟口径
  （avg/min/max/P50/P95/P99）；不按执行状态过滤——失败/超时同样有优化开销；
- 净开销：端到端每条平均 − baseline 执行每条平均（判断优化系统是否值得）；
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


def latency_stats(samples: list) -> dict:
    """对一组延迟样本（毫秒）做统计。样本 <2 时百分位为 null（不编造）。

    公共统计函数（H2）：CLI 批次汇总与 scripts/aggregate_agent.py 共用。
    """
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


def resource_stats(samples: list) -> dict:
    """对一组资源利用率样本（0~1）做统计（avg/max）。

    公共统计函数（H2）：CLI 批次汇总与 scripts/aggregate_agent.py 共用。
    """
    if not samples:
        return {"samples": 0, "avg": None, "max": None}
    return {"samples": len(samples),
            "avg": round(sum(samples) / len(samples), 4),
            "max": round(max(samples), 4)}


def cost_stats(samples: list) -> dict:
    """对一组阶段成本样本（毫秒）做每条平均统计（成本归因口径，不出分位数）。"""
    if not samples:
        return {"samples": 0, "avg_ms": None}
    return {"samples": len(samples), "avg_ms": round(sum(samples) / len(samples), 2)}


def net_overhead(e2e_samples: list, baseline_samples: list) -> Optional[float]:
    """净开销 = 端到端每条平均 − baseline 执行每条平均（判断优化系统是否值得）。"""
    if not e2e_samples or not baseline_samples:
        return None
    return round(sum(e2e_samples) / len(e2e_samples)
                 - sum(baseline_samples) / len(baseline_samples), 2)


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
            "latency_ms": latency_stats([]),
            "baseline_latency_ms": latency_stats([]),
            # D8：优化机制各阶段成本 + 端到端（空批次 → 无样本，如实 null）
            "stage_cost_ms": {"samples": 0},
            "end_to_end_ms": latency_stats([]),
            "net_overhead_ms": None,
            "assessment_distribution": {},
        }

    # 执行状态分布（真实状态，不推断）
    status_counts = {"success": 0, "failed": 0, "timeout": 0, "unknown": 0}
    latency_samples: list = []
    baseline_samples: list = []
    e2e_samples: list = []
    stage_samples: dict = {k: [] for k in (
        "analysis_ms", "retrieval_ms", "decision_ms", "monitoring_ms",
        "knowledge_update_ms", "pipeline_overhead_ms", "llm_backfill_ms")}
    assessments: dict = {}
    cpu_samples: list = []
    cpu_peak_samples: list = []
    mem_samples: list = []
    mem_peak_samples: list = []

    for t in traces:
        mon = t.get("monitoring") or {}
        st = mon.get("execution_status") or "unknown"
        status_counts[st] = status_counts.get(st, 0) + 1
        metrics = (t.get("execution") or {}).get("metrics") or {}
        lat = metrics.get("response_time_ms")
        if st == "success" and isinstance(lat, (int, float)) and not isinstance(lat, bool):
            latency_samples.append(float(lat))
        for src, dst in (("cpu_utilization", cpu_samples),
                         ("cpu_utilization_peak", cpu_peak_samples),
                         ("memory_utilization", mem_samples),
                         ("memory_utilization_peak", mem_peak_samples)):
            v = metrics.get(src)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                dst.append(float(v))
        bl = (t.get("execution") or {}).get("baseline_metrics") or {}
        b_lat = bl.get("response_time_ms")
        if isinstance(b_lat, (int, float)) and not isinstance(b_lat, bool):
            baseline_samples.append(float(b_lat))
        # D8：各阶段成本/端到端（runner 墙钟；不按执行状态过滤——失败/超时也有开销）
        ov = t.get("overhead") or {}
        for key in stage_samples:
            v = ov.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                stage_samples[key].append(float(v))
        v = ov.get("end_to_end_ms")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            e2e_samples.append(float(v))
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
        "latency_ms": latency_stats(latency_samples),
        "baseline_latency_ms": latency_stats(baseline_samples),
        # D8：优化机制各阶段成本（每条平均，成本归因）与端到端
        "stage_cost_ms": {
            "samples": len(stage_samples["pipeline_overhead_ms"]),
            "analysis_ms": cost_stats(stage_samples["analysis_ms"]),
            "retrieval_ms": cost_stats(stage_samples["retrieval_ms"]),
            "decision_ms": cost_stats(stage_samples["decision_ms"]),
            "monitoring_ms": cost_stats(stage_samples["monitoring_ms"]),
            "knowledge_update_ms": cost_stats(stage_samples["knowledge_update_ms"]),
            "pipeline_total_ms": cost_stats(stage_samples["pipeline_overhead_ms"]),
            "llm_backfill_ms": cost_stats(stage_samples["llm_backfill_ms"]),
        },
        "end_to_end_ms": latency_stats(e2e_samples),
        "net_overhead_ms": net_overhead(e2e_samples, baseline_samples),
        "resource_utilization": {   # 开题报告 3.5：平均与峰值口径
            "cpu": resource_stats(cpu_samples),
            "cpu_peak": resource_stats(cpu_peak_samples),
            "memory": resource_stats(mem_samples),
            "memory_peak": resource_stats(mem_peak_samples),
        },
        "assessment_distribution": assessments,
    }


def format_batch_summary(s: dict, title: str = "执行汇总（本批次）") -> str:
    """人类可读的汇总文本（终端打印用）。title 可自定义（如按文件命名批次）。"""
    ex = s["execution"]
    lines = [f"== {title} =="]
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
    # D8：优化机制各阶段成本（每条平均，成本归因）+ 端到端 + 净开销
    sc = s.get("stage_cost_ms") or {}
    if sc.get("samples"):
        parts = []
        for key, label in (("analysis_ms", "分析"), ("retrieval_ms", "检索"),
                           ("decision_ms", "决策"), ("monitoring_ms", "监控"),
                           ("knowledge_update_ms", "入库")):
            st = sc.get(key) or {}
            if st.get("avg_ms") is not None:
                parts.append(f"{label} {st['avg_ms']}ms")
        tot = sc.get("pipeline_total_ms") or {}
        if tot.get("avg_ms") is not None:
            parts.append(f"合计 {tot['avg_ms']}ms")
        lines.append(f"决策开销·各阶段（每条平均，样本 {sc['samples']}）: "
                     + " | ".join(parts))
        lb = sc.get("llm_backfill_ms") or {}
        if lb.get("samples"):
            lines.append(f"LLM 推理·异步补写（每条平均，样本 {lb['samples']}）: "
                         f"{lb['avg_ms']}ms（不计入端到端）")
    e2e = s.get("end_to_end_ms") or {}
    if e2e.get("samples"):
        lines.append(
            f"端到端（进入→结果，样本 {e2e['samples']}）: avg {e2e['avg_ms']}ms | "
            f"min {e2e['min_ms']}ms | max {e2e['max_ms']}ms | "
            f"P50 {e2e['p50_ms']}ms | P95 {e2e['p95_ms']}ms | P99 {e2e['p99_ms']}ms")
    if s.get("net_overhead_ms") is not None:
        lines.append(f"净开销（端到端 avg − baseline 执行 avg）: "
                     f"{s['net_overhead_ms']}ms/条")
    res = s.get("resource_utilization") or {}
    cpu, cpu_peak = res.get("cpu") or {}, res.get("cpu_peak") or {}
    mem, mem_peak = res.get("memory") or {}, res.get("memory_peak") or {}
    if (cpu.get("samples") or mem.get("samples")):
        lines.append(
            f"资源（样本 {cpu.get('samples', 0)}）: CPU avg {cpu.get('avg')} / 峰值 {cpu_peak.get('avg')} | "
            f"内存 avg {mem.get('avg')} / 峰值 {mem_peak.get('max')}"
        )
    return "\n".join(lines)
