"""Execution Monitoring Agent 使用示例（含三 Agent 流水线串联演示）。

运行：python3 examples/monitor_example.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.query_analysis import analyze  # noqa: E402
from mataof.agents.optimization_decision import decide  # noqa: E402
from mataof.agents.execution_monitoring import monitor  # noqa: E402


def main() -> None:
    # ---- 完整流水线：分析 → 决策 → 执行 → 监控反馈 ----
    query = "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000"

    analysis = analyze(query, query_id="q_demo")
    decision = decide({
        "query_analysis": analysis,
        "database_state": {"partition_info": {"total_partitions": 128, "covered_partitions": 2}},
    })
    strategy_id = decision["selected_strategy"]["strategy_id"]

    # 执行层返回的实测指标与 baseline（真实环境采集）
    feedback = monitor({
        "query_id": "q_demo",
        "strategy_id": strategy_id,
        "execution_status": "success",
        "metrics": {
            "response_time_ms": 12.3, "p95_latency_ms": 14.0, "p99_latency_ms": 15.5,
            "throughput": 320.5, "cpu_utilization": 0.42,
            "memory_utilization": 0.35, "io_throughput": 120.0,
        },
        "baseline_metrics": {
            "response_time_ms": 20.0, "p95_latency_ms": 24.0, "p99_latency_ms": 26.0,
            "throughput": 250.0, "cpu_utilization": 0.45,
            "memory_utilization": 0.38, "io_throughput": 130.0,
        },
    })

    print("== 流水线：分析 → 决策 → 执行 → 监控 ==")
    print("策略：", strategy_id)
    print("监控反馈：")
    print(json.dumps(feedback, ensure_ascii=False, indent=2))

    # ---- 场景 2：执行失败（事实采集，不猜测指标）----
    print("== 场景 2：执行失败 ==")
    r2 = monitor({
        "query_id": "q2",
        "strategy_id": "time_pruning=chunk_level_filtering|...",
        "execution_status": "failed",
        "failure_reason": "chunk 元数据缺失导致执行错误",
    })
    print(json.dumps(r2, ensure_ascii=False, indent=2))

    # ---- 场景 3：无 baseline（不生成比较结果）----
    print("== 场景 3：无 baseline ==")
    r3 = monitor({
        "query_id": "q3",
        "strategy_id": "time_pruning=full_scan|...",
        "execution_status": "success",
        "metrics": {"response_time_ms": 8.0, "cpu_utilization": 0.3},
    })
    print(json.dumps(r3, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
