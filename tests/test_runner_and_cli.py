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
    # stdout 必须是纯 JSON（摘要走 stderr）
    captured = capsys.readouterr()
    traces = json.loads(captured.out)
    assert isinstance(traces, list) and traces[0]["query_id"] == "q1"
    assert traces[0]["monitoring"]["performance_assessment"] == "improved"
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
