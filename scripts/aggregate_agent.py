#!/usr/bin/env python3
"""聚合 Agent 模式的结果（results/*/run_log.jsonl）为**最终汇总**。

输出格式与每批次汇总（mataof run 结束打印）完全一致，便于逐批次 vs 总体的对比。
数据来源：run_log.jsonl 的查询行（真实执行状态/延迟/资源）与 batch 行
（每批次的墙钟总用时，用于总用时与吞吐）。
纯 stdlib，不依赖任何 mataof 模块。

用法：
  python3 scripts/aggregate_agent.py results/round_dbs/run_log.jsonl [...] [--json]
"""

from __future__ import annotations

import json
import sys


def percentile(sorted_values: list, p: float):
    if not sorted_values:
        return None
    idx = max(0, int(p / 100 * len(sorted_values) + 0.999999) - 1)
    return sorted_values[idx]


def _latency_stats(samples: list) -> dict:
    if not samples:
        return {"samples": 0, "avg_ms": None, "min_ms": None, "max_ms": None,
                "p50_ms": None, "p95_ms": None, "p99_ms": None}
    s = sorted(samples)
    return {
        "samples": len(s),
        "avg_ms": round(sum(s) / len(s), 2),
        "min_ms": s[0],
        "max_ms": s[-1],
        "p50_ms": percentile(s, 50) if len(s) >= 2 else None,
        "p95_ms": percentile(s, 95) if len(s) >= 2 else None,
        "p99_ms": percentile(s, 99) if len(s) >= 2 else None,
    }


def _res_stats(samples: list) -> dict:
    if not samples:
        return {"samples": 0, "avg": None, "max": None}
    return {"samples": len(samples),
            "avg": round(sum(samples) / len(samples), 4),
            "max": round(max(samples), 4)}


def parse(files: list, run_id: str = None) -> dict:
    query_rows: list = []
    batch_rows: list = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    if run_id is not None and row.get("run_id") != run_id:
                        continue   # 按轮次过滤（默认不过滤=全部历史累计）
                    if row.get("batch"):
                        batch_rows.append(row)
                    elif row.get("query_id"):
                        query_rows.append(row)
        except OSError:
            continue

    total = len(query_rows)
    if total == 0:
        return {"total_queries": 0, "total_time_s": None, "throughput_qps": None,
                "execution": {}, "latency_ms": _latency_stats([]),
                "baseline_latency_ms": _latency_stats([]),
                "resource_utilization": {}, "assessment_distribution": {}}

    status = {"success": 0, "failed": 0, "timeout": 0, "unknown": 0}
    latencies: list = []
    baseline_latencies: list = []
    cpu, cpu_peak, mem, mem_peak = [], [], [], []
    assessments: dict = {}

    for r in query_rows:
        st = r.get("execution_status") or "unknown"
        status[st] = status.get(st, 0) + 1
        lat = r.get("response_time_ms")
        if st == "success" and isinstance(lat, (int, float)):
            latencies.append(float(lat))
        bl = r.get("baseline_response_time_ms")
        if isinstance(bl, (int, float)):
            baseline_latencies.append(float(bl))
        for src, dst in (("cpu_utilization", cpu), ("cpu_utilization_peak", cpu_peak),
                         ("memory_utilization", mem), ("memory_utilization_peak", mem_peak)):
            v = r.get(src)
            if isinstance(v, (int, float)):
                dst.append(float(v))
        a = r.get("performance_assessment") or "insufficient_evidence"
        assessments[a] = assessments.get(a, 0) + 1

    batch_time = sum(b.get("batch_total_time_s") or 0 for b in batch_rows)
    return {
        "total_queries": total,
        "batches": len(batch_rows),
        "total_time_s": round(batch_time, 2) if batch_rows else None,
        "throughput_qps": round(total / batch_time, 4) if batch_time > 0 else None,
        "execution": {
            "success": status.get("success", 0),
            "failed": status.get("failed", 0),
            "timeout": status.get("timeout", 0),
            "unknown": status.get("unknown", 0),
            "success_rate": round(status.get("success", 0) / total, 4),
        },
        "latency_ms": _latency_stats(latencies),
        "baseline_latency_ms": _latency_stats(baseline_latencies),
        "resource_utilization": {
            "cpu": _res_stats(cpu),
            "cpu_peak": _res_stats(cpu_peak),
            "memory": _res_stats(mem),
            "memory_peak": _res_stats(mem_peak),
        },
        "assessment_distribution": assessments,
    }


def format_summary(s: dict, title: str = "汇总（全部批次）") -> str:
    """与 mataof run 每批次汇总完全一致的排版。"""
    ex = s["execution"]
    lines = [f"== {title} =="]
    lines.append(
        f"查询数: {s['total_queries']} | 总用时: {s['total_time_s']}s | "
        f"吞吐: {s['throughput_qps']} 查询/秒"
    )
    lines.append(
        f"执行: 成功 {ex.get('success', 0)} / 失败 {ex.get('failed', 0)} / "
        f"超时 {ex.get('timeout', 0)} / 未知 {ex.get('unknown', 0)} "
        f"（成功率 {ex.get('success_rate')}）"
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
    res = s.get("resource_utilization") or {}
    cpu, cpu_peak = res.get("cpu") or {}, res.get("cpu_peak") or {}
    mem, mem_peak = res.get("memory") or {}, res.get("memory_peak") or {}
    if cpu.get("samples") or mem.get("samples"):
        lines.append(
            f"资源（样本 {cpu.get('samples', 0)}）: CPU avg {cpu.get('avg')} / 峰值 {cpu_peak.get('avg')} | "
            f"内存 avg {mem.get('avg')} / 峰值 {mem_peak.get('max')}"
        )
    return "\n".join(lines)


def main(argv: list) -> int:
    as_json = "--json" in argv
    run_id = None
    if "--run-id" in argv:
        i = argv.index("--run-id")
        run_id = argv[i + 1] if i + 1 < len(argv) else None
        argv = argv[:i] + argv[i + 2:]
    files = [a for a in argv if a != "--json"]
    if not files:
        print("用法: aggregate_agent.py <run_log.jsonl ...> [--run-id ID] [--json]",
              file=sys.stderr)
        return 2
    r = parse(files, run_id=run_id)
    if as_json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    title = f"汇总（轮次 {run_id}）" if run_id else "汇总（全部批次）"
    print(format_summary(r, title=title))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
