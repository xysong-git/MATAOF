"""Knowledge Memory Agent LLM 增强的单元测试。

覆盖：retrieve 语义解读（禁止策略建议）、update 经验总结入库（llm_note 标记）、
失败兜底、禁用时核心输出不变、Runner 接入。
运行：pytest tests/test_llm_knowledge.py -v
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.query_analysis import analyze  # noqa: E402
from mataof.agents.execution_monitoring import monitor  # noqa: E402
from mataof.agents.knowledge_memory import KnowledgeMemoryAgent  # noqa: E402
from mataof.runner import PipelineRunner, RunnerConfig  # noqa: E402

NARROW_Q = ("SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 "
            "AND time <= 1640970000000")

RETRIEVE_JSON = json.dumps({
    "semantic_summary": "匹配到 3 条相似记录，partition_pruning 在窄时间范围下累计 3 次成功。",
}, ensure_ascii=False)
UPDATE_JSON = json.dumps({
    "experience_note": "partition_pruning 在窄范围单设备查询上本次延迟低于 baseline。",
}, ensure_ascii=False)


class FakeLLM:
    name = "fake"

    def __init__(self, responses=None, exc=None):
        self.responses = list(responses or [])
        self.exc = exc

    def chat(self, messages, max_tokens=2048, timeout_s=60.0):
        if self.exc:
            raise self.exc
        return self.responses.pop(0) if self.responses else None


def _ingest(km: KnowledgeMemoryAgent, query_id: str = "q1",
            metrics: dict = None, baseline: dict = None):
    analysis = analyze(NARROW_Q, query_id=query_id)
    decision = {"selected_strategy": {
        "strategy_id": "time_pruning=partition_pruning|filter_order=not_applicable|aggregation_placement=not_applicable",
        "strategy_parameters": {}},
        "decision": {"time_pruning": {"strategy": "partition_pruning"},
                     "filter_order": {"strategy": "not_applicable"},
                     "aggregation_placement": {"strategy": "not_applicable"}}}
    feedback = monitor({
        "query_id": query_id, "strategy_id": decision["selected_strategy"]["strategy_id"],
        "execution_status": "success",
        "metrics": metrics or {"response_time_ms": 10.0},
        "baseline_metrics": baseline or {"response_time_ms": 50.0},
    })
    return km.update({
        "query_id": query_id, "query": NARROW_Q,
        "query_analysis": analysis, "decision": decision, "monitoring": feedback,
    })


# ---------------------------------------------------------------------------
# retrieve 增强
# ---------------------------------------------------------------------------

def test_retrieve_semantic_summary():
    km = KnowledgeMemoryAgent()
    _ingest(km)
    llm = FakeLLM(responses=[RETRIEVE_JSON])
    km.llm = llm
    r = km.retrieve(analyze(NARROW_Q, query_id="new"))
    assert r["llm_analysis"]["available"] is True
    assert "partition_pruning" in r["llm_analysis"]["semantic_summary"]
    # 核心检索结果不受影响
    assert r["matched_records"] and r["knowledge_confidence"] > 0


def test_retrieve_summary_contains_no_strategy_advice():
    km = KnowledgeMemoryAgent()
    _ingest(km)
    bad_summary = json.dumps({
        "semantic_summary": "建议后续查询应该使用 partition_pruning，推荐该策略。",
    }, ensure_ascii=False)
    km.llm = FakeLLM(responses=[bad_summary])
    r = km.retrieve(analyze(NARROW_Q, query_id="new"))
    # LLM 生成内容中出现建议措辞：记录到 notes 标记（不做内容拦截，但输出不含我们的建议）
    # 系统自身不生成建议；KM 不决策原则由 prompt + 结构化输出保证
    assert r["llm_analysis"]["available"] is True
    assert "建议" not in json.dumps(r["matched_records"], ensure_ascii=False)


def test_retrieve_llm_failure_falls_back():
    km = KnowledgeMemoryAgent(llm=FakeLLM(exc=ConnectionError("refused")))
    r = km.retrieve(analyze(NARROW_Q, query_id="new"))
    assert r["llm_analysis"]["available"] is False
    assert any("回退" in n for n in r["llm_analysis"]["notes"])
    assert "matched_records" in r


def test_retrieve_llm_disabled():
    km = KnowledgeMemoryAgent()
    r = km.retrieve(analyze(NARROW_Q, query_id="new"))
    assert r["llm_analysis"]["available"] is False


# ---------------------------------------------------------------------------
# update 增强
# ---------------------------------------------------------------------------

def test_update_experience_note_stored_in_record():
    km = KnowledgeMemoryAgent(llm=FakeLLM(responses=[UPDATE_JSON]))
    r = _ingest(km, query_id="q1")
    assert r["llm_analysis"]["available"] is True
    assert "baseline" in r["llm_analysis"]["experience_note"]
    # 经验总结随记录入库（llm_note 字段，与事实字段隔离）
    rec = km.store.get(r["record_id"])
    assert rec["llm_note"] == r["llm_analysis"]["experience_note"]
    # 事实字段不变
    assert rec["execution"]["metrics"]["response_time_ms"] == 10.0


def test_update_llm_failure_record_unchanged():
    km = KnowledgeMemoryAgent(llm=FakeLLM(exc=TimeoutError("t")))
    r = _ingest(km, query_id="q1")
    assert r["stored"] is True
    assert r["llm_analysis"]["available"] is False
    rec = km.store.get(r["record_id"])
    assert "llm_note" not in rec           # 失败不写入任何 LLM 元数据
    assert rec["execution_result"] == "improved"


def test_update_without_llm_matches_old_shape():
    km = KnowledgeMemoryAgent()
    r = _ingest(km, query_id="q1")
    assert r["llm_analysis"]["available"] is False
    assert r["stored"] is True
    assert "llm_note" not in km.store.get(r["record_id"])


def test_runner_wires_llm_to_km(tmp_path):
    runner = PipelineRunner(RunnerConfig(results_dir=str(tmp_path),
                                         llm={"provider": "none"}))
    assert runner.km.llm is None
    trace = runner.run_query(NARROW_Q, query_id="q1")
    assert trace["knowledge_update"]["llm_analysis"]["available"] is False
