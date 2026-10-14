#!/usr/bin/env python3
"""聚合 Agent 模式的结果（results/*/run_log.jsonl）为**最终汇总**。

输出格式与每批次汇总（mataof run 结束打印）完全一致，便于逐批次 vs 总体的对比。
数据来源：run_log.jsonl 的查询行（真实执行状态/延迟/资源）与 batch 行
（每批次的墙钟总用时，用于总用时与吞吐）。
统计函数复用 mataof.report（H2：单一实现）。

用法：
  python3 scripts/aggregate_agent.py results/round_dbs/run_log.jsonl [...] \
      [--run-id ID] [--json]
"""

from __future__ import annotations

import json
import os
import sys

# 路径引导：允许脚本独立运行时 import 项目包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.report import (  # noqa: E402
    cost_stats,
    format_batch_summary,
    latency_stats,
    net_overhead,
    resource_stats,
)


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
                "execution": {"success": 0, "failed": 0, "timeout": 0,
                              "unknown": 0, "success_rate": None},
                "latency_ms": latency_stats([]),
                "baseline_latency_ms": latency_stats([]),
                "stage_cost_ms": {"samples": 0},
                "end_to_end_ms": latency_stats([]),
                "net_overhead_ms": None,
                "resource_utilization": {}, "assessment_distribution": {}}

    status = {"success": 0, "failed": 0, "timeout": 0, "unknown": 0}
    latencies: list = []
    baseline_latencies: list = []
    stage_samples: dict = {k: [] for k in (
        "analysis_ms", "retrieval_ms", "decision_ms", "monitoring_ms",
        "knowledge_update_ms", "pipeline_overhead_ms", "end_to_end_ms")}
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
        # D8：各阶段成本/端到端（旧 run_log 行无这些字段 → 自动跳过，样本如实统计）
        for key in stage_samples:
            v = r.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                stage_samples[key].append(float(v))
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
        "latency_ms": latency_stats(latencies),
        "baseline_latency_ms": latency_stats(baseline_latencies),
        # D8：与每批次汇总完全一致的口径（分阶段成本归因 + 端到端 + 净开销；
        # llm_backfill 不落 run_log，轮次级 LLM 推理成本见 data/llm_log.jsonl）
        "stage_cost_ms": {
            "samples": len(stage_samples["pipeline_overhead_ms"]),
            "analysis_ms": cost_stats(stage_samples["analysis_ms"]),
            "retrieval_ms": cost_stats(stage_samples["retrieval_ms"]),
            "decision_ms": cost_stats(stage_samples["decision_ms"]),
            "monitoring_ms": cost_stats(stage_samples["monitoring_ms"]),
            "knowledge_update_ms": cost_stats(stage_samples["knowledge_update_ms"]),
            "pipeline_total_ms": cost_stats(stage_samples["pipeline_overhead_ms"]),
            "llm_backfill_ms": {"samples": 0, "avg_ms": None},
        },
        "end_to_end_ms": latency_stats(stage_samples["end_to_end_ms"]),
        "net_overhead_ms": net_overhead(stage_samples["end_to_end_ms"],
                                        baseline_latencies),
        "resource_utilization": {
            "cpu": resource_stats(cpu),
            "cpu_peak": resource_stats(cpu_peak),
            "memory": resource_stats(mem),
            "memory_peak": resource_stats(mem_peak),
        },
        "assessment_distribution": assessments,
    }


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
    print(format_batch_summary(r, title=title))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
