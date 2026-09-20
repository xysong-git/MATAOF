"""Optimization Decision Agent LLM 增强的单元测试。

覆盖：决策解读/偏好填充、偏好严格校验（未知名丢弃）、门控不受 LLM 解锁、
bonus 有界与同分翻转、失败兜底、禁用时输出不变（核心字段）。
运行：pytest tests/test_llm_decision.py -v
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.query_analysis import analyze  # noqa: E402
from mataof.agents.optimization_decision import decide  # noqa: E402

NARROW_Q = ("SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 "
            "AND time <= 1640970000000")
FILTER_Q = ("SELECT s_1666 FROM root.db800.g_0.d_0 WHERE time >= 1640966400000 "
            "AND time <= 1640966650000 AND root.db800.g_0.d_0.s_1666 > -5 "
            "AND root.db800.g_0.d_0.s_1766 > -5")


class FakeLLM:
    name = "fake"

    def __init__(self, responses=None, exc=None):
        self.responses = list(responses or [])
        self.exc = exc
        self.calls = 0

    def chat(self, messages, max_tokens=2048, timeout_s=60.0):
        self.calls += 1
        if self.exc:
            raise self.exc
        if self.responses:
            return self.responses.pop(0)
        return None


def _decide(query, llm=None, **kwargs):
    analysis = analyze(query, query_id="q")
    return decide({"query_analysis": analysis, **kwargs}, llm=llm)


REASON_JSON = json.dumps({
    "semantic_reason": "窄时间范围且分区覆盖低，partition_pruning 有明确依据；当前信息条件下的选择。",
    "preferences": {"time_pruning": ["partition_pruning", "full_scan"],
                    "filter_order": ["time_first"],
                    "aggregation_placement": []},
}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 语义解读与偏好展示（默认路径：LLM 不参与评分）
# ---------------------------------------------------------------------------

def test_llm_analysis_filled_without_affecting_decision():
    llm = FakeLLM(responses=[REASON_JSON])
    r1 = _decide(NARROW_Q)
    r2 = _decide(NARROW_Q, llm=llm)
    # 核心决策完全一致（LLM 只进 llm_analysis）
    assert r1["selected_strategy"] == r2["selected_strategy"]
    assert r1["overall_confidence"] == r2["overall_confidence"]
    la = r2["llm_analysis"]
    assert la["available"] is True
    assert "partition_pruning" in la["semantic_reason"]
    assert la["preferences"]["time_pruning"][0] == "partition_pruning"


def test_preferences_unknown_names_dropped():
    bad = json.dumps({
        "semantic_reason": "x",
        "preferences": {"time_pruning": ["magic_strategy", "full_scan"],
                        "filter_order": ["not_a_strategy"],
                        "aggregation_placement": []},
    })
    r = _decide(NARROW_Q, llm=FakeLLM(responses=[bad]))
    prefs = r["llm_analysis"]["preferences"]
    assert prefs["time_pruning"] == ["full_scan"]      # 未知名被丢弃
    assert "filter_order" not in prefs
    assert any("非法策略名" in n for n in r["llm_analysis"]["notes"])


def test_llm_failure_falls_back():
    r = _decide(NARROW_Q, llm=FakeLLM(exc=ConnectionError("refused")))
    la = r["llm_analysis"]
    assert la["available"] is False
    assert any("回退确定性路径" in n for n in la["notes"])
    assert r["decision"]["time_pruning"]["strategy"] == "partition_pruning"


def test_llm_invalid_json_falls_back():
    r = _decide(NARROW_Q, llm=FakeLLM(responses=["随便说点什么"]))
    assert r["llm_analysis"]["available"] is False
    assert r["decision_status"] == "success"           # 确定性决策不受影响


def test_llm_disabled_section_default():
    r = _decide(NARROW_Q)
    assert r["llm_analysis"]["available"] is False
    assert r["llm_analysis"]["semantic_reason"] is None


# ---------------------------------------------------------------------------
# 实验性偏好加分（llm_preference_bonus）
# ---------------------------------------------------------------------------

def test_bonus_cannot_unlock_high_risk_gate():
    # chunk_level_filtering 高风险：无历史证据 → 门控排除；LLM 偏好不能解锁
    db = {"physical_organization": {"chunk_level": {"total_chunks": 1000, "covered_chunks": 5}}}
    prefs_json = json.dumps({
        "semantic_reason": "x",
        "preferences": {"time_pruning": ["chunk_level_filtering"],
                        "filter_order": [], "aggregation_placement": []},
    })
    r = _decide(NARROW_Q, llm=FakeLLM(responses=[prefs_json]),
                database_state=db, llm_preference_bonus=True)
    assert r["decision"]["time_pruning"]["strategy"] != "chunk_level_filtering"
    assert any("chunk_level_filtering" in n for n in r["notes"])


def test_bonus_flips_close_call_within_bounds():
    # 构造 0.05 内的胶着：narrow + 非时间条件选择率极优 → device_tag_first 0.75
    # 对 time_first 0.70；LLM 偏好 time_first +0.05 → 平分 → 低风险 time_first 胜出
    db = {"statistics": {"selectivity": {
        "time": 0.4, "root.db800.g_0.d_0.s_1666": 0.02, "root.db800.g_0.d_0.s_1766": 0.05,
    }}}
    r_no = _decide(FILTER_Q, database_state=db)
    assert r_no["decision"]["filter_order"]["strategy"] == "device_tag_first"

    prefs_json = json.dumps({
        "semantic_reason": "x",
        "preferences": {"time_pruning": [], "filter_order": ["time_first"],
                        "aggregation_placement": []},
    })
    r_llm = _decide(FILTER_Q, llm=FakeLLM(responses=[prefs_json]),
                    database_state=db, llm_preference_bonus=True)
    assert r_llm["decision"]["filter_order"]["strategy"] == "time_first"
    assert "LLM 偏好加分" in r_llm["decision"]["filter_order"]["reason"]


def test_bonus_bounded_does_not_override_strong_evidence():
    # 强依据（分区覆盖）下 LLM 偏好 full_scan 只加 0.05，不足以翻盘
    db = {"partition_info": {"total_partitions": 128, "covered_partitions": 2}}
    prefs_json = json.dumps({
        "semantic_reason": "x",
        "preferences": {"time_pruning": ["full_scan"],
                        "filter_order": [], "aggregation_placement": []},
    })
    r = _decide(NARROW_Q, llm=FakeLLM(responses=[prefs_json]),
                database_state=db, llm_preference_bonus=True)
    assert r["decision"]["time_pruning"]["strategy"] == "partition_pruning"


def test_bonus_preference_request_failure_no_effect():
    r = _decide(NARROW_Q, llm=FakeLLM(exc=TimeoutError("t")), llm_preference_bonus=True)
    assert r["decision"]["time_pruning"]["strategy"] == "partition_pruning"
    assert any("不使用偏好加分" in n for n in r["notes"])


# ---------------------------------------------------------------------------
# 确定性契约
# ---------------------------------------------------------------------------

def test_core_fields_deterministic_without_llm():
    r1 = _decide(NARROW_Q)
    r2 = _decide(NARROW_Q)
    core1 = {k: v for k, v in r1.items() if k != "llm_analysis"}
    core2 = {k: v for k, v in r2.items() if k != "llm_analysis"}
    assert core1 == core2
