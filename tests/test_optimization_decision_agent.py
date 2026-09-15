"""Optimization Decision Agent 单元测试。

覆盖：输入契约、有界策略空间、维度适用性、风险门控与回退、
数据驱动的决策（选择率/分区信息/扫描估计）、历史证据相似度与加权、
置信度不虚高、确定性、schema 契约、不越权声明。
运行：pytest tests/test_optimization_decision_agent.py -v
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.query_analysis import analyze  # noqa: E402
from mataof.agents.optimization_decision import decide  # noqa: E402

REQUIRED_TOP_KEYS = {
    "query_id", "decision", "selected_strategy", "evidence",
    "overall_confidence", "fallback_strategy", "decision_status",
}
REQUIRED_DIMS = ("time_pruning", "filter_order", "aggregation_placement")


def _check_schema(r: dict) -> None:
    assert set(REQUIRED_TOP_KEYS) <= set(r.keys())
    for d in REQUIRED_DIMS:
        assert set(r["decision"][d].keys()) == {"strategy", "reason", "confidence"}
    assert 0.0 <= r["overall_confidence"] <= 1.0
    json.dumps(r)


def _decide(query: str, **kwargs) -> dict:
    """便捷入口：先用 Query Analysis Agent 分析，再决策（真实流水线）。"""
    analysis = analyze(query, query_id=kwargs.pop("query_id", "q_test"))
    return decide({"query_analysis": analysis, **kwargs})


def _record(record_id, query_type, span, range_level, device_count=1,
            non_time=0, has_agg=False, has_group_by=False, has_window=False,
            strategy=None, latency=10.0, db=None, sys_state=None, record_time=None):
    return {
        "record_id": record_id,
        "record_time": record_time,
        "query_features": {
            "query_type": query_type,
            "time": {"has_time_filter": True, "time_span": span, "range_level": range_level},
            "device": {"device_count": device_count, "multi_device": device_count > 1},
            "filter": {"non_time_filter_count": non_time},
            "aggregation": {"has_aggregation": has_agg, "has_group_by": has_group_by,
                            "has_window": has_window, "functions": ["avg"] if has_agg else []},
        },
        "database_state": db or {},
        "system_state": sys_state or {},
        "strategy": strategy or {},
        "execution_feedback": {"latency_ms": latency},
    }


# ---------------------------------------------------------------------------
# 输入契约
# ---------------------------------------------------------------------------

def test_invalid_input_no_analysis():
    r = decide({"query_id": "q1"})
    _check_schema(r)
    assert r["decision_status"] == "invalid_input"
    assert r["overall_confidence"] == 0.0


def test_decision_requires_more_than_sql():
    # 仅凭 SQL 文本不可决策：query_analysis 缺失 → invalid_input
    r = decide({"query_id": "q2", "query": "SELECT s1 FROM root.sg1.d1 WHERE time >= 1"})
    assert r["decision_status"] == "invalid_input"


def test_dimension_not_applicable():
    # 无聚合、无非时间谓词的点查询：filter_order / aggregation_placement 不适用
    r = _decide("SELECT s1 FROM root.sg1.d1 WHERE time = 1640966450000")
    _check_schema(r)
    assert r["decision"]["time_pruning"]["strategy"] != "not_applicable"
    assert r["decision"]["filter_order"]["strategy"] == "not_applicable"
    assert r["decision"]["aggregation_placement"]["strategy"] == "not_applicable"


# ---------------------------------------------------------------------------
# Time Pruning 决策
# ---------------------------------------------------------------------------

def test_time_pruning_narrow_with_partition_info():
    db = {"partition_info": {"total_partitions": 128, "covered_partitions": 2}}
    r = _decide(
        "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000",
        database_state=db,
    )
    assert r["decision"]["time_pruning"]["strategy"] == "partition_pruning"
    assert "partition_pruning" in r["selected_strategy"]["strategy_id"]
    assert any("partition_info" in s for s in r["evidence"]["database_state"])
    # 参数只包含事实性取值
    params = r["selected_strategy"]["strategy_parameters"]
    assert params["time_pruning"]["start_time"] == 1640966405000


def test_time_pruning_cold_start_prefers_full_scan():
    # wide 范围 + 无分区信息 + 无历史：partition_pruning 门控不通过 → full_scan
    r = _decide("SELECT s1 FROM root.sg1.d1 WHERE time >= 0 AND time <= 4102444800000")
    assert r["decision"]["time_pruning"]["strategy"] == "full_scan"
    assert any("partition_pruning" in n for n in r["notes"])  # 排除记录可追踪


def test_time_pruning_fallback_when_all_excluded():
    # 候选只有 partition_pruning，wide 范围 + 无数据 → 全部排除 → 回退 baseline
    r = _decide(
        "SELECT s1 FROM root.sg1.d1 WHERE time >= 0 AND time <= 4102444800000",
        candidate_strategies={"time_pruning": ["partition_pruning"],
                              "filter_order": [], "aggregation_placement": []},
    )
    assert r["decision"]["time_pruning"]["strategy"] == "full_scan"  # 内置 baseline
    assert r["decision"]["time_pruning"]["confidence"] == 0.3        # 回退置信度不虚高
    assert "full_scan" in r["fallback_strategy"]


def test_chunk_level_high_risk_gate_needs_history():
    db = {"physical_organization": {"chunk_level": {"total_chunks": 1000, "covered_chunks": 5}}}
    # 窄范围 + chunk 信息齐备，但无历史证据（高风险要求 ≥2 条）→ chunk 被门控排除
    r = _decide(
        "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000",
        database_state=db,
    )
    assert r["decision"]["time_pruning"]["strategy"] != "chunk_level_filtering"
    assert any("chunk_level_filtering" in n for n in r["notes"])


def test_chunk_level_selected_with_history():
    db = {"physical_organization": {"chunk_level": {"total_chunks": 1000, "covered_chunks": 5}}}
    history = [
        _record("h1", "range_query", 25000, "narrow",
                strategy={"time_pruning": "chunk_level_filtering"}, latency=5.0, db=db),
        _record("h2", "range_query", 30000, "narrow",
                strategy={"time_pruning": "chunk_level_filtering"}, latency=6.0, db=db),
        _record("h3", "range_query", 25000, "narrow",
                strategy={"time_pruning": "full_scan"}, latency=50.0, db=db),
    ]
    r = _decide(
        "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000",
        database_state=db, historical_records=history,
    )
    assert r["decision"]["time_pruning"]["strategy"] == "chunk_level_filtering"
    assert r["decision"]["time_pruning"]["confidence"] <= 0.7  # 高风险置信度上限
    assert "历史证据" in r["decision"]["time_pruning"]["reason"]


# ---------------------------------------------------------------------------
# Filter Order 决策
# ---------------------------------------------------------------------------

def test_filter_order_no_data_prefers_time_first():
    # 无选择率数据：不得假设某条件选择性更高 → device_tag_first 门控不通过 → time_first
    r = _decide(
        "SELECT s_1666 FROM root.db800.g_0.d_0 WHERE time >= 1640966400000 "
        "AND time <= 1640966650000 AND root.db800.g_0.d_0.s_1666 > -5 "
        "AND root.db800.g_0.d_0.s_1766 > -5"
    )
    assert r["decision"]["filter_order"]["strategy"] == "time_first"
    assert any("device_tag_first" in n for n in r["notes"])


def test_filter_order_with_selectivity_prefers_device_tag():
    db = {"statistics": {"selectivity": {
        "time": 0.4, "root.db800.g_0.d_0.s_1666": 0.02, "root.db800.g_0.d_0.s_1766": 0.05,
    }}}
    r = _decide(
        "SELECT s_1666 FROM root.db800.g_0.d_0 WHERE time >= 1640966400000 "
        "AND time <= 1640966650000 AND root.db800.g_0.d_0.s_1666 > -5 "
        "AND root.db800.g_0.d_0.s_1766 > -5",
        database_state=db,
    )
    assert r["decision"]["filter_order"]["strategy"] == "device_tag_first"
    assert "选择率" in r["decision"]["filter_order"]["reason"]


def test_unknown_candidate_names_rejected():
    r = _decide(
        "SELECT s1 FROM root.sg1.d1 WHERE time >= 1 AND time <= 2 AND s1 > 10 AND t1 = 'v'",
        candidate_strategies={"time_pruning": ["magic_strategy", "full_scan"],
                              "filter_order": ["time_first"],
                              "aggregation_placement": []},
    )
    assert "magic_strategy" not in r["selected_strategy"]["strategy_id"]
    assert any("magic_strategy" in n for n in r["notes"])


# ---------------------------------------------------------------------------
# Aggregation Placement 决策
# ---------------------------------------------------------------------------

def test_scan_level_gate_without_support():
    # 无窄范围/扫描估计/窗口 → scan_level 门控不通过；无 GROUP BY → intermediate 也不通过
    r = _decide(
        "SELECT COUNT(s_0) FROM root.test.d_0 WHERE time >= 0 AND time <= 4102444800000",
        candidate_strategies={"aggregation_placement": ["scan_level_aggregation",
                                                         "intermediate_level_aggregation",
                                                         "final_level_aggregation"]},
    )
    assert r["decision"]["aggregation_placement"]["strategy"] == "final_level_aggregation"
    assert any("scan_level_aggregation" in n for n in r["notes"])


def test_scan_level_selected_narrow_window():
    r = _decide(
        "SELECT AVG(s_0) FROM root.test.d_0 "
        "GROUP BY ([1640966405000, 1640976405000), 1h)"
    )
    assert r["decision"]["aggregation_placement"]["strategy"] == "scan_level_aggregation"
    assert "窗口聚合" in r["decision"]["aggregation_placement"]["reason"]


def test_intermediate_level_selected_with_group_by():
    # GROUP BY（非窗口）→ intermediate 有结构依据；scan_level 无支撑 → intermediate 胜出
    r = _decide(
        "SELECT COUNT(s1) FROM root.sg1.d1 GROUP BY LEVEL = 1",
        candidate_strategies={"aggregation_placement": ["intermediate_level_aggregation",
                                                         "final_level_aggregation"]},
    )
    assert r["decision"]["aggregation_placement"]["strategy"] == "intermediate_level_aggregation"


# ---------------------------------------------------------------------------
# 历史证据
# ---------------------------------------------------------------------------

def test_history_similarity_filters_dissimilar_records():
    history = [
        _record("dissimilar", "window_aggregation_query", 3_600_000_000, "wide",
                has_agg=True, has_group_by=True, has_window=True,
                strategy={"time_pruning": "partition_pruning"}, latency=1.0),  # 完全不同上下文
    ]
    r = _decide(
        "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000",
        historical_records=history,
    )
    used = [h for h in r["evidence"]["historical_records"] if h.get("record_id")]
    assert used == []  # 相似度低于阈值 → 不参与
    summary = next(h["summary"] for h in r["evidence"]["historical_records"] if "summary" in h)
    assert summary["below_similarity_threshold"] == 1


def test_history_evidence_boosts_matching_strategy():
    history = [
        _record("h1", "range_query", 25000, "narrow",
                strategy={"time_pruning": "partition_pruning"}, latency=10.0),
        _record("h2", "range_query", 30000, "narrow",
                strategy={"time_pruning": "full_scan"}, latency=80.0),
    ]
    # wide 范围（无 narrow 上下文加分），但历史证据 ≥1 条 → 门控通过
    r = _decide(
        "SELECT s1 FROM root.sg1.d1 WHERE time >= 0 AND time <= 4102444800000",
        historical_records=history,
    )
    assert r["decision"]["time_pruning"]["strategy"] == "partition_pruning"
    assert any(h["record_id"] == "h1" for h in r["evidence"]["historical_records"]
               if h.get("record_id"))


def test_invalid_history_records_ignored():
    history = [
        {"record_id": "bad1", "strategy": {}, "execution_feedback": {"latency_ms": 1}},
        {"record_id": "bad2", "query_features": {}, "strategy": {},
         "execution_feedback": {"latency_ms": "fast"}},  # 非数值延迟
    ]
    r = _decide(
        "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000",
        historical_records=history,
    )
    summary = next(h["summary"] for h in r["evidence"]["historical_records"] if "summary" in h)
    assert summary["invalid"] == 2


# ---------------------------------------------------------------------------
# 置信度与安全
# ---------------------------------------------------------------------------

def test_confidence_not_inflated_without_evidence():
    r = _decide("SELECT s1 FROM root.sg1.d1 WHERE time >= 0 AND time <= 4102444800000")
    assert r["overall_confidence"] <= 0.6  # 无数据库状态、无历史 → 上限 0.6
    assert r["decision"]["time_pruning"]["confidence"] <= 0.6


def test_confidence_higher_with_evidence():
    db = {"partition_info": {"total_partitions": 128, "covered_partitions": 2}}
    r_none = _decide(
        "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000"
    )
    r_rich = _decide(
        "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000",
        database_state=db,
        historical_records=[
            _record("h1", "range_query", 25000, "narrow",
                    strategy={"time_pruning": "partition_pruning"}, latency=8.0, db=db),
        ],
    )
    assert r_rich["decision"]["time_pruning"]["confidence"] >= \
        r_none["decision"]["time_pruning"]["confidence"]


def test_system_state_high_load_adjusts_scores():
    r = _decide(
        "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000",
        system_state={"cpu_utilization": 0.9, "memory_utilization": 0.5,
                      "io_utilization": 0.85},
    )
    assert r["decision"]["time_pruning"]["strategy"] == "partition_pruning"
    assert "高负载" in r["decision"]["time_pruning"]["reason"]
    assert any("load_level=high" in s for s in r["evidence"]["system_state"])


def test_no_optimality_claims():
    queries = [
        "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000",
        "SELECT AVG(s_0) FROM root.test.d_0 GROUP BY ([0, 1000000), 1h)",
        "SELECT s1 FROM root.sg1.d1 WHERE s1 > 10 AND t1 = 'v'",
    ]
    for q in queries:
        r = _decide(q)
        for d in REQUIRED_DIMS:
            reason = r["decision"][d]["reason"]
            for bad in ("全局最优", "绝对最优", "最优策略"):
                assert bad not in reason, f"{d} 出现越权声明：{reason}"


# ---------------------------------------------------------------------------
# 通用契约
# ---------------------------------------------------------------------------

def test_determinism():
    q = "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000"
    inp = {
        "query_analysis": analyze(q, query_id="x"),
        "database_state": {"partition_info": {"total_partitions": 128, "covered_partitions": 2}},
        "historical_records": [
            _record("h1", "range_query", 25000, "narrow",
                    strategy={"time_pruning": "partition_pruning"}, latency=8.0),
        ],
    }
    assert decide(inp) == decide(inp)


def test_flat_candidate_list():
    r = _decide(
        "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000",
        candidate_strategies=["full_scan", "partition_pruning"],
    )
    assert r["decision"]["time_pruning"]["strategy"] in ("full_scan", "partition_pruning")


def test_invalid_baseline_falls_back_to_default():
    r = _decide(
        "SELECT s1 FROM root.sg1.d1 WHERE time >= 0 AND time <= 4102444800000",
        candidate_strategies={"time_pruning": ["partition_pruning"],
                              "filter_order": [], "aggregation_placement": []},
        baseline_strategy={"time_pruning": "not_a_strategy"},
    )
    assert r["decision"]["time_pruning"]["strategy"] == "full_scan"
    assert any("baseline" in n for n in r["notes"])


def test_partial_fallback_status():
    # 时间维度回退、过滤维度成功 → partial_fallback
    r = _decide(
        "SELECT s1 FROM root.sg1.d1 WHERE time >= 0 AND time <= 4102444800000 "
        "AND s1 > 10 AND s2 < 20",
        candidate_strategies={"time_pruning": ["partition_pruning"],
                              "filter_order": ["time_first"],
                              "aggregation_placement": []},
    )
    assert r["decision_status"] == "partial_fallback"
    assert r["decision"]["filter_order"]["strategy"] == "time_first"
    assert "time_pruning=full_scan" in r["fallback_strategy"]
