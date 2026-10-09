#!/usr/bin/env python3
"""聚合原生 IoTDB baseline 输出（start-cli.sh 的 .out 文件）为统计报告。

数据来源：每个 .out 文件中 CLI 原生打印的 "It costs X.XXXs"（服务端执行耗时）。
纯 stdlib，不依赖任何 mataof 模块。

用法：
  python3 scripts/aggregate_native.py results_baseline/*.out [--json]
"""

from __future__ import annotations

import json
import re
import sys

COST_RE = re.compile(r"It costs ([\d.]+)s")


def percentile(sorted_values: list, p: float):
    if not sorted_values:
        return None
    idx = max(0, int(p / 100 * len(sorted_values) + 0.999999) - 1)
    return sorted_values[idx]


def parse(files: list) -> dict:
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


def main(argv: list) -> int:
    args = [a for a in argv if a != "--json"]
    as_json = "--json" in argv
    files = args
    if not files:
        print("用法: aggregate_native.py <out文件...> [--json]", file=sys.stderr)
        return 2
    r = parse(files)
    if as_json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    l = r["latency_s"]
    print(f"查询执行数: {r['executions']} | 报错行: {r['error_lines']} | 空结果: {r['empty_sets']}")
    print(f"总用时: {r['total_time_s']}s | 吞吐: {r['throughput_qps']} 查询/秒（服务端执行口径）")
    print(f"成功率: {r['success_rate']}（以报错行计）")
    print(f"延迟: avg {l['avg']}s | min {l['min']}s | max {l['max']}s | "
          f"P50 {l['p50']}s | P95 {l['p95']}s | P99 {l['p99']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
