"""Knowledge Memory Agent 单元测试。

覆盖：append-only 存储、无执行反馈不存储、时间戳不编造、三层匹配检索、
成功/失败经验模式、单次异常不否定历史、策略优先级变化、持久化、
决策 Agent 端到端消费 KM 输出、确定性、严格限制。
运行：pytest tests/test_knowledge_memory_agent.py -v
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.query_analysis import analyze  # noqa: E402
from mataof.agents.execution_monitoring import monitor  # noqa: E402
from mataof.agents.optimization_decision import decide  # noqa: E402
from mataof.agents.knowledge_memory import KnowledgeMemoryAgent  # noqa: E402

NARROW_Q = "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000"
MEDIUM_Q = "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1644566405000"
AGG_Q = "SELECT AVG(s_0) FROM root.test.d_0 GROUP BY ([1640966405000, 1640976405000), 1h)"


def _decision_for(query_id: str, strategy_id: str, by_dimension: dict) -> dict:
    return {
        "query_id": query_id,
        "selected_strategy": {"strategy_id": strategy_id, "strategy_parameters": {}},
        "decision": {d: {"strategy": s, "reason": "", "confidence": 0.5}
                     for d, s in by_dimension.items()},
    }


def _monitoring_for(query_id: str, strategy_id: str, metrics: dict,
                    baseline: dict = None, status: str = "success") -> dict:
    return monitor({
        "query_id": query_id,
        "strategy_id": strategy_id,
        "execution_status": status,
        "metrics": metrics,
        "baseline_metrics": baseline,
    })


def _ingest(km: KnowledgeMemoryAgent, query_id: str, query: str, strategy_id: str,
            by_dimension: dict, metrics: dict, baseline: dict = None,
            status: str = "success", timestamp=None, db_state: dict = None) -> dict:
    """完整装配一条记录：分析 → 决策(手工指定策略) → 监控 → 知识入库。"""
    analysis = analyze(query, query_id=query_id)
    decision = _decision_for(query_id, strategy_id, by_dimension)
    monitoring = _monitoring_for(query_id, strategy_id, metrics, baseline, status)
    return km.update({
        "query_id": query_id,
        "query": query,
        "query_analysis": analysis,
        "decision": decision,
        "monitoring": monitoring,
        "database_state": db_state or {},
        "timestamp": timestamp,
    })


PRUNE = {"time_pruning": "partition_pruning", "filter_order": "not_applicable",
         "aggregation_placement": "not_applicable"}
SCAN = {"time_pruning": "full_scan", "filter_order": "not_applicable",
        "aggregation_placement": "not_applicable"}
SID_PRUNE = "time_pruning=partition_pruning|filter_order=not_applicable|aggregation_placement=not_applicable"
SID_SCAN = "time_pruning=full_scan|filter_order=not_applicable|aggregation_placement=not_applicable"


# ---------------------------------------------------------------------------
# 更新（反馈入库）
# ---------------------------------------------------------------------------

def test_update_stores_record():
    km = KnowledgeMemoryAgent()
    r = _ingest(km, "q1", NARROW_Q, SID_PRUNE, PRUNE,
                {"response_time_ms": 10.0}, {"response_time_ms": 50.0}, timestamp=1700000000000)
    assert r["stored"] is True
    assert r["update_type"] == "new_record"
    assert r["record_id"].startswith("rec")
    assert r["strategy_effect"] == "improved"
    assert r["knowledge_update"]["success_record"] is True
    rec = km.store.get(r["record_id"])
    assert rec["query_record"]["query_id"] == "q1"
    assert rec["strategy"]["strategy_id"] == SID_PRUNE
    assert rec["execution"]["metrics"]["response_time_ms"] == 10.0
    assert rec["timestamp"] == 1700000000000


def test_update_rejects_without_monitoring():
    km = KnowledgeMemoryAgent()
    r = km.update({"query_id": "q1", "query_analysis": analyze(NARROW_Q, query_id="q1")})
    assert r["stored"] is False
    assert "missing_execution" in r["update_type"]
    assert km.store.count() == 0     # 不存储任何内容，不编造执行事实


def test_update_rejects_without_query_id():
    km = KnowledgeMemoryAgent()
    r = km.update({"monitoring": _monitoring_for("", "s", {"response_time_ms": 1.0})})
    assert r["stored"] is False
    assert "rejected_invalid" in r["update_type"]


def test_timestamp_not_fabricated():
    km = KnowledgeMemoryAgent()
    r = _ingest(km, "q1", NARROW_Q, SID_PRUNE, PRUNE, {"response_time_ms": 10.0})
    rec = km.store.get(r["record_id"])
    assert rec["timestamp"] is None
    assert any("时间戳" in n for n in r["notes"])


def test_append_only_no_modification():
    km = KnowledgeMemoryAgent()
    r1 = _ingest(km, "q1", NARROW_Q, SID_PRUNE, PRUNE, {"response_time_ms": 10.0})
    snapshot = json.dumps(km.store.get(r1["record_id"]), sort_keys=True)
    r2 = _ingest(km, "q1", NARROW_Q, SID_PRUNE, PRUNE, {"response_time_ms": 25.0})
    assert r2["record_id"] != r1["record_id"]
    assert km.store.count() == 2
    # 既有记录未被修改
    assert json.dumps(km.store.get(r1["record_id"]), sort_keys=True) == snapshot
    # 无修改/删除接口
    assert not hasattr(km.store, "delete")
    assert not hasattr(km.store, "update")


def test_strategy_priority_change():
    km = KnowledgeMemoryAgent()
    # 两次失败 → 一次成功：累计成功率 0 → 1/3，跨越阈值 → increased
    for i, (lat, base) in enumerate([(60.0, 20.0), (55.0, 20.0), (10.0, 20.0)]):
        r = _ingest(km, f"q{i}", NARROW_Q, SID_PRUNE, PRUNE,
                    {"response_time_ms": lat}, {"response_time_ms": base})
        if i == 2:
            assert r["knowledge_update"]["strategy_priority_change"] == "increased"
        else:
            assert r["knowledge_update"]["strategy_priority_change"] is None


def test_failure_record_marked():
    km = KnowledgeMemoryAgent()
    r = _ingest(km, "q1", NARROW_Q, SID_PRUNE, PRUNE, {}, status="failed")
    assert r["stored"] is True
    assert r["strategy_effect"] == "failed"
    assert r["knowledge_update"]["failure_record"] is True
    assert r["knowledge_update"]["success_record"] is False


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------

def _seed(km: KnowledgeMemoryAgent) -> None:
    """3 次成功（窄范围 partition_pruning）+ 1 次失败（full_scan）+ 1 条不相干记录。"""
    for i in range(3):
        _ingest(km, f"p{i}", NARROW_Q, SID_PRUNE, PRUNE,
                {"response_time_ms": 10.0 + i}, {"response_time_ms": 50.0},
                timestamp=1700000000000 + i * 1000)
    _ingest(km, "f0", NARROW_Q, SID_SCAN, SCAN,
            {"response_time_ms": 90.0}, {"response_time_ms": 50.0},
            timestamp=1700000003000)
    _ingest(km, "u0", AGG_Q, SID_SCAN, SCAN,
            {"response_time_ms": 5.0}, {"response_time_ms": 6.0},
            timestamp=1700000004000)


def test_retrieve_empty_store():
    km = KnowledgeMemoryAgent()
    r = km.retrieve(analyze(NARROW_Q, query_id="new"))
    assert r["matched_records"] == []
    assert r["historical_summary"]["successful_strategies"] == []
    assert r["knowledge_confidence"] == 0.0


def test_retrieve_tiers_and_summary():
    km = KnowledgeMemoryAgent()
    _seed(km)
    r = km.retrieve(analyze(NARROW_Q, query_id="new"))
    records = r["matched_records"]
    assert records, "应有匹配记录"
    # 相似度降序
    sims = [m["similarity"] for m in records]
    assert sims == sorted(sims, reverse=True)
    # 窄范围查询与自身历史 → strong；聚合查询 → 弱或不匹配
    levels = {m["match_level"] for m in records}
    assert "strong_match" in levels
    # 成功模式：partition_pruning 3 次成功
    sids = [s["strategy_id"] for s in r["historical_summary"]["successful_strategies"]]
    assert SID_PRUNE in sids
    pattern = next(s for s in r["historical_summary"]["successful_strategies"]
                   if s["strategy_id"] == SID_PRUNE)
    assert pattern["count"] == 3 and pattern["success_ratio"] == 1.0
    # 失败经验保留：full_scan 1 次失败
    fsids = [s["strategy_id"] for s in r["historical_summary"]["failed_strategies"]]
    assert SID_SCAN in fsids
    assert r["knowledge_confidence"] > 0.0


def test_single_failure_does_not_negate_history():
    km = KnowledgeMemoryAgent()
    for i in range(3):
        _ingest(km, f"p{i}", NARROW_Q, SID_PRUNE, PRUNE,
                {"response_time_ms": 10.0 + i}, {"response_time_ms": 50.0})
    _ingest(km, "p3", NARROW_Q, SID_PRUNE, PRUNE,
            {"response_time_ms": 200.0}, {"response_time_ms": 50.0})
    assert km.store.count() == 4                       # 历史样本全部保留
    r = km.retrieve(analyze(NARROW_Q, query_id="new"))
    # 成功率 3/4 = 0.75 ≥ 0.6 → 成功模式仍成立（不被单次异常否定）
    pattern = next(s for s in r["historical_summary"]["successful_strategies"]
                   if s["strategy_id"] == SID_PRUNE)
    assert pattern["success_ratio"] == 0.75
    assert SID_PRUNE in [s["strategy_id"] for s in r["historical_summary"]["failed_strategies"]]


def test_retrieval_is_feature_based_not_string_based():
    km = KnowledgeMemoryAgent()
    _ingest(km, "q1", NARROW_Q, SID_PRUNE, PRUNE, {"response_time_ms": 10.0},
            {"response_time_ms": 50.0})
    # 不同 SQL 文本（不同测点/设备路径），但特征一致 → 高度相似（非 SQL 字符串匹配）
    other_q = "SELECT s_1 FROM root.test.d_1 WHERE time >= 1640966405000 AND time <= 1640970000000"
    r = km.retrieve(analyze(other_q, query_id="new"))
    assert r["matched_records"] and r["matched_records"][0]["similarity"] >= 0.8
    # 特征差异大的查询（窗口聚合、宽范围）→ 相似度显著下降
    r2 = km.retrieve(analyze(AGG_Q, query_id="new"))
    if r2["matched_records"]:
        assert r2["matched_records"][0]["similarity"] < 0.6


def test_knowledge_confidence_scales_with_matches():
    km = KnowledgeMemoryAgent()
    _seed(km)
    r1 = km.retrieve(analyze(NARROW_Q, query_id="a"))
    r2 = km.retrieve(analyze(AGG_Q, query_id="b"))
    assert r1["knowledge_confidence"] > r2["knowledge_confidence"]


# ---------------------------------------------------------------------------
# 持久化与端到端
# ---------------------------------------------------------------------------

def test_persistence(tmp_path):
    path = str(tmp_path / "knowledge.json")
    km1 = KnowledgeMemoryAgent(store_path=path)
    _ingest(km1, "q1", NARROW_Q, SID_PRUNE, PRUNE,
            {"response_time_ms": 10.0}, {"response_time_ms": 50.0})
    assert os.path.exists(path)
    km2 = KnowledgeMemoryAgent(store_path=path)
    assert km2.store.count() == 1
    rec = next(iter(km2.store.all()))
    assert rec["query_record"]["query_id"] == "q1"


def test_decision_agent_consumes_km_output_end_to_end():
    """KM 检索 → 决策 Agent 证据：medium 范围无分区信息时，
    历史证据使 partition_pruning 通过门控并被选中。"""
    km = KnowledgeMemoryAgent()
    for i in range(3):
        _ingest(km, f"p{i}", NARROW_Q, SID_PRUNE, PRUNE,
                {"response_time_ms": 10.0 + i}, {"response_time_ms": 50.0},
                timestamp=1700000000000 + i * 1000)
    _ingest(km, "f0", NARROW_Q, SID_SCAN, SCAN,
            {"response_time_ms": 80.0}, {"response_time_ms": 50.0},
            timestamp=1700000003000)

    analysis_new = analyze(MEDIUM_Q, query_id="new")
    # 无历史：medium 范围无上下文加分、无历史证据 → full_scan
    r_no_hist = decide({"query_analysis": analysis_new})
    assert r_no_hist["decision"]["time_pruning"]["strategy"] == "full_scan"

    # 有历史：KM 检索结果作为决策输入
    km_result = km.retrieve(analysis_new, query_id="new")
    assert km_result["matched_records_for_decision"], "应检索到历史证据"
    r_with_hist = decide({
        "query_analysis": analysis_new,
        "historical_records": km_result["matched_records_for_decision"],
    })
    assert r_with_hist["decision"]["time_pruning"]["strategy"] == "partition_pruning"
    used = [h["record_id"] for h in r_with_hist["evidence"]["historical_records"]
            if h.get("record_id")]
    assert used, "决策应引用历史记录"


def test_km_does_not_decide_strategy():
    # 检索输出不包含任何策略建议字段；策略决策留给 Optimization Decision Agent
    km = KnowledgeMemoryAgent()
    _seed(km)
    r = km.retrieve(analyze(NARROW_Q, query_id="new"))
    blob = json.dumps(r, ensure_ascii=False)
    for bad in ("建议", "应使用", "选择以下策略", "推荐"):
        assert bad not in blob, f"检索输出出现策略建议：{bad}"


def test_determinism():
    km = KnowledgeMemoryAgent()
    _seed(km)
    a = analyze(NARROW_Q, query_id="x")
    r1 = km.retrieve(a)
    r2 = km.retrieve(a)
    assert r1 == r2


def test_stats():
    km = KnowledgeMemoryAgent()
    _seed(km)
    s = km.stats()
    assert s["total_records"] == 5
    assert s["strategy_statistics"][SID_PRUNE]["total"] == 3
    assert s["strategy_statistics"][SID_PRUNE]["success"] == 3
