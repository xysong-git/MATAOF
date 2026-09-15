"""Query Analysis Agent 单元测试。

覆盖六类分析任务、strict 限制（不虚构、unknown 标记、不越权决策）与输出契约。
运行：pytest tests/test_query_analysis_agent.py -v
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.query_analysis import analyze  # noqa: E402

REQUIRED_TOP_KEYS = {
    "query_id", "query", "query_type", "query_features",
    "optimization_relevant_features", "unknown_features",
    "analysis_confidence", "schema_version",
}
REQUIRED_FEATURE_SECTIONS = {"time", "device", "filter", "aggregation", "scan"}
REQUIRED_RELEVANCE_KEYS = {"time_pruning", "filter_order", "aggregation_placement"}


def _check_schema(result: dict) -> None:
    assert set(REQUIRED_TOP_KEYS) <= set(result.keys()), "缺少顶层字段"
    assert set(REQUIRED_FEATURE_SECTIONS) <= set(result["query_features"].keys())
    assert set(REQUIRED_RELEVANCE_KEYS) <= set(result["optimization_relevant_features"].keys())
    assert 0.0 <= result["analysis_confidence"] <= 1.0
    json.dumps(result)  # JSON 可序列化


# ---------------------------------------------------------------------------
# 任务 1：查询类型识别
# ---------------------------------------------------------------------------

def test_point_query():
    r = analyze("SELECT s_1666 FROM root.db800.g_0.d_0 WHERE time = 1640966450000", query_id="p1")
    _check_schema(r)
    assert r["query_type"] == "point_query"
    t = r["query_features"]["time"]
    assert t["has_time_filter"] is True
    assert t["start_time"] == 1640966450000 and t["end_time"] == 1640966450000
    assert t["time_span"] == 0
    assert t["range_level"] == "narrow"
    assert t["time_condition_form"] == "equality"


def test_range_query_epoch():
    r = analyze("SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000")
    _check_schema(r)
    assert r["query_type"] == "range_query"
    t = r["query_features"]["time"]
    assert t["start_time"] == 1640966405000 and t["end_time"] == 1640970000000
    assert t["time_span"] == 1640970000000 - 1640966405000
    # 3595000 ms < 1h → narrow
    assert t["range_level"] == "narrow"


def test_range_query_string_tz():
    r = analyze(
        "SELECT s1 FROM root.sg1.d1 WHERE time >= '2024-01-01T00:00:00+08:00' "
        "AND time <= '2024-01-02T00:00:00+08:00'"
    )
    _check_schema(r)
    t = r["query_features"]["time"]
    assert t["start_time"] == 1704038400000   # 2024-01-01T00:00:00+08:00
    assert t["end_time"] == 1704124800000
    assert t["time_span"] == 86_400_000
    # 阈值定义：span < 1h → narrow；1h <= span < 1d → medium；span >= 1d → wide
    assert t["range_level"] == "wide"         # 恰好 1d → wide
    assert r["query_type"] == "range_query"


def test_predicate_filter_query():
    r = analyze(
        "SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1 "
        "WHERE time >= 1640966400000 AND time <= 1640966650000 "
        "AND root.db800.g_0.d_0.s_1666 > -5 AND root.db800.g_0.d_0.s_1766 > -5"
    )
    _check_schema(r)
    assert r["query_type"] == "predicate_filter_query"
    f = r["query_features"]["filter"]
    assert f["filter_count"] == 4
    assert f["type_counts"]["time"] == 2
    assert f["type_counts"]["value"] == 2
    assert f["combination"]["op"] == "and"


def test_window_aggregation_query():
    r = analyze(
        "SELECT AVG(s_0) FROM root.test.d_0 "
        "GROUP BY ([1640966405000, 1640976405000), 1h)"
    )
    _check_schema(r)
    assert r["query_type"] == "window_aggregation_query"
    a = r["query_features"]["aggregation"]
    assert a["has_aggregation"] is True
    assert a["aggregation_functions"][0]["function"] == "avg"
    assert a["has_group_by"] is True
    assert a["has_window"] is True
    assert a["group_by_details"]["type"] == "time_window"
    w = a["group_by_details"]["window"]
    assert w["start_time"] == 1640966405000 and w["end_time"] == 1640976405000
    assert w["interval"] == "1h" and w["interval_ms"] == 3_600_000
    assert w["sliding_step"] is None


def test_sliding_window():
    r = analyze("SELECT AVG(s1) FROM root.sg1.d1 GROUP BY ([0, 4102444800000), 1h, 30m)")
    _check_schema(r)
    a = r["query_features"]["aggregation"]
    assert r["query_type"] == "window_aggregation_query"
    assert a["group_by_details"]["window"]["sliding_step"] == "30m"
    assert a["group_by_details"]["window"]["sliding_step_ms"] == 1_800_000


def test_group_by_time_function():
    r = analyze("SELECT AVG(s1) FROM root.sg1.d1 GROUP BY TIME(1h)")
    _check_schema(r)
    assert r["query_type"] == "window_aggregation_query"
    a = r["query_features"]["aggregation"]
    assert a["group_by_details"]["type"] == "time_function"
    assert a["group_by_details"]["interval"] == "1h"


def test_union_is_complex():
    r = analyze(
        "SELECT s_1666 FROM root.dbm.g_0.d_0, root.dbm.g_0.d_1 WHERE time >= 1 AND time <= 2 "
        "UNION SELECT s_1666 FROM root.dbm.g_0.d_0, root.dbm.g_0.d_1 WHERE time >= 3 AND time <= 4"
    )
    _check_schema(r)
    assert r["query_type"] == "complex_composite_query"
    # UNION 各分支时间范围不一致 → 时间范围 unknown，不得猜测
    t = r["query_features"]["time"]
    assert t["has_time_filter"] is True
    assert t["start_time"] is None and t["end_time"] is None
    assert any("UNION" in u for u in r["unknown_features"])


def test_in_subquery_is_complex():
    r = analyze("SELECT s1 FROM root.sg1.d1 WHERE time IN (SELECT time FROM root.sg1.d1 WHERE s1 > 100)")
    _check_schema(r)
    assert r["query_type"] == "complex_composite_query"
    t = r["query_features"]["time"]
    assert t["has_time_filter"] is True
    assert t["time_condition_form"] == "in_subquery"
    assert t["start_time"] is None


def test_agg_with_predicates_is_complex():
    r = analyze(
        "SELECT count(s_333), count(s_433) FROM root.dbs.g_0.d_0, root.dbs.g_0.d_1 "
        "WHERE time >= 1640966400000 AND time <= 1640966650000 "
        "AND root.dbs.g_0.d_0.s_333 > -5 AND root.dbs.g_0.d_0.s_433 > -5"
    )
    _check_schema(r)
    assert r["query_type"] == "complex_composite_query"
    a = r["query_features"]["aggregation"]
    assert a["has_aggregation"] is True
    assert len(a["aggregation_functions"]) == 2


def test_agg_with_range_only_is_complex():
    r = analyze(
        "SELECT count(s_333), count(s_433) FROM root.dbs.g_0.d_0, root.dbs.g_0.d_1 "
        "WHERE time >= 1640966400000 AND time <= 1640966650000"
    )
    _check_schema(r)
    assert r["query_type"] == "complex_composite_query"


def test_pure_aggregation_no_filter_is_unknown():
    r = analyze("SELECT COUNT(s_0) FROM root.test.d_0")
    _check_schema(r)
    assert r["query_type"] == "unknown"
    assert any("聚合" in u for u in r["unknown_features"])


def test_no_filter_select_is_unknown():
    r = analyze("SELECT s1 FROM root.sg1.d1")
    _check_schema(r)
    assert r["query_type"] == "unknown"
    assert any("访问意图" in u for u in r["unknown_features"])


def test_show_command_is_unknown():
    r = analyze("SHOW DEVICES root.test.* LIMIT 20")
    _check_schema(r)
    assert r["query_type"] == "unknown"
    assert any("非 SELECT" in u for u in r["unknown_features"])


def test_parse_error_is_unknown():
    r = analyze("SELECT s1 FROMM root.sg1.d1")
    _check_schema(r)
    assert r["query_type"] == "unknown"
    assert r["analysis_confidence"] == 0.1
    assert any("解析失败" in u for u in r["unknown_features"])


# ---------------------------------------------------------------------------
# 任务 2：时间特征
# ---------------------------------------------------------------------------

def test_no_time_filter():
    r = analyze("SELECT s_0 FROM root.test.d_0 WHERE s_0 > 100")
    t = r["query_features"]["time"]
    assert t["has_time_filter"] is False
    assert t["start_time"] is None and t["end_time"] is None
    assert t["time_span"] is None and t["range_level"] == "unknown"
    assert t["time_condition_form"] is None


def test_wide_range_level():
    r = analyze("SELECT s1 FROM root.sg1.d1 WHERE time >= 0 AND time <= 4102444800000")
    t = r["query_features"]["time"]
    assert t["range_level"] == "wide"


def test_time_in_list():
    r = analyze("SELECT s1 FROM root.sg1.d1 WHERE time IN (1640966400000, 1640966401000)")
    t = r["query_features"]["time"]
    assert t["start_time"] == 1640966400000 and t["end_time"] == 1640966401000
    assert t["time_condition_form"] == "in_list"


# ---------------------------------------------------------------------------
# 任务 3：设备维度
# ---------------------------------------------------------------------------

def test_multi_device():
    r = analyze("SELECT s_1666 FROM root.dbm.g_0.d_0, root.dbm.g_0.d_1 WHERE time >= 1 AND time <= 2")
    d = r["query_features"]["device"]
    assert d["has_device_filter"] is True
    assert d["device_count"] == 2
    assert d["multi_device"] is True
    assert d["device_paths"] == ["root.dbm.g_0.d_0", "root.dbm.g_0.d_1"]
    assert d["device_condition_count"] == 2


def test_single_device():
    r = analyze("SELECT s1 FROM root.sg1.d1 WHERE time >= 1 AND time <= 2")
    d = r["query_features"]["device"]
    assert d["has_device_filter"] is True
    assert d["device_count"] == 1
    assert d["multi_device"] is False


def test_wildcard_device_count_unknown():
    r = analyze("SELECT s_1 FROM root.test.* WHERE time >= 1 AND time <= 2")
    d = r["query_features"]["device"]
    assert d["has_device_filter"] is True
    assert d["device_count"] is None      # 不得猜测通配符匹配的设备数
    assert d["multi_device"] is None
    assert any("通配符" in u for u in r["unknown_features"])


def test_full_wildcard_no_device_filter():
    r = analyze("SELECT s1 FROM root.** WHERE time >= 1 AND time <= 2")
    d = r["query_features"]["device"]
    assert d["has_device_filter"] is False
    assert d["device_count"] is None


# ---------------------------------------------------------------------------
# 任务 4：过滤条件
# ---------------------------------------------------------------------------

def test_tag_or_attribute_ambiguity():
    r = analyze("SELECT s1 FROM root.sg1.d1 WHERE s1 > 10 AND t1 = 'v1'")
    f = r["query_features"]["filter"]
    assert f["filter_count"] == 2
    assert f["type_counts"]["value"] == 1
    assert f["type_counts"]["tag_or_attribute"] == 1   # t1 无法从 SQL 区分标签/属性
    assert any("tag_or_attribute" in u or "标签" in u for u in r["unknown_features"])


def test_selectivity_only_from_database_state():
    q = "SELECT s1 FROM root.sg1.d1 WHERE s1 > 10 AND time >= 1 AND time <= 2"
    r_no_db = analyze(q)
    assert r_no_db["query_features"]["filter"]["selectivity"] == {}
    assert any("选择率" in u for u in r_no_db["unknown_features"])

    db = {"statistics": {"selectivity": {"s1": 0.05, "time": 0.5}}}
    r_db = analyze(q, database_state=db)
    sel = r_db["query_features"]["filter"]["selectivity"]
    assert sel == {"s1": 0.05, "time": 0.5}   # 只拷贝，不计算
    # 非法选择率数据被忽略
    db_bad = {"statistics": {"selectivity": {"s1": 1.7}}}
    assert analyze(q, database_state=db_bad)["query_features"]["filter"]["selectivity"] == {}


# ---------------------------------------------------------------------------
# 任务 5：聚合特征
# ---------------------------------------------------------------------------

def test_aggregation_functions_iotdb_names():
    r = analyze("SELECT max_value(s_1668), min_value(s_1669) FROM root.db800.g_0.d_4")
    a = r["query_features"]["aggregation"]
    assert a["has_aggregation"] is True
    names = {f["function"] for f in a["aggregation_functions"]}
    assert names == {"max_value", "min_value"}


def test_group_by_level():
    r = analyze("SELECT COUNT(s1) FROM root.sg1.d1 GROUP BY LEVEL = 1")
    a = r["query_features"]["aggregation"]
    assert a["has_group_by"] is True
    assert a["group_by_details"]["type"] == "path_level"
    assert a["group_by_details"]["level"] == "1"


# ---------------------------------------------------------------------------
# 任务 6：扫描相关
# ---------------------------------------------------------------------------

def test_scan_estimate_only_from_database_state():
    q = "SELECT s1 FROM root.sg1.d1 WHERE time >= 1 AND time <= 2"
    r = analyze(q)
    s = r["query_features"]["scan"]
    assert s["estimated_scan_range"] is None
    assert s["scan_level"] == "unknown"

    db = {"scan_estimate": {"time_range": [0, 1_800_000], "estimated_points": 120}}
    r2 = analyze(q, database_state=db)
    s2 = r2["query_features"]["scan"]
    assert s2["estimated_scan_range"] == {"time_range": [0, 1_800_000], "estimated_points": 120}
    assert s2["scan_level"] == "narrow"     # 30min < 1h


def test_no_time_filter_implies_possible_large_scan():
    r = analyze("SELECT s1 FROM root.sg1.d1")
    s = r["query_features"]["scan"]
    assert s["possible_large_scan"] is True
    assert s["has_obvious_filter_conditions"] is False


def test_narrow_range_no_large_scan():
    r = analyze("SELECT s1 FROM root.sg1.d1 WHERE time >= 1640966400000 AND time <= 1640966450000")
    s = r["query_features"]["scan"]
    assert s["possible_large_scan"] is False
    assert s["has_obvious_filter_conditions"] is True


# ---------------------------------------------------------------------------
# 优化相关性：只述候选，不选策略
# ---------------------------------------------------------------------------

def test_relevance_statements():
    r = analyze(
        "SELECT s_1666 FROM root.db800.g_0.d_0, root.db800.g_0.d_1 "
        "WHERE time >= 1640966400000 AND time <= 1640966650000 "
        "AND root.db800.g_0.d_0.s_1666 > -5 AND root.db800.g_0.d_0.s_1766 > -5"
    )
    rel = r["optimization_relevant_features"]
    assert any("Time Pruning" in s and "候选" in s for s in rel["time_pruning"])
    assert any("Filter Order" in s and "候选" in s for s in rel["filter_order"])
    assert rel["aggregation_placement"] == []   # 无聚合 → 不产生该维度陈述


def test_relevance_no_strategy_claims():
    queries = [
        "SELECT s1 FROM root.sg1.d1 WHERE time >= 1 AND time <= 2",
        "SELECT AVG(s_0) FROM root.test.d_0 GROUP BY ([0, 1000000), 1h)",
        "SELECT s1 FROM root.sg1.d1 WHERE s1 > 10 AND t1 = 'v' AND s2 < 5",
        "SELECT count(s1) FROM root.sg1.d1 WHERE time >= 1 AND time <= 2",
    ]
    forbidden = ("应该使用", "必须使用", "建议采用", "最优", "执行计划", "重写为")
    for q in queries:
        r = analyze(q)
        for key, stmts in r["optimization_relevant_features"].items():
            for s in stmts:
                assert "候选" in s, f"{key} 陈述未以候选形式表述：{s}"
                for f in forbidden:
                    assert f not in s, f"越权表述出现在 {key}：{s}"


def test_relevance_silent_when_unknown():
    r = analyze("SELECT s1 FROM root.sg1.d1")   # 无时间过滤、无谓词、无聚合
    rel = r["optimization_relevant_features"]
    assert rel["time_pruning"] == []
    assert rel["filter_order"] == []
    assert rel["aggregation_placement"] == []


# ---------------------------------------------------------------------------
# 通用契约
# ---------------------------------------------------------------------------

def test_multi_statement_only_first():
    r = analyze("SELECT s1 FROM root.sg1.d1 WHERE time = 1; SELECT s2 FROM root.sg1.d2 WHERE time = 2")
    _check_schema(r)
    assert r["query_type"] == "point_query"
    assert any("仅分析第一条" in u for u in r["unknown_features"])


def test_align_by_device():
    r = analyze("SELECT * FROM root.sg1.d1 ALIGN BY DEVICE WHERE time >= 1 AND time <= 2")
    _check_schema(r)
    assert r["query_type"] == "complex_composite_query"


def test_determinism():
    q = "SELECT s1 FROM root.sg1.d1 WHERE time >= '2024-01-01 00:00:00' AND s1 > 10"
    assert analyze(q, query_id="x") == analyze(q, query_id="x")


def test_empty_input():
    r = analyze("")
    _check_schema(r)
    assert r["query_type"] == "unknown"
    assert r["analysis_confidence"] == 0.1


def test_confidence_high_for_fully_resolved_query():
    db = {
        "statistics": {"selectivity": {"s1": 0.1}},
        "scan_estimate": {"time_range": [0, 60_000], "estimated_points": 10},
    }
    r = analyze("SELECT s1 FROM root.sg1.d1 WHERE time >= 0 AND time <= 60000 AND s1 > 10",
                database_state=db)
    assert r["analysis_confidence"] == 1.0
