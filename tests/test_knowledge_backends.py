"""知识库持久化后端（JSONL / SQLite）测试。

覆盖：与 MemoryStore 同协议同语义（检索等价性）、持久化往返、批量落盘、
旧格式迁移（JSONL）、损坏文件兜底、runner 配置接线。
运行：pytest tests/test_knowledge_backends.py -v
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_knowledge_memory_agent import (  # noqa: E402
    AGG_DIM,
    AGG_Q,
    NARROW_Q,
    PRUNE,
    SCAN,
    SID_AGG,
    SID_PRUNE,
    SID_SCAN,
    _ingest,
)

from mataof.agents.knowledge_memory import KnowledgeMemoryAgent  # noqa: E402

BACKENDS = ["jsonl", "sqlite"]


def _seed(backend: str, path: str) -> KnowledgeMemoryAgent:
    km = KnowledgeMemoryAgent(store_path=path, backend=backend, save_interval=1)
    _ingest(km, "q1", NARROW_Q, SID_PRUNE, PRUNE, {"response_time_ms": 10.0},
            {"response_time_ms": 50.0}, timestamp=1700000000000)
    _ingest(km, "q2", NARROW_Q, SID_SCAN, SCAN, {"response_time_ms": 30.0},
            {"response_time_ms": 50.0}, timestamp=1700000001000)
    _ingest(km, "q3", AGG_Q, SID_AGG, AGG_DIM, {"response_time_ms": 20.0},
            {"response_time_ms": 40.0}, timestamp=1700000002000)
    return km


@pytest.mark.parametrize("backend", BACKENDS)
def test_persistence_roundtrip(tmp_path, backend):
    path = str(tmp_path / f"kb.{backend}")
    km = _seed(backend, path)
    km.store.flush()
    assert km.store.count() == 3

    km2 = KnowledgeMemoryAgent(store_path=path, backend=backend, save_interval=1)
    assert km2.store.count() == 3
    assert km2.store.get("rec00000001")["query_record"]["query_id"] == "q1"
    s = km2.stats()
    assert s["total_records"] == 3
    assert s["strategy_statistics"][SID_PRUNE]["total"] == 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_retrieval_equivalence_with_memory_store(tmp_path, backend):
    """同一批记录灌入 MemoryStore 与目标后端 → 检索输出全字段一致。"""
    from mataof.agents.query_analysis import analyze

    km_ref = _seed("json", None)         # 内存参考（json 后端，path=None）
    km_backend = _seed(backend, str(tmp_path / f"kb.{backend}"))

    for q, qid in ((NARROW_Q, "r1"), (AGG_Q, "r2")):
        for km in (km_ref, km_backend):
            _ingest(km, qid, q, SID_PRUNE, PRUNE, {"response_time_ms": 10.0},
                    {"response_time_ms": 50.0}, timestamp=1700000003000)

    db_state = {"device_scale": 1000, "data_scale_level": "small"}
    for qid, q in (("x1", NARROW_Q), ("x2", AGG_Q)):
        an = analyze(q, query_id=qid)
        r_ref = km_ref.retrieve(an, database_state=db_state, query_id=qid,
                                max_records=10)
        r_new = km_backend.retrieve(an, database_state=db_state, query_id=qid,
                                    max_records=10)
        # 签名聚合存储（sqlite）vs 逐条存储（json）的**不变量**逐字段一致：
        # 策略统计（按执行数加权，精确一致）、执行数分层计数、评价表
        assert r_new["historical_summary"]["strategy_statistics"] == \
            r_ref["historical_summary"]["strategy_statistics"]
        assert r_new["match_executions"] == r_ref["match_executions"]
        assert r_new["strategy_evaluations"] == r_ref["strategy_evaluations"]
        assert r_new["knowledge_confidence"] == r_ref["knowledge_confidence"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_save_interval_batching(tmp_path, backend):
    """save_interval>1：未到间隔不落盘，flush 后全部可见。"""
    path = str(tmp_path / f"kb.{backend}")
    km = KnowledgeMemoryAgent(store_path=path, backend=backend, save_interval=5)
    for i in range(7):
        _ingest(km, f"b{i}", NARROW_Q, SID_PRUNE, PRUNE,
                {"response_time_ms": 10.0 + i}, timestamp=1700000000000 + i)
    km.store.flush()

    km2 = KnowledgeMemoryAgent(store_path=path, backend=backend, save_interval=1)
    assert km2.store.count() == 7
    assert km2.store.get("rec00000007") is not None


def test_jsonl_legacy_format_migration(tmp_path):
    """旧格式单文件 JSON 库 → JSONL 自动读取并迁移为追加式布局。"""
    path = str(tmp_path / "kb.jsonl")
    km_old = KnowledgeMemoryAgent(store_path=None)      # 内存生成旧格式
    _ingest(km_old, "q1", NARROW_Q, SID_PRUNE, PRUNE, {"response_time_ms": 10.0},
            {"response_time_ms": 50.0}, timestamp=1700000000000)
    from mataof.agents.knowledge_memory.store import MemoryStore
    legacy = MemoryStore(path, save_interval=1)      # 直接以旧格式整库 JSON 落盘
    for rec in km_old.store.all():
        legacy.add(rec)
    legacy.flush()

    km_new = KnowledgeMemoryAgent(store_path=path, backend="jsonl", save_interval=1)
    assert km_new.store.count() == 1
    _ingest(km_new, "q2", AGG_Q, SID_AGG, AGG_DIM, {"response_time_ms": 20.0},
            {"response_time_ms": 40.0}, timestamp=1700000001000)
    km_new.store.flush()
    assert os.path.exists(f"{path}.records")             # 已迁移为追加式
    km3 = KnowledgeMemoryAgent(store_path=path, backend="jsonl", save_interval=1)
    assert km3.store.count() == 2                        # 迁移后记录不丢


@pytest.mark.parametrize("backend", BACKENDS)
def test_corrupted_file_falls_back_empty(tmp_path, backend):
    path = str(tmp_path / f"bad.{backend}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("NOT JSON AT ALL {{{")
    km = KnowledgeMemoryAgent(store_path=path, backend=backend, save_interval=1)
    assert km.store.count() == 0                         # 兜底空库，不崩溃


def test_runner_config_wires_backend(tmp_path):
    """runner 配置 knowledge_backend → 对应后端落盘格式。"""
    from mataof.runner import PipelineRunner, RunnerConfig
    rf = tmp_path / "results.json"
    rf.write_text(json.dumps({"results": []}), encoding="utf-8")
    for backend in BACKENDS:
        kb_path = str(tmp_path / f"run_{backend}.kb")
        runner = PipelineRunner(RunnerConfig(
            knowledge_store=kb_path, knowledge_backend=backend,
            results_dir=str(tmp_path / f"results_{backend}"),
            executor={"type": "file", "results_file": str(rf)},
            knowledge_save_interval=1,
        ))
        runner.run_query(NARROW_Q, query_id=f"q-{backend}")
        runner.km.store.flush()
        if backend == "jsonl":
            assert os.path.exists(f"{kb_path}.records")
        elif backend == "sqlite":
            import sqlite3
            conn = sqlite3.connect(kb_path)
            n = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
            nsig = conn.execute("SELECT COUNT(*) FROM signatures").fetchone()[0]
            conn.close()
            assert n == 1
            assert nsig == 1
