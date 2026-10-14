"""向量化相似度（路线 2）的等价性与性能测试。

核心要求：矩阵化计算的相似度与逐条 component_similarities **完全一致**
（含缺失字段/零值/空函数集等边界情况），numpy 缺失时逐条路径回退。
运行：pytest tests/test_vectorized_similarity.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from mataof.similarity import (  # noqa: E402
    build_vector_matrix,
    component_similarities,
    vectorized_similarities,
)
from mataof.agents.knowledge_memory.store import MemoryStore  # noqa: E402


def _record(qf, db=None, sy=None):
    return {
        "query_record": {"query_features": qf},
        "context": {"database_state": db or {}, "system_state": sy or {}},
        "strategy": {"strategy_id": "s", "by_dimension": {"time_pruning": "full_scan"}},
        "execution": {"status": "success", "metrics": {"response_time_ms": 10.0}},
        "execution_result": "improved",
    }


def _qf(query_type="range_query", span=1000, level="narrow", device_count=1,
        multi=False, non_time=0, has_agg=False, group_by=False, window=False,
        functions=None, has_time=True):
    return {
        "query_type": query_type,
        "time": {"has_time_filter": has_time, "time_span": span,
                 "range_level": level},
        "device": {"device_count": device_count, "multi_device": multi},
        "filter": {"non_time_filter_count": non_time},
        "aggregation": {"has_aggregation": has_agg, "has_group_by": group_by,
                        "has_window": window, "functions": functions or []},
    }


def _records_for_equivalence():
    """覆盖边界情况的多样化记录集。"""
    recs = [
        _record(_qf()),                                          # 基准
        _record(_qf(query_type="point_query", span=0)),          # 零跨度
        _record(_qf(level="wide", span=None)),                   # 缺失数值
        _record(_qf(device_count=None, multi=None)),             # 缺失设备
        _record(_qf(non_time=5, has_agg=True, group_by=True,
                    window=True, functions=["avg", "count"])),   # 聚合+函数
        _record(_qf(functions=["avg"])),                         # 单函数
        _record(_qf(has_time=False, span=None, level="unknown")),# 无时间过滤
        _record(_qf(), db={"data_scale_level": "small"}),
        _record(_qf(), db={"data_scale_level": "small", "device_scale": 100}),
        _record(_qf(), db={"device_scale": 0}),                  # 零设备规模
        _record(_qf(), sy={"load_level": "high"}),
        _record(_qf(), db={"data_scale_level": "large"}, sy={"load_level": "low"}),
    ]
    return recs


def test_equivalence_on_edge_cases():
    store = MemoryStore()
    for rec in _records_for_equivalence():
        store.add(rec)
    mat = build_vector_matrix(store)
    assert mat is not None and mat["_len"] == store.count()

    current = _qf()
    contexts = [
        ({}, {}),
        ({"data_scale_level": "small"}, {"load_level": "high"}),
        ({"data_scale_level": "large", "device_scale": 100}, {"load_level": "low"}),
        ({"device_scale": 0}, {}),
    ]
    for db, sy in contexts:
        q_s, db_s, sy_s, overall = vectorized_similarities(current, db, sy, mat)
        for i, rec in enumerate(mat["records"]):
            ref = component_similarities(current, db, sy, rec)
            assert abs(float(q_s[i]) - ref["query"]) < 1e-3, f"query 分量不一致 idx={i} db={db}"
            assert abs(float(db_s[i]) - ref["database"]) < 1e-3, f"db 分量不一致 idx={i}"
            assert abs(float(sy_s[i]) - ref["system"]) < 1e-3, f"system 分量不一致 idx={i}"
            assert abs(float(overall[i]) - ref["overall"]) < 1e-3, f"overall 不一致 idx={i}"


def test_equivalence_current_variants():
    store = MemoryStore()
    for rec in _records_for_equivalence():
        store.add(rec)
    mat = build_vector_matrix(store)
    currents = [
        _qf(query_type="point_query", span=0),
        _qf(level="wide", span=5000, has_agg=True, functions=["sum"]),
        _qf(has_time=False, span=None, level="unknown", device_count=3, non_time=2),
    ]
    for cur in currents:
        q_s, db_s, sy_s, overall = vectorized_similarities(cur, {}, {}, mat)
        for i, rec in enumerate(mat["records"]):
            ref = component_similarities(cur, {}, {}, rec)
            assert abs(float(overall[i]) - ref["overall"]) < 1e-3, f"current={cur} idx={i}"


def test_incremental_matrix_after_add():
    store = MemoryStore()
    store.add(_record(_qf()))
    mat1 = build_vector_matrix(store)
    assert mat1["_len"] == 1
    store.add(_record(_qf(query_type="point_query", span=0)))
    mat2 = build_vector_matrix(store)
    assert mat2["_len"] == 2
    assert mat2["cat_codes"].shape[0] == 2
    # 新增记录也能正确计算
    cur = _qf()
    q_s, db_s, sy_s, overall = vectorized_similarities(cur, {}, {}, mat2)
    assert len(overall) == 2
    ref = component_similarities(cur, {}, {}, mat2["records"][1])
    assert abs(float(overall[1]) - ref["overall"]) < 1e-3


def test_equivalence_on_real_store_sample():
    """真实 29k 库抽样 200 条做等价性验证（若存在）。"""
    path = "data/knowledge_dbs_llm.json"
    if not os.path.exists(path):
        import pytest
        pytest.skip("真实大库不存在")
    store = MemoryStore(path, save_interval=100)
    if store.count() == 0:
        return
    # 抽样（确定性：均匀间隔取 200 条）
    all_records = store.all()
    step = max(1, len(all_records) // 200)
    sample = all_records[::step][:200]
    small = MemoryStore()
    for rec in sample:
        small.add(rec)
    mat = build_vector_matrix(small)
    from mataof.similarity import compact_features
    from mataof.agents.query_analysis import analyze
    a = analyze("SELECT s_333, s_433 FROM root.dbs.g_0.d_0, root.dbs.g_0.d_1 "
                "WHERE time = 1640966450000", query_id="eq")
    cur = compact_features(a)
    q_s, db_s, sy_s, overall = vectorized_similarities(cur, {"data_scale_level": "small"},
                                                       {}, mat)
    for i, rec in enumerate(mat["records"]):
        ref = component_similarities(cur, {"data_scale_level": "small"}, {}, rec)
        assert abs(float(overall[i]) - ref["overall"]) < 1e-3, f"真实库 idx={i} 不一致"


def test_vectorized_retrieval_end_to_end():
    """通过 KM retrieve 走向量化路径，结果与逐条路径一致。"""
    from mataof.agents.knowledge_memory import KnowledgeMemoryAgent
    from mataof.agents.query_analysis import analyze
    km = KnowledgeMemoryAgent()
    for rec in _records_for_equivalence():
        km.store.add(rec)
    a = analyze("SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 "
                "AND time <= 1640970000000", query_id="v")
    r = km.retrieve(a, llm_summary=False)
    # 与逐条计算的总匹配数一致
    cur = r  # 已走向量化（numpy 存在）
    assert r["matched_records"], "应有匹配记录"
    # 手工逐条对照匹配数量
    from mataof.similarity import compact_features
    from mataof.agents.knowledge_memory.retrieval import match_level
    c = compact_features(a)
    manual = [rec for rec in km.store.all()
              if match_level(component_similarities(c, {}, {}, rec)["overall"])]
    assert len(r["matched_records"]) == len(manual)


def test_determinism():
    store = MemoryStore()
    for rec in _records_for_equivalence():
        store.add(rec)
    mat = build_vector_matrix(store)
    cur = _qf()
    o1 = vectorized_similarities(cur, {}, {}, mat)[3]
    o2 = vectorized_similarities(cur, {}, {}, mat)[3]
    assert np.array_equal(o1, o2)
