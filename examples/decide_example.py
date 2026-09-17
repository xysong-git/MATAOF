"""Optimization Decision Agent 使用示例（含与 Query Analysis Agent 的流水线串联）。

运行：python3 examples/decide_example.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.query_analysis import analyze  # noqa: E402
from mataof.agents.optimization_decision import decide  # noqa: E402


def run_pipeline(query: str, query_id: str, **extra_inputs) -> dict:
    """分析 → 决策 的完整流水线。"""
    analysis = analyze(query, query_id=query_id)
    return decide({"query_analysis": analysis, **extra_inputs})


def main() -> None:
    # 场景 1：窄范围 + 分区信息 → 期望 partition_pruning
    print("=" * 20, "场景 1：窄范围 + 分区信息", "=" * 20)
    r1 = run_pipeline(
        "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000",
        "s1",
        database_state={"partition_info": {"total_partitions": 128, "covered_partitions": 2}},
    )
    print(json.dumps(r1, ensure_ascii=False, indent=2))

    # 场景 2：宽范围 + 无任何上下文 → 安全保守选择
    print("=" * 20, "场景 2：宽范围 + 冷启动", "=" * 20)
    r2 = run_pipeline(
        "SELECT s1 FROM root.sg1.d1 WHERE time >= 0 AND time <= 4102444800000", "s2"
    )
    print(json.dumps(r2, ensure_ascii=False, indent=2))

    # 场景 3：选择率数据驱动 Filter Order + 历史证据驱动 Time Pruning
    print("=" * 20, "场景 3：选择率 + 历史证据", "=" * 20)
    query3 = (
        "SELECT s_1666 FROM root.db800.g_0.d_0 WHERE time >= 1640966400000 "
        "AND time <= 1640966650000 AND root.db800.g_0.d_0.s_1666 > -5 "
        "AND root.db800.g_0.d_0.s_1766 > -5"
    )
    history = [
        {
            "record_id": "h1",
            "record_time": 1640967000000,
            "query_features": {
                "query_type": "predicate_filter_query",
                "time": {"has_time_filter": True, "time_span": 25000, "range_level": "narrow"},
                "device": {"device_count": 1, "multi_device": False},
                "filter": {"non_time_filter_count": 2},
                "aggregation": {"has_aggregation": False, "has_group_by": False,
                                "has_window": False, "functions": []},
            },
            "database_state": {},
            "system_state": {},
            "strategy": {"time_pruning": "partition_pruning",
                         "filter_order": "time_first",
                         "aggregation_placement": "not_applicable"},
            "execution_feedback": {"latency_ms": 12.0},
        },
        {
            "record_id": "h2",
            "record_time": 1640968000000,
            "query_features": {
                "query_type": "predicate_filter_query",
                "time": {"has_time_filter": True, "time_span": 30000, "range_level": "narrow"},
                "device": {"device_count": 1, "multi_device": False},
                "filter": {"non_time_filter_count": 2},
                "aggregation": {"has_aggregation": False, "has_group_by": False,
                                "has_window": False, "functions": []},
            },
            "database_state": {},
            "system_state": {},
            "strategy": {"time_pruning": "full_scan",
                         "filter_order": "time_first",
                         "aggregation_placement": "not_applicable"},
            "execution_feedback": {"latency_ms": 60.0},
        },
    ]
    r3 = run_pipeline(
        query3, "s3",
        database_state={"statistics": {"selectivity": {
            "time": 0.4,
            "root.db800.g_0.d_0.s_1666": 0.02,
            "root.db800.g_0.d_0.s_1766": 0.05,
        }}},
        historical_records=history,
        reference_time=1640969000000,
    )
    print(json.dumps(r3, ensure_ascii=False, indent=2))

    # 场景 4：窗口聚合 → scan-level 下推候选
    print("=" * 20, "场景 4：窗口聚合", "=" * 20)
    r4 = run_pipeline(
        "SELECT AVG(s_0) FROM root.test.d_0 GROUP BY ([1640966405000, 1640976405000), 1h)",
        "s4",
        system_state={"cpu_utilization": 0.9, "memory_utilization": 0.4,
                      "io_utilization": 0.8},
    )
    print(json.dumps(r4, ensure_ascii=False, indent=2))

    # 场景 5：混合模型 —— 候选携带等价 SQL，决策输出可直接执行的 SQL
    print("=" * 20, "场景 5：候选携带等价 SQL（混合模型）", "=" * 20)
    query5 = (
        "SELECT s_1666 FROM root.db800.g_0.d_0 "
        "WHERE root.db800.g_0.d_0.s_1666 > -5 AND root.db800.g_0.d_0.s_1766 > -5 "
        "AND time >= 1640966400000 AND time <= 1640966650000"
    )
    time_first_sql = (
        "SELECT s_1666 FROM root.db800.g_0.d_0 "
        "WHERE time >= 1640966400000 AND time <= 1640966650000 "
        "AND root.db800.g_0.d_0.s_1666 > -5 AND root.db800.g_0.d_0.s_1766 > -5"
    )
    r5 = run_pipeline(
        query5, "s5",
        candidate_strategies={
            "filter_order": [
                {"strategy": "time_first", "equivalent_sql": time_first_sql},
                "device_tag_first",
            ],
            "time_pruning": ["full_scan", "partition_pruning"],
            "aggregation_placement": [],
        },
    )
    print("选中策略：", r5["selected_strategy"]["strategy_id"])
    print("可直接执行的等价 SQL：", r5["selected_strategy"]["equivalent_sql"])


if __name__ == "__main__":
    main()
