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
    build_historical_reference,
    load_config_file,
    load_device_inventory,
    split_sql_statements,
    validate_device_inventory,
)
from mataof.executors import FileExecutor, NullExecutor, build_executor  # noqa: E402
from mataof.cli import main  # noqa: E402
from mataof.agents.query_analysis import analyze  # noqa: E402
from mataof.similarity import compact_features  # noqa: E402

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

    # 第二次：同结构查询，历史证据应被检索并引用（fetch 维度照常选择）
    t2 = runner.run_query(MEDIUM_Q, query_id="q2")
    assert t2["knowledge_retrieval"]["matched_count"] > 0
    assert t2["decision"]["decision"]["fetch_size"]["strategy"] == "fetch_large"
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


def _hist_record(record_id: str, plan: str, latency: float,
                 similarity: float = 0.9, count: int = 1,
                 query_features: dict | None = None,
                 database_state: dict | None = None) -> dict:
    return {
        "record_id": record_id,
        "query_features": query_features or {},
        "database_state": database_state or {},
        "strategy": {"plan": plan} if plan else {},
        "execution_feedback": {"latency_ms": latency},
        "similarity": similarity,
        "count": count,
    }


def test_historical_reference_prefers_decision_baseline_history():
    decision = {
        "plan_selection": {
            "single_ms": 12.5, "single_source": "history_baseline",
            "baseline_plan_id": "baseline_single_df",
            "evaluations": [{"plan_id": "baseline_single_df", "history_count": 4}],
        },
    }
    ref = build_historical_reference(decision, [])
    assert ref == {"response_time_ms": 12.5, "source": "history_baseline",
                   "plan_id": "baseline_single_df", "count": 4,
                   "scope": "similar_records"}


def test_historical_reference_prefers_adequate_baseline_history():
    # baseline 历史样本充分（>=3 且 >= 同形状计划证据的 10%）→ 用 baseline 衡量优化收益
    decision = {"selected_strategy": {"execution_plan": {"plan_id": "exec_fetch_10000_df"}}}
    records = [_hist_record("r1", "baseline_single_df", 10.0, count=5),
               _hist_record("r2", "exec_fetch_10000_df", 8.0, count=20)]
    ref = build_historical_reference(decision, records)
    assert ref["source"] == "history_baseline_records"
    assert ref["response_time_ms"] == 10.0


def test_historical_reference_rejects_sparse_cold_baseline_history():
    # q1 实况：baseline 只被选择性执行过 9 次（1 次冷启动 1542ms + 8 次
    # 48.4ms），而所选计划在本查询形状上有 19999 次 13.29ms 的实测。
    # baseline 样本不足同计划的 10% → 不得用 271ms 的加权基线把 12ms 误判为
    # improved；改用同计划实测 13.29ms。
    decision = {"selected_strategy": {"execution_plan": {"plan_id": "exec_fetch_10000_df"}}}
    records = [
        _hist_record("r1", "baseline_single_df", 1542.03, similarity=0.7684, count=1),
        _hist_record("r2", "baseline_single_df", 48.4, similarity=0.5474, count=8),
        _hist_record("r3", "exec_fetch_10000_df", 13.29, similarity=0.7684, count=19999),
    ]
    ref = build_historical_reference(decision, records)
    assert ref["source"] == "history_same_plan"
    assert ref["response_time_ms"] == 13.29
    assert ref["count"] == 19999


def test_historical_reference_ignores_sparse_baseline_even_for_baseline_plan():
    # 所选计划就是 baseline 时也不能无条件用它：q1 实况 baseline 只有 9 次
    # （含 1542ms 冷启动），同形状其他计划有数万次实测。稀疏 baseline 不能
    # 把 12ms 的正常执行误判为 improved，应改用相似历史典型表现。
    decision = {"selected_strategy": {"execution_plan": {
        "plan_id": "baseline_single_df"}}}
    records = [
        _hist_record("r1", "baseline_single_df", 1542.03,
                     similarity=0.7684, count=1),
        _hist_record("r2", "exec_fetch_10000_df", 13.29,
                     similarity=0.7684, count=19999),
    ]
    ref = build_historical_reference(decision, records)
    assert ref["source"] == "history_matched"
    assert abs(ref["response_time_ms"] - 13.366) < 0.01
    assert ref["count"] == 20000


def test_historical_reference_fallbacks_use_measured_history():
    decision = {"selected_strategy": {"execution_plan": {"plan_id": "exec_fetch_10000_df"}}}
    records = [_hist_record("r1", "baseline_single_df", 10.0),
               _hist_record("r2", "exec_fetch_10000_df", 8.0)]
    # baseline 仅 1 次（不足 3 次）→ 改用同计划实测历史
    ref = build_historical_reference(decision, records)
    assert ref["source"] == "history_same_plan"
    assert ref["response_time_ms"] == 8.0
    # 无 baseline 历史 → 同计划历史
    ref = build_historical_reference(decision, records[1:])
    assert ref["source"] == "history_same_plan"
    assert ref["response_time_ms"] == 8.0
    # 计划维度缺失（旧库/关闭计划）→ 相似历史典型表现
    ref = build_historical_reference(decision, [_hist_record("r3", "", 9.0)])
    assert ref["source"] == "history_matched" and ref["response_time_ms"] == 9.0


def test_historical_reference_requires_measured_history():
    # 先验/数据库状态估计不是实测历史 → 不得作为监控参照
    assert build_historical_reference(
        {"plan_selection": {"single_ms": 30.0, "single_source": "prior"}}, []) is None
    assert build_historical_reference({}, []) is None
    # 低于相似度阈值的历史不参与参照
    assert build_historical_reference(
        {}, [_hist_record("r1", "baseline_single_df", 10.0, similarity=0.1)]) is None


def test_historical_reference_uses_same_shape_evidence_only():
    # q10 与 q3 结构相同、测点不同（q3 参照 3.539ms vs q10 实际 4.10ms）：
    # q10 自身有 ≥3 次实测时，参照只取 q10 的实测，不混入近邻形状。
    q3_sql = ("SELECT s_1666, s_1716 FROM root.dbl.g_0.d_81, root.dbl.g_0.d_82 "
              "WHERE time >= 1640966407296 AND time <= 1640972447361 "
              "AND root.dbl.g_0.d_81.s_1666 > -5 "
              "AND root.dbl.g_0.d_82.s_1716 > -5")
    q10_sql = ("SELECT s_1667, s_1717 FROM root.dbl.g_0.d_81, root.dbl.g_0.d_82 "
               "WHERE time >= 1640966407296 AND time <= 1640972447361 "
               "AND root.dbl.g_0.d_81.s_1667 > -4 "
               "AND root.dbl.g_0.d_82.s_1717 > -4")
    q10_analysis = analyze(q10_sql)
    q10_features = compact_features(q10_analysis)
    q3_features = compact_features(analyze(q3_sql))
    large = {"data_scale_level": "large"}

    def _pair(n_q10: int) -> list:
        recs = [_hist_record(f"q3-{i}", "baseline_single_df", 3.539,
                             query_features=q3_features, database_state=large)
                for i in range(3)]
        recs += [_hist_record(f"q10-{i}", "baseline_single_df", 4.10,
                              query_features=q10_features, database_state=large)
                 for i in range(n_q10)]
        return recs

    ref = build_historical_reference({}, _pair(3), query_analysis=q10_analysis,
                                     database_state=large)
    assert ref["scope"] == "same_shape"
    assert ref["source"] == "history_baseline_records"
    assert ref["response_time_ms"] == 4.10
    assert ref["count"] == 3

    # q10 样本不足 3 次 → 退回全部相似证据（不因近邻形状存在而丢弃参照）
    ref = build_historical_reference({}, _pair(2), query_analysis=q10_analysis,
                                     database_state=large)
    assert ref["scope"] == "similar_records"
    assert ref["count"] == 5
    assert ref["response_time_ms"] < 4.10


def test_seen_query_gets_effect_assessment_from_history(tmp_path):
    # 首条查询无历史 → insufficient；同形查询有实测历史 → unchanged/degraded，
    # 不再因为“没有 baseline_metrics”而一律证据不足。
    rf = _results_file(tmp_path, [
        {"query_id": "q1", "execution_status": "success",
         "metrics": {"response_time_ms": 10.0}},
        {"query_id": "q2", "execution_status": "success",
         "metrics": {"response_time_ms": 10.2}},
        {"query_id": "q3", "execution_status": "success",
         "metrics": {"response_time_ms": 30.0}},
    ])
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path / "results"),
        executor={"type": "file", "results_file": rf},
    ))
    t1 = runner.run_query(NARROW_Q, query_id="q1")
    assert t1["monitoring_reference"] is None
    assert t1["monitoring"]["performance_assessment"] == "insufficient_evidence"

    t2 = runner.run_query(NARROW_Q, query_id="q2")
    assert t2["monitoring_reference"]["response_time_ms"] == 10.0
    # 首条执行可能已由先验选择非 baseline 计划 → 无 baseline 历史时
    # 使用同计划实测历史；两者都必须是"实测"参照，不能是成本先验。
    assert t2["monitoring_reference"]["source"] in (
        "history_baseline", "history_baseline_records", "history_same_plan")
    assert t2["monitoring"]["performance_assessment"] == "unchanged"

    t3 = runner.run_query(NARROW_Q, query_id="q3")
    assert t3["monitoring_reference"]["response_time_ms"] is not None
    assert t3["monitoring"]["performance_assessment"] == "degraded"


def test_runner_plans_are_intrinsic_to_agent(tmp_path):
    # 计划生成不是独立开关/外挂层：Runner 只传 query 与事实输入，
    # 决策 Agent 自行编译候选计划并选择，执行器消费 execution_plan。
    rf = _results_file(tmp_path, [
        {"query_id": "q1", "execution_status": "success",
         "metrics": {"response_time_ms": 10.0}, "timestamp": 1700000000000},
    ])
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path / "results"),
        executor={"type": "file", "results_file": rf},
    ))
    trace = runner.run_query(NARROW_Q, query_id="q1")
    dec = trace["decision"]
    plan = dec["decision"]["plan"]
    assert plan["strategy"]
    assert dec["selected_strategy"]["execution_plan"]["plan_id"] == plan["strategy"]
    assert f"plan={plan['strategy']}" in dec["selected_strategy"]["strategy_id"]
    row = json.loads((tmp_path / "results" / "run_log.jsonl")
                     .read_text(encoding="utf-8").splitlines()[0])
    assert row["plan_id"] == plan["strategy"]
    assert row["plan_family"] == plan["family"]


def test_runner_can_ablate_planning_via_decision_input(tmp_path):
    # 消融/对照：Agent 的自动计划生成可关闭；维度策略路径保持可用。
    from mataof.agents.optimization_decision import decide
    from mataof.agents.query_analysis import analyze
    out = decide({
        "query_id": "q1",
        "query": NARROW_Q,
        "query_analysis": analyze(NARROW_Q, query_id="q1"),
        "planning": {"enabled": False},
    })
    assert "plan" not in out["decision"]
    assert "execution_plan" not in out["selected_strategy"]


def test_runner_planning_can_be_disabled_in_config(tmp_path):
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path / "results"),
        planning={"enabled": False},
    ))
    trace = runner.run_query(NARROW_Q, query_id="q1")
    assert "plan" not in trace["decision"]["decision"]
    assert "execution_plan" not in trace["decision"]["selected_strategy"]


def test_load_device_inventory(tmp_path):
    p = tmp_path / "inventory.json"
    p.write_text(json.dumps(["root.test.d_0", "root.test.d_1"]), encoding="utf-8")
    assert load_device_inventory(None, str(p)) == ["root.test.d_0", "root.test.d_1"]
    mapping = {"root.test.*": ["root.test.d_0"]}
    assert load_device_inventory(mapping) == mapping
    assert load_device_inventory({"bad": [1]}) is None      # 格式无效 → 安全降级
    assert load_device_inventory(None, str(tmp_path / "missing.json")) is None


def test_validate_device_inventory_keeps_complete_drops_incomplete():
    inv = {"root.a.*": ["root.a.d_0", "root.a.d_1"],
           "root.b.*": ["root.b.d_0", "root.b.d_1"]}
    counts = {"root.a.*": 2, "root.b.*": 3}     # b 清单缺 1 台
    kept, notes = validate_device_inventory(inv, counts.get)
    assert kept == {"root.a.*": ["root.a.d_0", "root.a.d_1"]}
    assert any("root.b.*" in n and "2 台" in n and "3 台" in n for n in notes)


def test_validate_device_inventory_all_dropped_returns_none():
    inv = {"root.a.*": ["root.a.d_0"], "root.b.*": ["root.b.d_0"]}
    kept, notes = validate_device_inventory(inv, lambda p: 0)
    assert kept is None
    assert any("不生成设备" in n for n in notes)


def test_validate_device_inventory_count_error_drops_pattern():
    inv = {"root.a.*": ["root.a.d_0"], "root.b.*": ["root.b.d_0"]}

    def count(pattern):
        if pattern == "root.a.*":
            raise ConnectionError("socket closed")
        return 1

    kept, notes = validate_device_inventory(inv, count)
    assert kept == {"root.b.*": ["root.b.d_0"]}
    assert any("root.a.*" in n and "socket closed" in n for n in notes)


def test_validate_device_inventory_none_and_unparseable():
    # None（无结果）与不可解析（返回非整数）都剔除
    kept, notes = validate_device_inventory({"root.a.*": ["x"]}, lambda p: None)
    assert kept is None and notes
    kept, notes = validate_device_inventory({"root.a.*": ["x"]}, lambda p: "oops")
    assert kept is None and any("不是整数" in n for n in notes)


def test_validate_device_inventory_passthrough_without_checker():
    inv = {"root.a.*": ["root.a.d_0"]}
    assert validate_device_inventory(inv, None)[0] == inv
    assert validate_device_inventory(None, None)[0] is None
    # 非 dict（全局清单）无法按模式核对 → 原样返回并说明
    kept, notes = validate_device_inventory(["root.a.d_0"], lambda p: 1)
    assert kept == ["root.a.d_0"] and notes


def test_runner_overhead_timing(tmp_path):
    # D8：trace["overhead"] 含各阶段成本 + 合计 + 端到端（进入→结果）；
    # 落盘文件与 run_log 行同样携带分阶段字段
    rf = _results_file(tmp_path, [])
    results_dir = str(tmp_path / "results")
    runner = PipelineRunner(RunnerConfig(
        results_dir=results_dir, executor={"type": "file", "results_file": rf}))
    t = runner.run_query(NARROW_Q, query_id="q1")
    ov = t["overhead"]
    for key in ("analysis_ms", "retrieval_ms", "decision_ms", "execution_stage_ms",
                "monitoring_ms", "knowledge_update_ms", "pipeline_overhead_ms",
                "end_to_end_ms", "llm_backfill_ms"):
        assert key in ov, f"overhead 缺失字段: {key}"
    assert ov["end_to_end_ms"] is not None
    assert ov["llm_backfill_ms"] is None          # 非异步 LLM 模式 → None
    assert ov["pipeline_overhead_ms"] >= 0
    # 端到端（进入→结果）= 执行前开销 + 执行阶段 → 必 ≥ 执行前开销
    # （各分项分别 round 到 2 位后求和可能比总数多 0.01，给舍入容差）
    pre_exec = round(ov["analysis_ms"] + ov["retrieval_ms"] + ov["decision_ms"], 2)
    assert ov["end_to_end_ms"] + 0.02 >= pre_exec
    # 分项一致性：合计 = 各非执行阶段之和
    stage_sum = round(ov["analysis_ms"] + ov["retrieval_ms"] + ov["decision_ms"]
                      + ov["monitoring_ms"] + ov["knowledge_update_ms"], 2)
    assert ov["pipeline_overhead_ms"] == stage_sum
    # 落盘文件与 run_log 行均携带分阶段字段
    saved = json.loads(open(os.path.join(results_dir, "q1.json"),
                            encoding="utf-8").read())
    assert saved["overhead"]["end_to_end_ms"] is not None
    row = json.loads((tmp_path / "results" / "run_log.jsonl")
                     .read_text(encoding="utf-8").splitlines()[0])
    for key in ("analysis_ms", "retrieval_ms", "decision_ms", "monitoring_ms",
                "knowledge_update_ms", "pipeline_overhead_ms", "end_to_end_ms"):
        assert row[key] is not None, f"run_log 缺失字段: {key}"


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
        "planning": {"enabled": False},
    }), encoding="utf-8")
    config = load_config_file(str(cfg))
    assert config.knowledge_store == str(tmp_path / "k.json")
    assert config.results_dir == str(tmp_path / "r")
    assert config.planning == {"enabled": False}


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


def test_cli_multiple_files(tmp_path):
    rf = _results_file(tmp_path, [])
    results_dir = str(tmp_path / "results")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"results_dir": results_dir,
                               "executor": {"type": "file", "results_file": rf}}),
                   encoding="utf-8")
    f1 = tmp_path / "q1.sql"; f1.write_text(f"{NARROW_Q};", encoding="utf-8")
    f2 = tmp_path / "q2.sql"; f2.write_text(f"{MEDIUM_Q};", encoding="utf-8")
    # 模拟 shell 通配展开：--file 后跟多个文件
    code = main(["run", "--config", str(cfg), "--file", str(f1), str(f2)])
    assert code == 0
    lines = (tmp_path / "results" / "run_log.jsonl").read_text(encoding="utf-8").splitlines()
    query_lines = [l for l in lines if not json.loads(l).get("batch")]
    assert len(query_lines) == 2
    sources = {json.loads(l)["source"] for l in query_lines}
    assert sources == {"q1.sql", "q2.sql"}


def test_cli_path_like_positional_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    f = tmp_path / "q9.sql"; f.write_text(f"{NARROW_Q};", encoding="utf-8")
    # 位置参数是文件路径 → 明确报错，绝不把路径当 SQL 执行
    code = main(["run", "--config", cfg, str(f)])
    assert code == 2


def test_cli_missing_file_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    code = main(["run", "--config", cfg, "--file", str(tmp_path / "nope.sql")])
    assert code == 2


def test_cli_run_limit(tmp_path):
    f = tmp_path / "queries.sql"
    f.write_text(f"{NARROW_Q};{MEDIUM_Q};{NARROW_Q};", encoding="utf-8")
    cfg = _cfg(tmp_path, results_dir=str(tmp_path / "results"))
    assert main(["run", "--config", cfg, "--file", str(f), "--limit", "2"]) == 0
    log = (tmp_path / "results" / "run_log.jsonl").read_text(encoding="utf-8")
    query_lines = [l for l in log.strip().splitlines() if not json.loads(l).get("batch")]
    assert len(query_lines) == 2


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
    query_lines = [l for l in lines if not l.get("batch")]
    assert len(query_lines) == 2
    assert query_lines[0]["sampling"] == {"mode": "random", "limit": 2, "total": 5, "seed": 7}


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
