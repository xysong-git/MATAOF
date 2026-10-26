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


def _decide(query, llm=None, llm_decide=False, **kwargs):
    analysis = analyze(query, query_id="q")
    return decide({"query_analysis": analysis, **kwargs}, llm=llm,
                  llm_decide=llm_decide)


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


# ---------------------------------------------------------------------------
# LLM 参与决策（decision_mode="llm_primary"）：LLM 选择 + 合法性校验，确定性兜底
# ---------------------------------------------------------------------------

def _decide_json(decisions: dict, reasons: dict = None) -> str:
    return json.dumps({"decisions": decisions,
                       "reasons": reasons or {}}, ensure_ascii=False)


def test_llm_decide_valid_selection_applied():
    # NARROW_Q 只适用 time_pruning；LLM 合法选择 full_scan → 采用并标注
    llm = FakeLLM(responses=[_decide_json(
        {"time_pruning": "full_scan", "filter_order": None,
         "aggregation_placement": None},
        {"time_pruning": "时间跨度窄但分区覆盖未知，保守选择全表扫描"})])
    r = _decide(NARROW_Q, llm=llm, llm_decide=True)
    assert r["decision"]["time_pruning"]["strategy"] == "full_scan"
    assert "llm_selected" in r["decision"]["time_pruning"]["reason"]
    assert r["selected_strategy"]["strategy_id"].startswith(
        "time_pruning=full_scan|")
    assert any("llm_selected" in n for n in r["notes"])
    assert llm.calls == 1                            # 决策模式恰好 1 次调用


def test_llm_decide_unknown_name_rejected():
    llm = FakeLLM(responses=[_decide_json(
        {"time_pruning": "magic_strategy", "filter_order": None,
         "aggregation_placement": None})])
    r = _decide(NARROW_Q, llm=llm, llm_decide=True)
    # 候选外策略名 → 该维度保持确定性决策（兜底）
    assert r["decision"]["time_pruning"]["strategy"] == "partition_pruning"
    assert any("候选外策略名" in n for n in r["notes"])


def test_llm_decide_unmet_requirements_rejected():
    # NARROW_Q 无非时间过滤 → filter_order 不适用；LLM 硬选 → 忽略该维度
    llm = FakeLLM(responses=[_decide_json(
        {"time_pruning": None, "filter_order": "time_first",
         "aggregation_placement": "final_level_aggregation"})])
    r = _decide(NARROW_Q, llm=llm, llm_decide=True)
    assert r["decision"]["filter_order"]["strategy"] == "not_applicable"
    assert r["decision"]["aggregation_placement"]["strategy"] == "not_applicable"
    assert any("维度不适用" in n for n in r["notes"])


def test_llm_decide_failure_fully_falls_back():
    r = _decide(NARROW_Q, llm=FakeLLM(exc=ConnectionError("refused")),
                llm_decide=True)
    assert r["decision"]["time_pruning"]["strategy"] == "partition_pruning"
    assert any("回退确定性决策" in n for n in r["notes"])


def test_llm_decide_all_nulls_keep_deterministic():
    r = _decide(NARROW_Q, llm=FakeLLM(responses=[_decide_json(
        {"time_pruning": None, "filter_order": None,
         "aggregation_placement": None})]), llm_decide=True)
    assert r["decision"]["time_pruning"]["strategy"] == "partition_pruning"


def test_llm_decide_equivalent_sql_from_candidate():
    # 候选提供方带 equivalent_sql（富条目契约：{"strategy": 名, ...}）：
    # LLM 只选名字，SQL 取自候选条目
    candidates = {
        "filter_order": [
            {"strategy": "time_first",
             "equivalent_sql": "SELECT s_1666 FROM root.db800.g_0.d_0 WHERE time >= 1640966400000 AND time <= 1640966650000 AND s_1666 > -5 AND s_1766 > -5",
             "parameters": {}},
        ],
        "time_pruning": [{"strategy": "full_scan", "equivalent_sql": None,
                          "parameters": {}}],
        "aggregation_placement": [{"strategy": "final_level_aggregation",
                                   "equivalent_sql": None, "parameters": {}}],
    }
    llm = FakeLLM(responses=[_decide_json(
        {"time_pruning": "full_scan", "filter_order": "time_first",
         "aggregation_placement": None},
        {"filter_order": "时间条件选择率更低，时间过滤先行"})])
    r = _decide(FILTER_Q, llm=llm, llm_decide=True, candidate_strategies=candidates)
    assert r["decision"]["filter_order"]["strategy"] == "time_first"
    # equivalent_sql 来自候选提供方，而非 LLM 生成
    assert r["decision"]["filter_order"]["equivalent_sql"] == \
        candidates["filter_order"][0]["equivalent_sql"]
    assert r["selected_strategy"]["equivalent_sql"] == \
        candidates["filter_order"][0]["equivalent_sql"]
    assert "time_pruning=full_scan" in r["selected_strategy"]["strategy_id"]
    assert "filter_order=time_first" in r["selected_strategy"]["strategy_id"]


def test_llm_decide_disabled_identical_to_deterministic():
    # llm_decide=False：即使 LLM 返回决策 JSON 也不采用（与不启用 LLM 决策一致；
    # LLM 仅做同步解读——1 次 llm_analysis 调用，决策字段逐字段不变）
    llm = FakeLLM(responses=[_decide_json(
        {"time_pruning": "full_scan", "filter_order": None,
         "aggregation_placement": None})])
    r0 = _decide(NARROW_Q)
    r1 = _decide(NARROW_Q, llm=llm, llm_decide=False)
    assert r1["selected_strategy"] == r0["selected_strategy"]
    assert r1["decision"] == r0["decision"]
    assert llm.calls == 1                            # 仅解读调用，未走决策请求路径
