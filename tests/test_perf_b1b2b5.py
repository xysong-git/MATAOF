"""B1（合并 LLM 调用）/ B2（查询级并行）/ B5（批量落盘）的单元测试。

运行：pytest tests/test_perf_b1b2b5.py -v
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.knowledge_memory import KnowledgeMemoryAgent  # noqa: E402
from mataof.agents.knowledge_memory.store import MemoryStore  # noqa: E402
from mataof.runner import PipelineRunner, RunnerConfig  # noqa: E402

NARROW_Q = ("SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 "
            "AND time <= 1640970000000")


class FakeLLM:
    """返回完整合并字段的 mock（QA/决策字段可空，合并字段齐全）。"""

    name = "fake"

    def __init__(self):
        self.calls = 0
        self.prompts = []

    def chat(self, messages, max_tokens=2048, timeout_s=60.0):
        self.calls += 1
        self.prompts.append(messages[1]["content"] if len(messages) > 1 else "")
        return json.dumps({
            "semantic_summary": "语义摘要",
            "query_type_hint": "unknown",
            "condition_hints": {},
            "semantic_reason": "决策解读",
            "preferences": {"time_pruning": ["full_scan"], "filter_order": [],
                            "aggregation_placement": []},
            "performance_summary": "延迟 12.3 ms，相对 baseline 降低 38.5%。",
            "anomaly_summary": None,
            "experience_note": "该策略在窄时间范围查询上表现良好。",
        }, ensure_ascii=False)


def _records(count=3):
    return [{"query_record": {"query_id": f"q{i}",
                              "query_features": {"query_type": "range_query"}},
             "strategy": {"strategy_id": "s",
                          "by_dimension": {"time_pruning": "partition_pruning"}},
             "execution": {"status": "success",
                           "metrics": {"response_time_ms": 10.0}},
             "execution_result": "improved"}
            for i in range(count)]


# ---------------------------------------------------------------------------
# B5：批量落盘与线程安全
# ---------------------------------------------------------------------------

def test_save_interval_batching(tmp_path):
    path = str(tmp_path / "k.json")
    store = MemoryStore(path, save_interval=5)
    for rec in _records(3):
        store.add(rec)
    assert not os.path.exists(path)          # 未达阈值不落盘
    for rec in _records(3)[:2]:
        store.add(rec)
    assert os.path.exists(path)              # 第 5 次落盘
    store.flush()                            # flush 幂等安全


def test_flush_persists_tail(tmp_path):
    path = str(tmp_path / "k.json")
    store = MemoryStore(path, save_interval=10)
    store.add(_records(1)[0])
    assert not os.path.exists(path)
    store.flush()
    reloaded = MemoryStore(path)
    assert reloaded.count() == 1


def test_store_thread_safety(tmp_path):
    import threading
    path = str(tmp_path / "k.json")
    store = MemoryStore(path, save_interval=20)
    errors = []

    def worker(n):
        try:
            for i in range(n):
                rec = _records(1)[0]
                store.add(rec)
                store.record_evaluation(rec)
                store.evaluations()
                store.count()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(25,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert store.count() == 200
    store.flush()
    assert MemoryStore(path).count() == 200


# ---------------------------------------------------------------------------
# B1：合并 LLM 调用（4→3 次/条）
# ---------------------------------------------------------------------------

def test_combined_llm_call_counts(tmp_path):
    # 带真实执行数据（FileExecutor 回放），保证数字校验有事实集
    rf = tmp_path / "exec.json"
    rf.write_text(json.dumps({"results": [
        {"query_id": "q1", "execution_status": "success",
         "metrics": {"response_time_ms": 12.3},
         "baseline_metrics": {"response_time_ms": 20.0}},
    ]}), encoding="utf-8")
    llm = FakeLLM()
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path),
        executor={"type": "file", "results_file": str(rf)},
        llm={"provider": "none"},   # llm 客户端由下面注入
    ))
    runner.llm = llm
    t = runner.run_query(NARROW_Q, query_id="q1")
    # 三条 LLM 调用：QA 摘要、决策解读、合并（监控+KM）
    assert llm.calls == 3, f"期望 3 次调用，实际 {llm.calls}"
    # 监控 llm_analysis 由合并调用填充
    la = t["monitoring"]["llm_analysis"]
    assert la["performance_summary"] is not None
    # 知识记录里 llm_note 由合并调用生成（未再发起第 4 次调用）
    rec = runner.km.store.get(t["knowledge_update"]["record_id"])
    assert rec.get("llm_note") is not None
    assert "窄时间范围" in rec["llm_note"]


def test_combined_call_number_validation(tmp_path):
    class FakeLLMFabricated(FakeLLM):
        def chat(self, messages, max_tokens=2048, timeout_s=60.0):
            self.calls += 1
            return json.dumps({
                "performance_summary": "延迟 99.9 ms",   # 编造数字
                "anomaly_summary": None,
                "experience_note": "经验总结",
            }, ensure_ascii=False)
    llm = FakeLLMFabricated()
    runner = PipelineRunner(RunnerConfig(results_dir=str(tmp_path)))
    runner.llm = llm
    t = runner.run_query(NARROW_Q, query_id="q1")
    la = t["monitoring"]["llm_analysis"]
    assert la["performance_summary"] is None          # 数字校验拒绝编造
    assert any("疑似编造" in n for n in la["notes"])
    # 经验总结仍生效（无数字约束字段）
    rec = runner.km.store.get(t["knowledge_update"]["record_id"])
    assert rec.get("llm_note") == "经验总结"


def test_llm_not_sampled_zero_calls(tmp_path):
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path),
        llm={"provider": "none", "sample_rate": 10},
    ))
    runner.llm = FakeLLM()          # 注入；但采样逻辑应使其前几条不被调用
    runner._llm_every = 10          # 强制每 10 条采样
    t = runner.run_query(NARROW_Q, query_id="q1")   # seq=1，不采样
    assert runner.llm.calls == 0
    assert t["monitoring"]["llm_analysis"]["available"] is False


# ---------------------------------------------------------------------------
# B2：查询级并行
# ---------------------------------------------------------------------------

def test_parallel_run_statements(tmp_path):
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path),
        llm={"provider": "none"},
        run_id="par",
    ))
    stmts = [NARROW_Q] * 8
    traces = runner.run_statements(stmts, source="test.sql", workers=4)
    assert len(traces) == 8
    # query_id 唯一（线程安全序号）
    ids = [t["query_id"] for t in traces]
    assert len(set(ids)) == 8
    # 知识库一致
    assert runner.km.store.count() == 8
    # 结果按输入顺序（ThreadPoolExecutor.map 保序）
    assert ids == sorted(ids)


def test_parallel_run_file_with_real_store(tmp_path):
    path = str(tmp_path / "k.json")
    f = tmp_path / "q.sql"
    f.write_text(";".join([NARROW_Q] * 6) + ";", encoding="utf-8")
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path / "results"),
        knowledge_store=path, knowledge_save_interval=20,
        run_id="parf",
    ))
    traces = runner.run_file(str(f), workers=4)
    assert len(traces) == 6
    runner.km.store.flush()
    assert MemoryStore(path).count() == 6
