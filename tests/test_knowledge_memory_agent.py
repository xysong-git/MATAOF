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


PRUNE = {"alignment": "time_aligned", "fetch_size": "fetch_large"}
SCAN = {"alignment": "align_by_device", "fetch_size": "fetch_default"}
SID_PRUNE = "alignment=time_aligned|fetch_size=fetch_large"
SID_SCAN = "alignment=align_by_device|fetch_size=fetch_default"
SID_AGG = "alignment=time_aligned|fetch_size=fetch_default"
AGG_DIM = {"alignment": "time_aligned", "fetch_size": "fetch_default"}


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
    # 无历史：fetch_large 有实测先验加分 → 直接选择
    r_no_hist = decide({"query_analysis": analysis_new})
    assert r_no_hist["decision"]["fetch_size"]["strategy"] == "fetch_large"

    # 有历史：KM 检索结果作为决策输入
    km_result = km.retrieve(analysis_new, query_id="new")
    assert km_result["matched_records_for_decision"], "应检索到历史证据"
    r_with_hist = decide({
        "query_analysis": analysis_new,
        "historical_records": km_result["matched_records_for_decision"],
    })
    assert r_with_hist["decision"]["fetch_size"]["strategy"] == "fetch_large"
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


def test_retrieve_max_records_truncation():
    km = KnowledgeMemoryAgent()
    for i in range(6):
        _ingest(km, f"p{i}", NARROW_Q, SID_PRUNE, PRUNE,
                {"response_time_ms": 10.0 + i}, {"response_time_ms": 50.0})
    r = km.retrieve(analyze(NARROW_Q, query_id="new"), max_records=3)
    assert len(r["matched_records"]) == 3
    assert len(r["matched_records_for_decision"]) == 3
    assert r["truncated"] == {"listed": 3, "total_matched": 6}
    # summary 统计仍基于全部 6 条匹配
    stats = r["historical_summary"]["strategy_statistics"][SID_PRUNE]
    assert stats["total"] == 6
    # 截断按相似度取 top-K（第一条相似度最高）
    sims = [m["similarity"] for m in r["matched_records"]]
    assert sims == sorted(sims, reverse=True)


def test_retrieve_no_truncation_by_default():
    km = KnowledgeMemoryAgent()
    for i in range(3):
        _ingest(km, f"p{i}", NARROW_Q, SID_PRUNE, PRUNE,
                {"response_time_ms": 10.0 + i}, {"response_time_ms": 50.0})
    r = km.retrieve(analyze(NARROW_Q, query_id="new"))
    assert len(r["matched_records"]) == 3   # API 默认不截断
    assert r["truncated"] is None


def test_decision_reuses_precomputed_similarity(monkeypatch):
    """H1：检索记录携带 similarity 时，决策端不再重算。"""
    from mataof.agents.optimization_decision import decide
    import mataof.similarity as sim_mod

    def _fail_if_called(*a, **k):
        raise AssertionError("预计算 similarity 存在时不应重算")

    monkeypatch.setattr(sim_mod, "record_similarity", _fail_if_called)
    analysis = analyze(NARROW_Q, query_id="q1")
    records = [{
        "record_id": "r1", "record_time": 1700000000000,
        "similarity": 0.7,                       # 预计算值
        "query_features": {}, "database_state": {}, "system_state": {},
        "strategy": {"time_pruning": "partition_pruning"},
        "execution_feedback": {"latency_ms": 10.0},
    }]
    r = decide({"query_analysis": analysis, "historical_records": records})
    used = [h for h in r["evidence"]["historical_records"] if h.get("record_id")]
    assert used and used[0]["similarity"] == 0.7
    # 无 similarity 的旧格式输入仍走重算路径（兼容）
    monkeypatch.setattr(sim_mod, "record_similarity",
                        lambda *a, **k: 0.6)
    records[0].pop("similarity")
    r2 = decide({"query_analysis": analysis, "historical_records": records})
    used2 = [h for h in r2["evidence"]["historical_records"] if h.get("record_id")]
    assert used2 and used2[0]["similarity"] == 0.6


def test_runner_evidence_bounded(tmp_path):
    # runner 默认 knowledge_max_records=100：证据清单有界
    rf = tmp_path / "results.json"
    rf.write_text(json.dumps({"results": []}), encoding="utf-8")
    from mataof.runner import PipelineRunner, RunnerConfig
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path / "results"),
        executor={"type": "file", "results_file": str(rf)},
    ))
    trace = runner.run_query(NARROW_Q, query_id="q1")
    used = [h for h in trace["decision"]["evidence"]["historical_records"] if h.get("record_id")]
    assert len(used) <= 100


def test_stats():
    km = KnowledgeMemoryAgent()
    _seed(km)
    s = km.stats()
    assert s["total_records"] == 5
    assert s["strategy_statistics"][SID_PRUNE]["total"] == 3
    assert s["strategy_statistics"][SID_PRUNE]["success"] == 3


# ---------------------------------------------------------------------------
# 等价性：融合快路径 vs 参考逐条路径（检索聚合优化第三步）
# ---------------------------------------------------------------------------

SID_AGG = ("alignment=align_by_device|fetch_size=fetch_large")
AGG_DIM = {"alignment": "align_by_device", "fetch_size": "fetch_large"}


def _seed_mixed(km: KnowledgeMemoryAgent, n_dup: int = 2) -> None:
    """播种多样记录；n_dup 轮完全相同的特征组合 → 制造同分并列（验证证据排序）。"""
    qs = [NARROW_Q, MEDIUM_Q, AGG_Q]
    sids = [SID_PRUNE, SID_SCAN, SID_AGG]
    dims = [PRUNE, SCAN, AGG_DIM]
    for i in range(n_dup):
        for j, q in enumerate(qs):
            lat = 10.0 + 15.0 * j
            _ingest(km, f"m{i}-{j}", q, sids[j], dims[j],
                    {"response_time_ms": lat},
                    {"response_time_ms": lat + 20.0},
                    timestamp=1700000000000 + i * 3_600_000)


def _force_scalar_retrieve(monkeypatch):
    """让 retrieve 内部 import numpy 失败 → 强制逐条参考路径。"""
    import builtins
    real_import = builtins.__import__

    def _no_numpy(name, *a, **k):
        if name == "numpy":
            raise ImportError("test: numpy disabled")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_numpy)


def test_retrieve_fused_path_equals_reference_path(monkeypatch):
    """优化后的融合快路径与逐条参考路径输出**全字段一致**。

    覆盖：top-K matched_records（含相似度数值与顺序）、historical_summary
    （成功/失败模式、策略统计）、match_counts、knowledge_confidence、
    truncated、matched_records_for_decision（证据排序）。
    """
    km = KnowledgeMemoryAgent()
    _seed_mixed(km, n_dup=3)   # 含同分并列
    analysis = analyze(NARROW_Q, query_id="cmp")
    db_state = {"device_scale": 1000, "data_scale_level": "small"}

    r_fast = km.retrieve(analysis, database_state=db_state, query_id="cmp",
                         max_records=3)
    assert r_fast["match_counts"]["strong"] > 0          # 非平凡匹配

    _force_scalar_retrieve(monkeypatch)
    r_ref = km.retrieve(analysis, database_state=db_state, query_id="cmp",
                        max_records=3)

    assert r_ref == r_fast


def test_retrieve_decision_equivalence(monkeypatch):
    """证据 → 最终决策逐字段一致（第三步：验证最终决策）。"""
    km = KnowledgeMemoryAgent()
    _seed_mixed(km, n_dup=3)
    analysis = analyze(MEDIUM_Q, query_id="dec")
    db_state = {"device_scale": 1000, "data_scale_level": "small"}

    r_fast = km.retrieve(analysis, database_state=db_state, query_id="dec",
                         max_records=10)
    d_fast = decide({
        "query_id": "dec", "query_analysis": analysis,
        "database_state": db_state, "system_state": None,
        "historical_records": r_fast["matched_records_for_decision"],
        "reference_time": 1700000000000,
    })
    assert d_fast["selected_strategy"]["strategy_id"]     # 决策非平凡

    _force_scalar_retrieve(monkeypatch)
    r_ref = km.retrieve(analysis, database_state=db_state, query_id="dec",
                        max_records=10)
    d_ref = decide({
        "query_id": "dec", "query_analysis": analysis,
        "database_state": db_state, "system_state": None,
        "historical_records": r_ref["matched_records_for_decision"],
        "reference_time": 1700000000000,
    })

    assert r_ref == r_fast
    assert d_ref == d_fast


def test_aggregate_rows_equals_aggregate_matches():
    """投影行快路径与参考聚合在相同输入下返回逐字段一致的 7 元组。"""
    km = KnowledgeMemoryAgent()
    _seed_mixed(km, n_dup=2)
    store = km.store

    import numpy as np
    from mataof.similarity import (build_vector_matrix, compact_features,
                                   vectorized_similarities)
    from mataof.agents.knowledge_memory.retrieval import (_agg_rows,
                                                          _aggregate_matches,
                                                          _aggregate_rows)

    an = analyze(NARROW_Q, query_id="x")
    cur = compact_features(an)
    db_state = {"device_scale": 1000, "data_scale_level": "small"}
    mat = build_vector_matrix(store)
    q_s, db_s, sy_s, overall = vectorized_similarities(cur, db_state, None, mat)
    order = np.argsort(-overall, kind="stable")

    rows = _agg_rows(store)
    fast = _aggregate_rows(rows, order, overall, q_s, db_s, sy_s,
                           mat["records"], 3)
    ref = _aggregate_matches(
        ((mat["records"][i], float(overall[i]), float(q_s[i]),
          float(db_s[i]), float(sy_s[i])) for i in order), 3)
    assert fast == ref


def test_agg_rows_cache_consistent_after_append():
    """投影行缓存 append-only 增量：追加记录后行数与库一致、检索结果正常。"""
    km = KnowledgeMemoryAgent()
    _seed_mixed(km, n_dup=2)
    store = km.store

    from mataof.agents.knowledge_memory.retrieval import _agg_rows
    rows1 = _agg_rows(store)
    assert len(rows1) == store.count() == 6

    _ingest(km, "extra", AGG_Q, SID_AGG, AGG_DIM, {"response_time_ms": 30.0},
            {"response_time_ms": 55.0}, timestamp=1700000000000)
    rows2 = _agg_rows(store)
    assert len(rows2) == store.count() == 7
    assert rows2[:6] == rows1                    # 旧行原样保留（append-only）

    an = analyze(AGG_Q, query_id="c2")
    r = km.retrieve(an, database_state={"device_scale": 1000,
                                        "data_scale_level": "small"},
                    query_id="c2", max_records=100)
    # 新记录已参与匹配（max_records=100 ≥ 全匹配数 → 全部列出）
    listed_ids = [m["record_id"] for m in r["matched_records"]]
    assert "rec00000007" in listed_ids


def test_retrieve_timing_out_diagnostic():
    """timing_out 只作诊断侧信道：返回值保持确定性（不含计时字段）。"""
    from mataof.agents.knowledge_memory.retrieval import retrieve
    km = KnowledgeMemoryAgent()
    _seed_mixed(km, n_dup=1)
    an = analyze(NARROW_Q, query_id="t1")

    r1 = retrieve(km.store, an, None, None, "t1")
    timing: dict = {}
    r2 = retrieve(km.store, an, None, None, "t1", timing_out=timing)
    assert r1 == r2                              # 计时不影响输出
    assert "timing_ms" not in r2
    for key in ("matrix_ms", "scan_ms", "argsort_ms", "match_loop_ms", "total_ms"):
        assert key in timing and timing[key] >= 0


def test_retrieve_summary_false_decision_fields_identical():
    """决策路径（summary=False）与完整路径的**决策消费字段**逐字段一致。"""
    km = KnowledgeMemoryAgent()
    _seed_mixed(km, n_dup=3)
    an = analyze(NARROW_Q, query_id="s")
    db_state = {"device_scale": 1000, "data_scale_level": "small"}
    r_full = km.retrieve(an, database_state=db_state, query_id="s", max_records=5)
    r_fast = km.retrieve(an, database_state=db_state, query_id="s", max_records=5,
                         summary=False)
    for key in ("matched_records", "matched_records_for_decision", "match_counts",
                "knowledge_confidence", "truncated", "strategy_evaluations"):
        assert r_fast[key] == r_full[key], f"决策字段不一致: {key}"
    assert r_fast["summary_skipped"] is True
    assert r_full["summary_skipped"] is False
    # 展示聚合：快路径置空，完整路径有内容
    assert r_fast["historical_summary"] == {"successful_strategies": [],
                                            "failed_strategies": [],
                                            "strategy_statistics": {}}
    assert r_full["historical_summary"]["strategy_statistics"]
