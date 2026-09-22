"""PipelineRunner 与 CLI 正式入口的单元测试。

覆盖：NullExecutor 不虚构、FileExecutor 装载与缺失查询、Runner 闭环
（第二次查询复用历史经验）、追踪落盘内容、CLI 子命令与退出码。
运行：pytest tests/test_runner_and_cli.py -v
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.runner import (  # noqa: E402
    PipelineRunner,
    RunnerConfig,
    load_config_file,
    split_sql_statements,
)
from mataof.executors import FileExecutor, NullExecutor, build_executor  # noqa: E402
from mataof.cli import main  # noqa: E402

NARROW_Q = "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000"
MEDIUM_Q = "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1644566405000"


def _results_file(tmp_path, items: list) -> str:
    p = tmp_path / "execution_results.json"
    p.write_text(json.dumps({"results": items}), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------

def test_null_executor_does_not_fabricate(tmp_path):
    runner = PipelineRunner(RunnerConfig(results_dir=str(tmp_path)))
    trace = runner.run_query(NARROW_Q, query_id="q1")
    assert trace["execution"]["execution_status"] == "unknown"
    assert trace["execution"]["metrics"] == {}
    assert trace["monitoring"]["metrics"]["response_time_ms"] is None
    assert trace["monitoring"]["performance_assessment"] == "insufficient_evidence"
    assert trace["knowledge_update"]["stored"] is True   # 状态事实（unknown）照实记录


def test_file_executor_loads_and_missing_query(tmp_path):
    rf = _results_file(tmp_path, [
        {"query_id": "q1", "execution_status": "success",
         "metrics": {"response_time_ms": 10.0},
         "baseline_metrics": {"response_time_ms": 50.0}, "timestamp": 1700000000000},
    ])
    ex = FileExecutor(rf)
    r = ex.execute(NARROW_Q, "q1", {})
    assert r.execution_status == "success"
    assert r.metrics["response_time_ms"] == 10.0
    assert r.timestamp == 1700000000000
    # 缺失查询 → unknown，不编造
    r2 = ex.execute(NARROW_Q, "q2", {})
    assert r2.execution_status == "unknown"
    assert r2.metrics == {}


def test_build_executor_defaults():
    assert isinstance(build_executor(None), NullExecutor)
    assert isinstance(build_executor("null"), NullExecutor)


# ---------------------------------------------------------------------------
# Runner 闭环
# ---------------------------------------------------------------------------

def test_runner_closed_loop_reuses_history(tmp_path):
    rf = _results_file(tmp_path, [
        {"query_id": "q1", "execution_status": "success",
         "metrics": {"response_time_ms": 10.0},
         "baseline_metrics": {"response_time_ms": 50.0}, "timestamp": 1700000000000},
        {"query_id": "q2", "execution_status": "success",
         "metrics": {"response_time_ms": 12.0},
         "baseline_metrics": {"response_time_ms": 50.0}, "timestamp": 1700000001000},
    ])
    config = RunnerConfig(
        results_dir=str(tmp_path / "results"),
        executor={"type": "file", "results_file": rf},
        database_state={"partition_info": {"total_partitions": 128, "covered_partitions": 2}},
    )
    runner = PipelineRunner(config)

    t1 = runner.run_query(NARROW_Q, query_id="q1")
    assert t1["monitoring"]["performance_assessment"] == "improved"
    assert t1["knowledge_update"]["stored"] is True

    # 第二次：medium 范围、无分区信息（配置相同数据库状态，但查询本身跨度不同）
    # 历史证据应使决策选择 partition_pruning
    t2 = runner.run_query(MEDIUM_Q, query_id="q2")
    assert t2["knowledge_retrieval"]["matched_count"] > 0
    assert t2["decision"]["decision"]["time_pruning"]["strategy"] == "partition_pruning"
    used = [h["record_id"] for h in t2["decision"]["evidence"]["historical_records"]
            if h.get("record_id")]
    assert used

    # 追踪落盘
    trace_file = tmp_path / "results" / "q2.json"
    assert trace_file.exists()
    saved = json.loads(trace_file.read_text(encoding="utf-8"))
    assert saved["query_id"] == "q2"
    assert saved["decision"]["selected_strategy"]["strategy_id"] == \
        t2["decision"]["selected_strategy"]["strategy_id"]
    log_file = tmp_path / "results" / "run_log.jsonl"
    lines = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2 and lines[1]["query_id"] == "q2"


def test_runner_run_file_sql(tmp_path):
    rf = _results_file(tmp_path, [])
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path / "results"),
        executor={"type": "file", "results_file": rf},
    ))
    f = tmp_path / "queries.sql"
    f.write_text(f"{NARROW_Q};{MEDIUM_Q};", encoding="utf-8")
    traces = runner.run_file(str(f))
    assert len(traces) == 2


def test_split_sql_statements_strips_comments():
    text = ("-- Q1 generated 2000 queries\n"
            f"{NARROW_Q};\n"
            "-- 第二条注释\n"
            f"{MEDIUM_Q};")
    stmts = split_sql_statements(text)
    assert stmts == [NARROW_Q, MEDIUM_Q]
    assert all(not s.startswith("--") for s in stmts)


def test_runner_run_file_with_comments_executes_clean_sql(tmp_path):
    rf = _results_file(tmp_path, [])
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path / "results"),
        executor={"type": "file", "results_file": rf},
    ))
    f = tmp_path / "queries.sql"
    f.write_text(f"-- 注释头\n{NARROW_Q};", encoding="utf-8")
    traces = runner.run_file(str(f))
    assert len(traces) == 1
    assert not traces[0]["query"].startswith("--")


def test_runner_without_results_dir(tmp_path):
    runner = PipelineRunner(RunnerConfig())   # 全部默认：不落盘、NullExecutor
    trace = runner.run_query(NARROW_Q, query_id="q1")
    assert trace["query_id"] == "q1"
    assert runner.stats()["total_records"] == 1


def test_load_config_file(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "knowledge_store": str(tmp_path / "k.json"),
        "results_dir": str(tmp_path / "r"),
        "executor": {"type": "null"},
    }), encoding="utf-8")
    config = load_config_file(str(cfg))
    assert config.knowledge_store == str(tmp_path / "k.json")
    assert config.results_dir == str(tmp_path / "r")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cfg(tmp_path, results_file=None, results_dir=None, store=None) -> str:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "knowledge_store": store or str(tmp_path / "k.json"),
        "results_dir": results_dir or str(tmp_path / "results"),
        "executor": {"type": "file", "results_file": results_file}
                    if results_file else {"type": "null"},
    }), encoding="utf-8")
    return str(cfg)


def test_cli_run_json(tmp_path, capsys):
    rf = _results_file(tmp_path, [
        {"query_id": "q1", "execution_status": "success",
         "metrics": {"response_time_ms": 10.0},
         "baseline_metrics": {"response_time_ms": 50.0}},
    ])
    cfg = _cfg(tmp_path, results_file=rf)
    f = tmp_path / "queries.jsonl"
    f.write_text(json.dumps({"query_id": "q1", "query": NARROW_Q}), encoding="utf-8")
    code = main(["run", "--config", cfg, "--json", "--file", str(f)])
    assert code == 0
    # stdout 必须是纯 JSON：{traces, batch_summary}（摘要走 stderr）
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, dict) and isinstance(data["traces"], list)
    assert data["traces"][0]["query_id"] == "q1"
    assert data["traces"][0]["monitoring"]["performance_assessment"] == "improved"
    s = data["batch_summary"]
    assert s["total_queries"] == 1
    assert s["execution"]["success_rate"] == 1.0
    assert s["latency_ms"]["samples"] == 1
    assert "assessment" in captured.err or "status" in captured.err


def test_cli_run_requires_query(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    code = main(["run", "--config", cfg])
    assert code == 2


def test_cli_stats_and_retrieve(tmp_path):
    cfg = _cfg(tmp_path)
    assert main(["run", "--config", cfg, NARROW_Q]) == 0
    assert main(["stats", "--config", cfg]) == 0
    assert main(["retrieve", "--config", cfg, NARROW_Q]) == 0
    assert main(["retrieve", "--config", cfg, "--json", NARROW_Q]) == 0


def test_cli_run_file(tmp_path):
    f = tmp_path / "queries.sql"
    f.write_text(f"{NARROW_Q};", encoding="utf-8")
    cfg = _cfg(tmp_path)
    assert main(["run", "--config", cfg, "--file", str(f)]) == 0


def test_cli_run_limit(tmp_path):
    f = tmp_path / "queries.sql"
    f.write_text(f"{NARROW_Q};{MEDIUM_Q};{NARROW_Q};", encoding="utf-8")
    cfg = _cfg(tmp_path, results_dir=str(tmp_path / "results"))
    assert main(["run", "--config", cfg, "--file", str(f), "--limit", "2"]) == 0
    log = (tmp_path / "results" / "run_log.jsonl").read_text(encoding="utf-8")
    assert len(log.strip().splitlines()) == 2


def test_unique_query_ids_across_runs_no_overwrite(tmp_path):
    # 两次独立运行（不同 run_id）写同一 results_dir → 文件互不覆盖
    results_dir = str(tmp_path / "results")
    runner1 = PipelineRunner(RunnerConfig(results_dir=results_dir, run_id="runA"))
    t1 = runner1.run_query(NARROW_Q)
    runner2 = PipelineRunner(RunnerConfig(results_dir=results_dir, run_id="runB"))
    t2 = runner2.run_query(NARROW_Q)
    assert t1["query_id"].startswith("runA-")
    assert t2["query_id"].startswith("runB-")
    files = sorted(os.listdir(results_dir))
    assert f"{t1['query_id']}.json" in files and f"{t2['query_id']}.json" in files


def test_write_trace_never_overwrites_same_query_id(tmp_path):
    # 极端情况：同名 query_id 写两次 → 第二个文件自动加后缀，第一个保留
    results_dir = str(tmp_path / "results")
    runner = PipelineRunner(RunnerConfig(results_dir=results_dir))
    t1 = runner.run_query(NARROW_Q, query_id="fixed")
    t2 = runner.run_query(NARROW_Q, query_id="fixed")
    files = sorted(os.listdir(results_dir))
    assert "fixed.json" in files and "fixed-2.json" in files
    saved1 = json.loads(open(os.path.join(results_dir, "fixed.json")).read())
    assert saved1["query"] == NARROW_Q


def test_run_file_records_source(tmp_path):
    rf = _results_file(tmp_path, [])
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path / "results"),
        executor={"type": "file", "results_file": rf},
        run_id="runA",
    ))
    f = tmp_path / "q1.sql"
    f.write_text(f"{NARROW_Q};", encoding="utf-8")
    traces = runner.run_file(str(f))
    assert traces[0]["source"] == "q1.sql"
    log = (tmp_path / "results" / "run_log.jsonl").read_text(encoding="utf-8")
    assert json.loads(log.splitlines()[0])["source"] == "q1.sql"


def test_run_file_per_file_limit_seq(tmp_path):
    rf = _results_file(tmp_path, [])
    results_dir = str(tmp_path / "results")
    runner = PipelineRunner(RunnerConfig(
        results_dir=results_dir, executor={"type": "file", "results_file": rf},
        per_file_limit=2, run_id="seqrun",
    ))
    qs = ["SELECT s_1 FROM root.a.d WHERE time = 1", "SELECT s_2 FROM root.a.d WHERE time = 2",
          "SELECT s_3 FROM root.a.d WHERE time = 3",
          "SELECT s_4 FROM root.b.d WHERE time = 1", "SELECT s_5 FROM root.b.d WHERE time = 2",
          "SELECT s_6 FROM root.b.d WHERE time = 3"]
    f1 = tmp_path / "q1.sql"; f1.write_text(";".join(qs[:3]) + ";", encoding="utf-8")
    f2 = tmp_path / "q2.sql"; f2.write_text(";".join(qs[3:]) + ";", encoding="utf-8")
    t1 = runner.run_file(str(f1))
    t2 = runner.run_file(str(f2))
    assert [t["query"] for t in t1] == qs[:2]          # 每个文件各取前 2 条
    assert [t["query"] for t in t2] == qs[3:5]
    assert t1[0]["sampling"] == {"mode": "seq", "limit": 2, "total": 3}
    log = (tmp_path / "results" / "run_log.jsonl").read_text(encoding="utf-8")
    assert json.loads(log.splitlines()[0])["sampling"]["mode"] == "seq"


def test_run_file_sample_random_reproducible(tmp_path):
    rf = _results_file(tmp_path, [])
    qs = [f"SELECT s_{i} FROM root.a.d WHERE time = {i}" for i in range(6)]
    f = tmp_path / "q.sql"; f.write_text(";".join(qs) + ";", encoding="utf-8")

    def run(seed):
        runner = PipelineRunner(RunnerConfig(
            results_dir=str(tmp_path / f"r{seed}"),
            executor={"type": "file", "results_file": rf},
            per_file_limit=2, sample_mode="random", sample_seed=seed, run_id="r",
        ))
        return [t["query"] for t in runner.run_file(str(f))]

    a1, a2 = run(7), run(7)          # 同种子 → 完全一致（可复现）
    assert a1 == a2
    assert len(a1) == 2
    b = run(99)
    assert len(b) == 2


def test_cli_per_file_limit_and_sample(tmp_path):
    rf = _results_file(tmp_path, [])
    results_dir = str(tmp_path / "results")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "results_dir": results_dir,
        "executor": {"type": "file", "results_file": rf},
    }), encoding="utf-8")
    f = tmp_path / "q.sql"
    f.write_text(";".join(f"SELECT s_{i} FROM root.a.d WHERE time = {i}" for i in range(5)) + ";",
                 encoding="utf-8")
    code = main(["run", "--config", str(cfg), "--file", str(f),
                 "--per-file-limit", "2", "--sample", "random", "--seed", "7"])
    assert code == 0
    lines = [json.loads(l) for l in
             (tmp_path / "results" / "run_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["sampling"] == {"mode": "random", "limit": 2, "total": 5, "seed": 7}


def test_llm_sample_rate(tmp_path):
    # sample_rate=3：第 3 条查询才启用 LLM，其余走确定性快路径
    from mataof.agents.query_analysis.agent import QueryAnalysisAgent
    class _FakeLLM:
        name = "fake"
        def __init__(self):
            self.calls = 0
        def chat(self, messages, max_tokens=2048, timeout_s=60.0):
            self.calls += 1
            return '{"semantic_summary": "x", "query_type_hint": "unknown", "condition_hints": {}}'
    fake = _FakeLLM()
    runner = PipelineRunner(RunnerConfig(results_dir=str(tmp_path),
                                         llm={"provider": "none", "sample_rate": 3}))
    runner.llm = fake   # 注入 mock（覆盖 provider=none 的 None）
    for i in range(1, 4):
        t = runner.run_query(NARROW_Q, query_id=f"q{i}")
        avail = t["analysis"]["llm_analysis"]["available"]
        if i == 3:
            assert avail is True
        else:
            assert avail is False
    assert fake.calls >= 1
