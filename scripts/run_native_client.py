#!/usr/bin/env python3
"""原生 baseline 客户端计时执行（与 agent 模式同一把尺子）。

作用：不走任何 Agent 路径，用与 agent 模式**完全相同**的 IoTDB 客户端与计时代码
（mataof.executors.iotdb.IoTDBExecutor，measure_baseline=False）直发原始 SQL，
逐条记录客户端墙钟 response_time_ms（发送 → 完整消费结果集）。
采样逻辑与 agent 模式共用 sample_items（同种子 → 同抽样，实验可复现）。

输出：每个查询文件一个 .jsonl（每行一条查询记录 + 末尾一条 batch 行），
供 aggregate_native.py 解析。

用法：
  python3 scripts/run_native_client.py --file q1.sql --out out.jsonl \
      [--per-file-limit N] [--sample seq|random] [--seed 42] \
      [--host 127.0.0.1] [--port 6667] [--user root] [--password root]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

# 路径引导：允许脚本独立运行时 import 项目包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.executors.iotdb import IoTDBExecutor  # noqa: E402
from mataof.runner import sample_items, split_sql_statements  # noqa: E402


def run_file(executor: IoTDBExecutor, path: str, out_path: str,
             per_file_limit, sample_mode: str, seed: int) -> int:
    """执行一个查询文件，逐条写 .jsonl（查询行 + batch 行），返回执行条数。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    statements, sampling = sample_items(
        split_sql_statements(text), per_file_limit or None, sample_mode,
        random.Random(seed), seed)
    name = os.path.basename(path)
    t0 = time.time()
    n = 0
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as out:
        for i, stmt in enumerate(statements):
            result = executor.execute(stmt, f"{name}-{i:04d}", {})
            row = {
                "query_id": f"{name}-{i:04d}",
                "query": stmt,
                "execution_status": result.execution_status,
                "response_time_ms": result.metrics.get("response_time_ms"),
                "failure_reason": result.failure_reason,
                "cpu_utilization": result.metrics.get("cpu_utilization"),
                "cpu_utilization_peak": result.metrics.get("cpu_utilization_peak"),
                "memory_utilization": result.metrics.get("memory_utilization"),
                "memory_utilization_peak": result.metrics.get("memory_utilization_peak"),
                "sampling": sampling,
            }
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
        out.write(json.dumps({"batch": True, "query_id": None,
                              "batch_total_time_s": round(time.time() - t0, 2),
                              "batch_queries": n}, ensure_ascii=False) + "\n")
    return n


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file", required=True, help="查询文件（.sql 按分号拆分）")
    p.add_argument("--out", required=True, help="输出 .jsonl 路径")
    p.add_argument("--per-file-limit", type=int, default=None,
                   help="每个文件取样条数（默认全量；与 agent 模式同语义）")
    p.add_argument("--sample", choices=["seq", "random"], default="seq")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6667)
    p.add_argument("--user", default="root")
    p.add_argument("--password", default="root")
    args = p.parse_args(argv)

    executor = IoTDBExecutor(host=args.host, port=args.port, user=args.user,
                             password=args.password, measure_baseline=False)
    try:
        n = run_file(executor, args.file, args.out, args.per_file_limit,
                     args.sample, args.seed)
    finally:
        executor.close()
    print(f"[native] {os.path.basename(args.file)}: {n} 条 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
