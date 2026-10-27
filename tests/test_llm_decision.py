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
MULTI_Q = ("SELECT s_0 FROM root.test.d_0, root.test.d_1 WHERE time >= 1640966405000 "
           "AND time <= 1640970000000")


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
    "semantic_reason": "默认时间对齐在宽设备扫描下实测显著占优；当前信息条件下的选择。",
    "preferences": {"alignment": ["time_aligned"],
                    "fetch_size": ["fetch_large"]},
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
    assert "时间对齐" in la["semantic_reason"]
    assert la["preferences"]["alignment"][0] == "time_aligned"


def test_preferences_unknown_names_dropped():
    bad = json.dumps({
        "semantic_reason": "x",
        "preferences": {"alignment": ["magic_strategy", "align_by_device"],
                        "fetch_size": ["not_a_strategy"]},
    })
    r = _decide(NARROW_Q, llm=FakeLLM(responses=[bad]))
    prefs = r["llm_analysis"]["preferences"]
    assert prefs["alignment"] == ["align_by_device"]      # 未知名被丢弃
    assert "fetch_size" not in prefs
    assert any("非法策略名" in n for n in r["llm_analysis"]["notes"])


def test_llm_failure_falls_back():
    r = _decide(NARROW_Q, llm=FakeLLM(exc=ConnectionError("refused")))
    la = r["llm_analysis"]
    assert la["available"] is False
    assert any("回退确定性路径" in n for n in la["notes"])
    assert r["decision"]["fetch_size"]["strategy"] == "fetch_large"


def test_llm_invalid_json_falls_back():
    r = _decide(NARROW_Q, llm=FakeLLM(responses=["随便说点什么"]))
    assert r["llm_analysis"]["available"] is False
    assert r["decision_status"] == "success"           # 确定性决策不受影响


def test_llm_disabled_section_default():
    r = _decide(NARROW_Q)
    assert r["llm_analysis"]["available"] is False
    assert r["llm_analysis"]["semantic_reason"] is None


# ---------------------------------------------------------------------------
# 实验性偏好加分（llm_preference_bonus；当前目录全低风险、分差不可翻盘，
# 加分机制保留但无测试场景——失败路径仍验证"无副作用"）
# ---------------------------------------------------------------------------

def test_bonus_preference_request_failure_no_effect():
    r = _decide(NARROW_Q, llm=FakeLLM(exc=TimeoutError("t")), llm_preference_bonus=True)
    assert r["decision"]["fetch_size"]["strategy"] == "fetch_large"
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
    # NARROW_Q：alignment 不适用；LLM 合法选择 fetch_default → 采用并标注
    llm = FakeLLM(responses=[_decide_json(
        {"alignment": None, "fetch_size": "fetch_default"},
        {"fetch_size": "保守选择默认批次"})])
    r = _decide(NARROW_Q, llm=llm, llm_decide=True)
    assert r["decision"]["fetch_size"]["strategy"] == "fetch_default"
    assert "llm_selected" in r["decision"]["fetch_size"]["reason"]
    assert "fetch_size=fetch_default" in r["selected_strategy"]["strategy_id"]
    assert any("llm_selected" in n for n in r["notes"])
    assert llm.calls == 1                            # 决策模式恰好 1 次调用


def test_llm_decide_unknown_name_rejected():
    llm = FakeLLM(responses=[_decide_json(
        {"alignment": None, "fetch_size": "magic_strategy"})])
    r = _decide(NARROW_Q, llm=llm, llm_decide=True)
    # 候选外策略名 → 该维度保持确定性决策（兜底）
    assert r["decision"]["fetch_size"]["strategy"] == "fetch_large"
    assert any("候选外策略名" in n for n in r["notes"])


def test_llm_decide_unmet_requirements_rejected():
    # NARROW_Q 单设备 → alignment 不适用；LLM 硬选 → 忽略该维度
    llm = FakeLLM(responses=[_decide_json(
        {"alignment": "align_by_device", "fetch_size": None})])
    r = _decide(NARROW_Q, llm=llm, llm_decide=True)
    assert r["decision"]["alignment"]["strategy"] == "not_applicable"
    assert any("维度不适用" in n for n in r["notes"])


def test_llm_decide_failure_fully_falls_back():
    r = _decide(NARROW_Q, llm=FakeLLM(exc=ConnectionError("refused")),
                llm_decide=True)
    assert r["decision"]["fetch_size"]["strategy"] == "fetch_large"
    assert any("回退确定性决策" in n for n in r["notes"])


def test_llm_decide_all_nulls_keep_deterministic():
    r = _decide(NARROW_Q, llm=FakeLLM(responses=[_decide_json(
        {"alignment": None, "fetch_size": None})]), llm_decide=True)
    assert r["decision"]["fetch_size"]["strategy"] == "fetch_large"


def test_llm_decide_equivalent_sql_from_candidate():
    # 候选提供方带 equivalent_sql（富条目契约：{"strategy": 名, ...}）：
    # 决策只选名字，SQL 取自候选条目（LLM 全 null → 确定性选择）
    aligned = MULTI_Q + " ALIGN BY DEVICE"
    candidates = {
        "alignment": [
            {"strategy": "align_by_device", "equivalent_sql": aligned,
             "parameters": {}},
        ],
        "fetch_size": [{"strategy": "fetch_default"}],
    }
    llm = FakeLLM(responses=[_decide_json(
        {"alignment": None, "fetch_size": None})])
    r = _decide(MULTI_Q, llm=llm, llm_decide=True, candidate_strategies=candidates)
    assert r["decision"]["alignment"]["strategy"] == "align_by_device"
    # equivalent_sql 来自候选提供方，而非 LLM 生成
    assert r["decision"]["alignment"]["equivalent_sql"] == aligned
    assert r["selected_strategy"]["equivalent_sql"] == aligned


def test_llm_decide_disabled_identical_to_deterministic():
    # llm_decide=False：即使 LLM 返回决策 JSON 也不采用（与不启用 LLM 决策一致；
    # LLM 仅做同步解读——1 次 llm_analysis 调用，决策字段逐字段不变）
    llm = FakeLLM(responses=[_decide_json(
        {"alignment": None, "fetch_size": "fetch_default"})])
    r0 = _decide(NARROW_Q)
    r1 = _decide(NARROW_Q, llm=llm, llm_decide=False)
    assert r1["selected_strategy"] == r0["selected_strategy"]
    assert r1["decision"] == r0["decision"]
    assert llm.calls == 1                            # 仅解读调用，未走决策请求路径
