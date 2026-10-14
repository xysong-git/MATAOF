#!/usr/bin/env python3
"""聚合原生 IoTDB baseline 结果为统计报告（支持两种数据格式）。

新格式（推荐）：run_native_client.py 输出的 .jsonl——用与 agent 模式完全相同的
IoTDB 客户端与计时代码直发原始 SQL，逐条客户端墙钟 response_time_ms
（发送 → 完整消费结果集），与 agent 的延迟口径一致，可直接对比。

旧格式（历史数据）：start-cli.sh 的 .out 文件——CLI 客户端进程打印的
"It costs X.XXXs"（CLI 进程墙钟：发送 → 收到第一批结果；不含后续结果行消费，
也不是服务器内部执行时间）。仅用于回看历史数据。

用法：
  python3 scripts/aggregate_native.py results_baseline/*.jsonl [--json]
  python3 scripts/aggregate_native.py results_baseline/*.out   [--json]   # 旧数据
"""

from __future__ import annotations

import json
import os
import re
import sys

# 路径引导 + 公共统计复用（H2）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.report import (  # noqa: E402
    latency_stats,
    percentile_nearest_rank as percentile,
)

COST_RE = re.compile(r"It costs ([\d.]+)s")


def parse_jsonl(files: list) -> dict:
    """解析 run_native_client.py 输出（客户端墙钟，与 agent 同口径）。"""
    rows: list = []
    batch_time = 0.0
    n_batches = 0
    for f in files:
        try:
            fh = open(f, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("batch"):
                    batch_time += float(row.get("batch_total_time_s") or 0)
                    n_batches += 1
                else:
                    rows.append(row)

    status = {"success": 0, "failed": 0, "timeout": 0, "unknown": 0}
    latencies: list = []
    for r in rows:
        st = r.get("execution_status") or "unknown"
        status[st] = status.get(st, 0) + 1
        v = r.get("response_time_ms")
        if st == "success" and isinstance(v, (int, float)) and not isinstance(v, bool):
            latencies.append(float(v))

    total = len(rows)
    return {
        "executions": total,
        "format": "client-jsonl（客户端墙钟，与 agent 同口径）",
        "status": status,
        "total_time_s": round(batch_time, 2) if n_batches else None,
        "throughput_qps": round(total / batch_time, 4) if batch_time > 0 else None,
        "latency_ms": latency_stats(latencies),
        "success_rate": round(status.get("success", 0) / total, 4) if total else None,
    }


def parse_out(files: list) -> dict:
    """解析旧格式 .out 文件（CLI "It costs"：发送→第一批结果，客户端墙钟）。"""
    costs: list[float] = []
    errors = empties = 0
    for f in files:
        try:
            text = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        costs += [float(m) for m in COST_RE.findall(text)]
        errors += text.count("Msg:")
        empties += text.count("Empty set")
    s = sorted(costs)
    n = len(s)
    return {
        "executions": n,
        "format": "legacy-out（CLI 客户端墙钟：发送→第一批结果）",
        "error_lines": errors,
        "empty_sets": empties,
        "total_time_s": round(sum(s), 4) if n else None,
        "throughput_qps": round(n / sum(s), 4) if n else None,
        "latency_s": {
            "samples": n,
            "avg": round(sum(s) / n, 6) if n else None,
            "min": s[0] if n else None,
            "max": s[-1] if n else None,
            "p50": percentile(s, 50) if n else None,
            "p95": percentile(s, 95) if n else None,
            "p99": percentile(s, 99) if n else None,
        },
        "success_rate": round((n - errors) / n, 4) if n else None,
    }


def print_jsonl(r: dict) -> None:
    status = r["status"]
    l = r["latency_ms"]
    print(f"查询执行数: {r['executions']} | 口径: {r['format']}")
    print(f"状态: 成功 {status['success']} / 失败 {status['failed']} / "
          f"超时 {status['timeout']} / 未知 {status['unknown']}（成功率 {r['success_rate']}）")
    print(f"总用时（客户端墙钟）: {r['total_time_s']}s | 吞吐: {r['throughput_qps']} 查询/秒")
    print(f"延迟: avg {l['avg_ms']}ms | min {l['min_ms']}ms | max {l['max_ms']}ms | "
          f"P50 {l['p50_ms']}ms | P95 {l['p95_ms']}ms | P99 {l['p99_ms']}ms")


def print_out(r: dict) -> None:
    l = r["latency_s"]
    print(f"查询执行数: {r['executions']} | 口径: {r['format']}")
    print(f"报错行: {r['error_lines']} | 空结果: {r['empty_sets']} | 成功率: {r['success_rate']}")
    print(f"总用时: {r['total_time_s']}s | 吞吐: {r['throughput_qps']} 查询/秒")
    print(f"延迟: avg {l['avg']}s | min {l['min']}s | max {l['max']}s | "
          f"P50 {l['p50']}s | P95 {l['p95']}s | P99 {l['p99']}s")


def main(argv: list) -> int:
    as_json = "--json" in argv
    files = [a for a in argv if a != "--json"]
    if not files:
        print("用法: aggregate_native.py <结果文件...> [--json]", file=sys.stderr)
        return 2
    jsonl_files = [f for f in files if f.endswith(".jsonl")]
    out_files = [f for f in files if not f.endswith(".jsonl")]
    if jsonl_files and out_files:
        # 混合输入只统计新格式（旧 .out 请单独统计），避免两把尺子混算
        print(f"警告: 输入混有旧 .out（{len(out_files)} 个），本次只统计 .jsonl",
              file=sys.stderr)
    if jsonl_files:
        r = parse_jsonl(jsonl_files)
        if as_json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print_jsonl(r)
        return 0
    r = parse_out(out_files)
    if as_json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print_out(r)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
