"""聚合脚本（scripts/aggregate_*.py）的单元测试（subprocess 调用，纯 stdlib）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


def _run(script: str, *args) -> str:
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_aggregate_native(tmp_path):
    f = tmp_path / "a.out"
    f.write_text("It costs 0.010s\nIt costs 0.020s\nIt costs 0.030s\nMsg: error\nEmpty set\n",
                 encoding="utf-8")
    out = _run("aggregate_native.py", "--json", str(f))
    r = json.loads(out)
    assert r["executions"] == 3
    assert r["error_lines"] == 1 and r["empty_sets"] == 1
    assert r["latency_s"]["min"] == 0.01 and r["latency_s"]["max"] == 0.03
    assert r["latency_s"]["p50"] == 0.02
    assert r["total_time_s"] == 0.06
    assert r["throughput_qps"] == 50.0
    assert r["success_rate"] == round(2 / 3, 4)


def test_aggregate_native_empty(tmp_path):
    f = tmp_path / "empty.out"
    f.write_text("", encoding="utf-8")
    r = json.loads(_run("aggregate_native.py", "--json", str(f)))
    assert r["executions"] == 0
    assert r["latency_s"]["p50"] is None
    assert r["success_rate"] is None


def test_aggregate_native_jsonl(tmp_path):
    # 新格式：run_native_client.py 输出（客户端墙钟，与 agent 同口径）
    f = tmp_path / "a.jsonl"
    rows = [
        {"query_id": "q1", "execution_status": "success", "response_time_ms": 10.0},
        {"query_id": "q2", "execution_status": "success", "response_time_ms": 20.0},
        {"query_id": "q3", "execution_status": "failed", "response_time_ms": None},
        {"batch": True, "query_id": None, "batch_total_time_s": 1.5, "batch_queries": 3},
    ]
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")
    r = json.loads(_run("aggregate_native.py", "--json", str(f)))
    assert r["executions"] == 3
    assert r["status"] == {"success": 2, "failed": 1, "timeout": 0, "unknown": 0}
    assert r["success_rate"] == round(2 / 3, 4)
    l = r["latency_ms"]
    assert l["samples"] == 2 and l["avg_ms"] == 15.0
    assert l["min_ms"] == 10.0 and l["max_ms"] == 20.0
    assert l["p50_ms"] == 10.0 and l["p95_ms"] == 20.0
    assert r["total_time_s"] == 1.5
    assert r["throughput_qps"] == 2.0
    # 格式化输出与 agent/llm 汇总同风格
    text = _run("aggregate_native.py", str(f))
    for key in ("查询数", "总用时", "吞吐", "执行: 成功", "成功率", "效果分布",
                "延迟（成功执行", "P50", "P95", "P99", "决策开销·各阶段（原生模式）",
                "端到端（原生模式"):
        assert key in text, f"native 汇总缺失字段: {key}"


def test_aggregate_agent(tmp_path):
    f = tmp_path / "run_log.jsonl"
    rows = [
        {"query_id": "q1", "execution_status": "success", "response_time_ms": 10.0,
         "performance_assessment": "improved",
         "baseline_response_time_ms": 50.0,
         "analysis_ms": 2.0, "retrieval_ms": 0.5, "decision_ms": 0.3,
         "monitoring_ms": 0.1, "knowledge_update_ms": 0.1,
         "pipeline_overhead_ms": 3.0, "end_to_end_ms": 40.0,
         "cpu_utilization": 0.1, "cpu_utilization_peak": 0.4,
         "memory_utilization": 0.2, "memory_utilization_peak": 0.5},
        {"query_id": "q2", "execution_status": "success", "response_time_ms": 20.0,
         "performance_assessment": "unchanged",
         "baseline_response_time_ms": 40.0,
         "analysis_ms": 1.0, "retrieval_ms": 0.2, "decision_ms": 0.2,
         "monitoring_ms": 0.1, "knowledge_update_ms": 0.1,
         "pipeline_overhead_ms": 1.6, "end_to_end_ms": 30.0,
         "cpu_utilization": 0.2, "cpu_utilization_peak": 0.5,
         "memory_utilization": 0.3, "memory_utilization_peak": 0.6},
        {"query_id": "q3", "execution_status": "failed", "response_time_ms": None,
         "performance_assessment": "failed"},
        {"query_id": "q4", "execution_status": "timeout", "response_time_ms": None,
         "performance_assessment": "failed"},
        {"batch": True, "query_id": None, "batch_total_time_s": 8.0, "batch_queries": 4},
    ]
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")
    r = json.loads(_run("aggregate_agent.py", "--json", str(f)))
    assert r["total_queries"] == 4
    assert r["execution"]["success"] == 2 and r["execution"]["timeout"] == 1
    assert r["execution"]["success_rate"] == 0.5
    assert r["latency_ms"]["samples"] == 2
    assert r["latency_ms"]["avg_ms"] == 15.0
    assert r["latency_ms"]["p95_ms"] == 20.0
    assert r["assessment_distribution"] == {"improved": 1, "unchanged": 1, "failed": 2}
    # 新增口径：总用时/吞吐/baseline/资源（与每批次汇总对齐）
    assert r["batches"] == 1
    assert r["total_time_s"] == 8.0
    assert r["throughput_qps"] == 0.5
    bl = r["baseline_latency_ms"]
    assert bl["samples"] == 2 and bl["avg_ms"] == 45.0
    res = r["resource_utilization"]
    assert res["cpu"]["avg"] == 0.15 and res["cpu_peak"]["max"] == 0.5
    assert res["memory_peak"]["max"] == 0.6
    # D8：分阶段成本归因 + 端到端 + 净开销（与批次汇总同口径；缺字段的行跳过）
    sc = r["stage_cost_ms"]
    assert sc["samples"] == 2
    assert sc["analysis_ms"] == {"samples": 2, "avg_ms": 1.5}
    assert sc["retrieval_ms"]["avg_ms"] == 0.35
    assert sc["pipeline_total_ms"] == {"samples": 2, "avg_ms": 2.3}
    e2e = r["end_to_end_ms"]
    assert e2e["samples"] == 2 and e2e["avg_ms"] == 35.0
    assert e2e["min_ms"] == 30.0 and e2e["max_ms"] == 40.0
    assert r["net_overhead_ms"] == -10.0          # 35 − (50+40)/2


def test_aggregate_agent_formatted_like_batch_summary(tmp_path):
    f = tmp_path / "run_log.jsonl"
    f.write_text(json.dumps({"query_id": "q1", "execution_status": "success",
                             "response_time_ms": 10.0,
                             "performance_assessment": "improved",
                             "baseline_response_time_ms": 50.0,
                             "analysis_ms": 2.0, "retrieval_ms": 0.5,
                             "decision_ms": 0.3, "monitoring_ms": 0.1,
                             "knowledge_update_ms": 0.1,
                             "pipeline_overhead_ms": 3.0, "end_to_end_ms": 40.0,
                             "cpu_utilization": 0.1, "cpu_utilization_peak": 0.1,
                             "memory_utilization": 0.2, "memory_utilization_peak": 0.2})
                 + "\n"
                 + json.dumps({"batch": True, "query_id": None,
                               "batch_total_time_s": 2.0, "batch_queries": 1})
                 + "\n", encoding="utf-8")
    text = _run("aggregate_agent.py", str(f))
    for key in ("查询数", "总用时", "吞吐", "成功率", "效果分布", "延迟（成功执行",
                "P50", "P95", "P99", "baseline 延迟", "资源",
                "决策开销·各阶段", "端到端（进入→结果", "净开销"):
        assert key in text, f"汇总缺失字段: {key}"


def test_aggregate_agent_merges_files(tmp_path):
    f1 = tmp_path / "l1.jsonl"
    f2 = tmp_path / "l2.jsonl"
    f1.write_text(json.dumps({"query_id": "a", "execution_status": "success",
                              "response_time_ms": 5.0,
                              "performance_assessment": "improved"}) + "\n"
                  + json.dumps({"batch": True, "query_id": None,
                                "batch_total_time_s": 1.0, "batch_queries": 1})
                  + "\n", encoding="utf-8")
    f2.write_text(json.dumps({"query_id": "b", "execution_status": "success",
                              "response_time_ms": 7.0,
                              "performance_assessment": "improved"}) + "\n"
                  + json.dumps({"batch": True, "query_id": None,
                                "batch_total_time_s": 3.0, "batch_queries": 1})
                  + "\n", encoding="utf-8")
    r = json.loads(_run("aggregate_agent.py", "--json", str(f1), str(f2)))
    assert r["total_queries"] == 2
    assert r["latency_ms"]["samples"] == 2
    assert r["total_time_s"] == 4.0          # 各批次用时求和
    assert r["throughput_qps"] == 0.5


def test_aggregate_agent_run_id_filter(tmp_path):
    # 同目录含两轮历史行：按 run_id 过滤只统计本轮
    f = tmp_path / "run_log.jsonl"
    rows = [
        {"run_id": "round-old", "query_id": "o1", "execution_status": "success",
         "response_time_ms": 100.0, "performance_assessment": "improved"},
        {"run_id": "round-new", "query_id": "n1", "execution_status": "success",
         "response_time_ms": 10.0, "performance_assessment": "improved"},
        {"run_id": "round-new", "query_id": "n2", "execution_status": "success",
         "response_time_ms": 20.0, "performance_assessment": "unchanged"},
        {"batch": True, "run_id": "round-old", "query_id": None,
         "batch_total_time_s": 100.0, "batch_queries": 1},
        {"batch": True, "run_id": "round-new", "query_id": None,
         "batch_total_time_s": 2.0, "batch_queries": 2},
    ]
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")
    r_all = json.loads(_run("aggregate_agent.py", "--json", str(f)))
    assert r_all["total_queries"] == 3
    r_new = json.loads(_run("aggregate_agent.py", "--json", "--run-id", "round-new", str(f)))
    assert r_new["total_queries"] == 2
    assert r_new["total_time_s"] == 2.0
    assert r_new["throughput_qps"] == 1.0
    assert r_new["latency_ms"]["avg_ms"] == 15.0   # 只含本轮样本
    # 格式化标题含轮次
    text = _run("aggregate_agent.py", "--run-id", "round-new", str(f))
    assert "轮次 round-new" in text
