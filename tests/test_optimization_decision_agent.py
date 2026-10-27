"""Optimization Decision Agent 单元测试（策略空间重设计后的目录）。

覆盖：输入契约、有界策略空间、维度适用性（对齐方式/结果批次）、
数据驱动决策（设备数实测先验）、历史证据相似度与加权、
置信度不虚高、确定性、schema 契约、不越权声明、
混合模型（候选携带等价 SQL / 执行参数）。
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
REQUIRED_DIMS = ("alignment", "fetch_size")

MULTI_Q = ("SELECT s_0 FROM root.test.d_0, root.test.d_1 WHERE time >= 1640966405000 "
           "AND time <= 1640970000000")
SINGLE_Q = ("SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 "
            "AND time <= 1640970000000")
WIDE_Q = ("SELECT s_0 FROM root.test.d_0, root.test.d_1, root.test.d_2, "
          "root.test.d_3, root.test.d_4, root.test.d_5, root.test.d_6, "
          "root.test.d_7, root.test.d_8, root.test.d_9 WHERE time >= 1640966405000 "
          "AND time <= 1640970000000")


def _check_schema(r: dict) -> None:
    assert set(REQUIRED_TOP_KEYS) <= set(r.keys())
    for d in REQUIRED_DIMS:
        assert set(r["decision"][d].keys()) == {"strategy", "reason", "confidence",
                                                "equivalent_sql"}
    assert "equivalent_sql" in r["selected_strategy"]
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


def test_dimension_not_applicable_single_device():
    # 单设备查询：对齐方式无差异 → alignment 不适用；fetch_size 恒适用
    r = _decide(SINGLE_Q)
    _check_schema(r)
    assert r["decision"]["alignment"]["strategy"] == "not_applicable"
    assert r["decision"]["fetch_size"]["strategy"] == "fetch_large"


# ---------------------------------------------------------------------------
# Alignment 决策（实测先验：设备数驱动的方向）
# ---------------------------------------------------------------------------

def test_wide_device_scan_prefers_time_aligned():
    # 设备数 ≥10：实测默认对齐占优 15~25× → time_aligned
    r = _decide(WIDE_Q)
    assert r["decision"]["alignment"]["strategy"] == "time_aligned"
    assert "设备数" in r["decision"]["alignment"]["reason"]
    assert "alignment=time_aligned" in r["selected_strategy"]["strategy_id"]


def test_align_by_device_selected_with_history_evidence():
    # 少量设备：结构先验仍偏 time_aligned；历史证据显示按设备对齐更快 → 翻盘
    history = [
        _record("h1", "range_query", 25000, "narrow", device_count=2,
                strategy={"alignment": "align_by_device"}, latency=3.0),
        _record("h2", "range_query", 30000, "narrow", device_count=2,
                strategy={"alignment": "align_by_device"}, latency=4.0),
        _record("h3", "range_query", 25000, "narrow", device_count=2,
                strategy={"alignment": "time_aligned"}, latency=20.0),
    ]
    r = _decide(MULTI_Q, historical_records=history)
    assert r["decision"]["alignment"]["strategy"] == "align_by_device"
    assert "历史证据" in r["decision"]["alignment"]["reason"]


def test_unknown_candidate_names_rejected():
    r = _decide(
        MULTI_Q,
        candidate_strategies={"alignment": ["magic_strategy", "time_aligned"],
                              "fetch_size": ["fetch_large"]},
    )
    assert "magic_strategy" not in r["selected_strategy"]["strategy_id"]
    assert any("magic_strategy" in n for n in r["notes"])


# ---------------------------------------------------------------------------
# Fetch Size 决策（实测 ~15-20% 收益 → fetch_large 结构性占优）
# ---------------------------------------------------------------------------

def test_fetch_large_chosen_by_default():
    r = _decide(SINGLE_Q)
    assert r["decision"]["fetch_size"]["strategy"] == "fetch_large"
    assert "fetch_size=fetch_large" in r["selected_strategy"]["strategy_id"]


def test_fetch_candidate_restriction_respected():
    # 候选只有 fetch_default → 只选它（有界策略空间）
    r = _decide(SINGLE_Q,
                candidate_strategies={"alignment": [],
                                      "fetch_size": ["fetch_default"]})
    assert r["decision"]["fetch_size"]["strategy"] == "fetch_default"


# ---------------------------------------------------------------------------
# 历史证据
# ---------------------------------------------------------------------------

def test_history_similarity_filters_dissimilar_records():
    history = [
        _record("dissimilar", "window_aggregation_query", 3_600_000_000, "wide",
                has_agg=True, has_group_by=True, has_window=True,
                strategy={"alignment": "time_aligned"}, latency=1.0),
    ]
    r = _decide(WIDE_Q, historical_records=history)
    used = [h for h in r["evidence"]["historical_records"] if h.get("record_id")]
    assert used == []  # 相似度低于阈值 → 不参与
    summary = next(h["summary"] for h in r["evidence"]["historical_records"]
                   if "summary" in h)
    assert summary["below_similarity_threshold"] == 1


def test_history_evidence_boosts_matching_strategy():
    history = [
        _record("h1", "range_query", 25000, "narrow", device_count=10,
                strategy={"alignment": "time_aligned"}, latency=3.0),
        _record("h2", "range_query", 30000, "narrow", device_count=10,
                strategy={"alignment": "align_by_device"}, latency=80.0),
    ]
    r = _decide(WIDE_Q, historical_records=history)
    assert r["decision"]["alignment"]["strategy"] == "time_aligned"
    assert any(h["record_id"] == "h1" for h in r["evidence"]["historical_records"]
               if h.get("record_id"))


def test_invalid_history_records_ignored():
    history = [
        {"record_id": "bad1", "strategy": {}, "execution_feedback": {"latency_ms": 1}},
        {"record_id": "bad2", "query_features": {}, "strategy": {},
         "execution_feedback": {"latency_ms": "fast"}},  # 非数值延迟
    ]
    r = _decide(WIDE_Q, historical_records=history)
    summary = next(h["summary"] for h in r["evidence"]["historical_records"]
                   if "summary" in h)
    assert summary["invalid"] == 2


# ---------------------------------------------------------------------------
# 置信度与安全
# ---------------------------------------------------------------------------

def test_confidence_not_inflated_without_evidence():
    r = _decide(WIDE_Q)
    assert r["overall_confidence"] <= 0.6  # 无数据库状态、无历史 → 上限 0.6


def test_confidence_higher_with_evidence():
    r_none = _decide(WIDE_Q)
    r_rich = _decide(WIDE_Q, historical_records=[
        _record("h1", "range_query", 25000, "narrow", device_count=10,
                strategy={"alignment": "time_aligned"}, latency=8.0),
    ])
    assert r_rich["decision"]["alignment"]["confidence"] >= \
        r_none["decision"]["alignment"]["confidence"]


def test_system_state_recorded_in_evidence():
    # 负载状态进入证据（新目录暂无负载交互加分——诚实：不编造调整依据）
    r = _decide(WIDE_Q, system_state={"cpu_utilization": 0.9,
                                      "memory_utilization": 0.5,
                                      "io_utilization": 0.85})
    assert any("load_level=high" in s for s in r["evidence"]["system_state"])


def test_no_optimality_claims():
    queries = [SINGLE_Q, MULTI_Q, WIDE_Q]
    for q in queries:
        r = _decide(q)
        for d in REQUIRED_DIMS:
            reason = r["decision"][d]["reason"]
            for bad in ("全局最优", "绝对最优", "最优策略"):
                assert bad not in reason, f"{d} 出现越权声明：{reason}"


# ---------------------------------------------------------------------------
# 混合模型：候选携带等价 SQL / 执行参数，决策输出可直接执行
# ---------------------------------------------------------------------------

def test_align_candidate_carries_equivalent_sql():
    # 候选提供方给出 ALIGN BY DEVICE 等价 SQL；历史证据使 align_by_device 胜出
    aligned_sql = MULTI_Q + " ALIGN BY DEVICE"
    history = [
        _record("h1", "range_query", 25000, "narrow", device_count=2,
                strategy={"alignment": "align_by_device"}, latency=2.0),
        _record("h2", "range_query", 30000, "narrow", device_count=2,
                strategy={"alignment": "align_by_device"}, latency=3.0),
    ]
    r = _decide(
        MULTI_Q,
        historical_records=history,
        candidate_strategies={
            "alignment": [
                {"strategy": "time_aligned"},
                {"strategy": "align_by_device", "equivalent_sql": aligned_sql},
            ],
            "fetch_size": ["fetch_large"],
        },
    )
    assert r["decision"]["alignment"]["strategy"] == "align_by_device"
    assert r["decision"]["alignment"]["equivalent_sql"] == aligned_sql
    # 唯一 SQL 维度 → 组合结果即该 SQL，可直接交给执行层
    assert r["selected_strategy"]["equivalent_sql"] == aligned_sql


def test_no_sql_candidate_returns_original_query():
    # 全部无 SQL（默认候选）→ 组合结果为原查询文本
    r = _decide(SINGLE_Q)
    assert r["selected_strategy"]["equivalent_sql"] == SINGLE_Q


def test_multiple_sql_dimensions_not_composed():
    # 两个维度都提供 SQL（人为构造）→ 不冒险组合，置 null；维度级仍各自给出
    r = _decide(
        MULTI_Q,
        candidate_strategies={
            "alignment": [
                {"strategy": "time_aligned",
                 "equivalent_sql": MULTI_Q + " ALIGN BY TIME"},
            ],
            "fetch_size": [
                {"strategy": "fetch_large",
                 "equivalent_sql": MULTI_Q + " LIMIT 100"},
            ],
        },
    )
    assert r["selected_strategy"]["equivalent_sql"] is None


def test_candidate_parameters_passed_through():
    # fetch_large 的执行级参数（候选提供方生成）原样透传
    r = _decide(
        SINGLE_Q,
        candidate_strategies={
            "fetch_size": [
                {"strategy": "fetch_large", "parameters": {"fetch_size": 10000}},
            ],
            "alignment": [],
        },
    )
    params = r["selected_strategy"]["strategy_parameters"]
    assert params["fetch_size"]["fetch_size"] == 10000


def test_plain_name_candidates_backward_compatible():
    # 纯名称候选（旧格式）仍可用，equivalent_sql 为 null
    r = _decide(MULTI_Q,
                candidate_strategies={"alignment": ["time_aligned", "align_by_device"],
                                      "fetch_size": ["fetch_default"]})
    assert r["decision"]["alignment"]["strategy"] == "time_aligned"
    assert r["decision"]["alignment"]["equivalent_sql"] is None
    assert r["selected_strategy"]["equivalent_sql"] == MULTI_Q


def test_invalid_equivalent_sql_ignored():
    r = _decide(
        SINGLE_Q,
        candidate_strategies={
            "fetch_size": [
                {"strategy": "fetch_large", "equivalent_sql": ""},   # 空串无效
            ],
            "alignment": [],
        },
    )
    assert r["decision"]["fetch_size"]["equivalent_sql"] is None
    assert any("equivalent_sql" in n for n in r["notes"])


# ---------------------------------------------------------------------------
# 通用契约
# ---------------------------------------------------------------------------

def test_determinism():
    inp = {
        "query_analysis": analyze(WIDE_Q, query_id="x"),
        "historical_records": [
            _record("h1", "range_query", 25000, "narrow", device_count=10,
                    strategy={"alignment": "time_aligned"}, latency=8.0),
        ],
    }
    assert decide(inp) == decide(inp)


def test_flat_candidate_list():
    r = _decide(WIDE_Q, candidate_strategies=["time_aligned", "align_by_device"])
    assert r["decision"]["alignment"]["strategy"] in ("time_aligned", "align_by_device")


def test_invalid_baseline_falls_back_to_default():
    r = _decide(
        WIDE_Q,
        candidate_strategies={"alignment": ["align_by_device"],
                              "fetch_size": ["fetch_default"]},
        baseline_strategy={"alignment": "not_a_strategy",
                           "fetch_size": "not_a_strategy"},
    )
    assert any("baseline" in n for n in r["notes"])


def test_decision_status_success():
    r = _decide(WIDE_Q)
    assert r["decision_status"] == "success"
