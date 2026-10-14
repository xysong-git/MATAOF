"""批次执行汇总报告（report.py）的单元测试。

覆盖：百分位计算（最近秩法）、样本不足如实 null、执行成功率/分布、
吞吐量、baseline 统计、空批次、格式化输出。
运行：pytest tests/test_report.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.report import batch_summary, format_batch_summary, percentile_nearest_rank  # noqa: E402


def _trace(qid, status="success", lat=None, baseline_lat=None, assessment=None,
           ts=None, overhead=None):
    t = {
        "query_id": qid,
        "execution": {
            "execution_status": status,
            "metrics": {"response_time_ms": lat} if lat is not None else {},
            "baseline_metrics": {"response_time_ms": baseline_lat}
                                if baseline_lat is not None else None,
            "timestamp": ts,
        },
        "monitoring": {
            "execution_status": status,
            "performance_assessment": assessment or "insufficient_evidence",
        },
    }
    if overhead is not None:
        t["overhead"] = overhead
    return t


def test_percentile_nearest_rank():
    vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile_nearest_rank(vals, 50) == 5
    assert percentile_nearest_rank(vals, 95) == 10
    assert percentile_nearest_rank(vals, 99) == 10
    assert percentile_nearest_rank([], 50) is None


def test_batch_summary_statistics():
    traces = [
        _trace("q1", lat=10.0, baseline_lat=50.0, assessment="improved", ts=1000),
        _trace("q2", lat=20.0, baseline_lat=48.0, assessment="improved", ts=2000),
        _trace("q3", lat=30.0, baseline_lat=52.0, assessment="unchanged", ts=3000),
        _trace("q4", lat=5.0, assessment="degraded", ts=4000),
        _trace("q5", status="failed"),
        _trace("q6", status="timeout"),
        _trace("q7", status="unknown"),
    ]
    s = batch_summary(traces, total_time_s=10.0)
    assert s["total_queries"] == 7
    assert s["total_time_s"] == 10.0
    assert s["throughput_qps"] == 0.7
    ex = s["execution"]
    assert (ex["success"], ex["failed"], ex["timeout"], ex["unknown"]) == (4, 1, 1, 1)
    assert ex["success_rate"] == round(4 / 7, 4)   # 输出保留 4 位
    l = s["latency_ms"]
    assert l["samples"] == 4
    assert l["min_ms"] == 5.0 and l["max_ms"] == 30.0
    assert l["avg_ms"] == 16.25
    # 最近秩：n=4, P50 → ceil(4*0.5)-1 = 1 → 排序后 [5,10,20,30] 第 2 个 = 10
    assert l["p50_ms"] == 10.0
    assert l["p95_ms"] == 30.0
    assert l["p99_ms"] == 30.0
    bl = s["baseline_latency_ms"]
    assert bl["samples"] == 3 and bl["avg_ms"] == 50.0
    assert s["assessment_distribution"] == {"improved": 2, "unchanged": 1,
                                            "degraded": 1, "insufficient_evidence": 3}


def test_batch_summary_small_sample_no_fabrication():
    # 仅 1 条成功样本：百分位如实 null，不编造
    s = batch_summary([_trace("q1", lat=10.0, assessment="improved")], total_time_s=1.0)
    l = s["latency_ms"]
    assert l["samples"] == 1
    assert l["p50_ms"] is None and l["p95_ms"] is None and l["p99_ms"] is None
    assert l["avg_ms"] == 10.0


def test_batch_summary_overhead_and_e2e():
    # D8：决策开销 = 各阶段成本归因（每条平均，不出分位数）；
    # 端到端 = 进入→结果（延迟口径）；净开销 = 端到端 avg − baseline 执行 avg；
    # 不按执行状态过滤（失败/超时同样有优化开销），无 overhead 节的旧 trace 跳过
    traces = [
        _trace("q1", lat=10.0, baseline_lat=8.0, overhead={
            "analysis_ms": 2.0, "retrieval_ms": 0.5, "decision_ms": 0.3,
            "execution_stage_ms": 30.0, "monitoring_ms": 0.1,
            "knowledge_update_ms": 0.1, "pipeline_overhead_ms": 3.0,
            "end_to_end_ms": 40.0, "llm_backfill_ms": None}),
        _trace("q2", lat=20.0, baseline_lat=10.0, overhead={
            "analysis_ms": 1.0, "retrieval_ms": 0.2, "decision_ms": 0.2,
            "execution_stage_ms": 20.0, "monitoring_ms": 0.1,
            "knowledge_update_ms": 0.1, "pipeline_overhead_ms": 1.6,
            "end_to_end_ms": 30.0, "llm_backfill_ms": 500.0}),
        _trace("q3", status="failed"),   # 旧格式 trace（无 overhead）→ 不计入
        _trace("q4", status="timeout", overhead={
            "analysis_ms": 3.0, "retrieval_ms": 0.5, "decision_ms": 0.5,
            "execution_stage_ms": 40.0, "monitoring_ms": 0.2,
            "knowledge_update_ms": 0.2, "pipeline_overhead_ms": 4.4,
            "end_to_end_ms": 50.0, "llm_backfill_ms": None}),
    ]
    s = batch_summary(traces, total_time_s=10.0)
    sc = s["stage_cost_ms"]
    assert sc["samples"] == 3                       # 失败/超时也有开销
    assert sc["analysis_ms"] == {"samples": 3, "avg_ms": 2.0}
    assert sc["retrieval_ms"]["avg_ms"] == 0.4
    assert sc["decision_ms"]["avg_ms"] == 0.33
    assert sc["monitoring_ms"]["samples"] == 3
    assert sc["knowledge_update_ms"]["avg_ms"] == round(0.4 / 3, 2)
    assert sc["pipeline_total_ms"] == {"samples": 3, "avg_ms": 3.0}
    assert sc["llm_backfill_ms"] == {"samples": 1, "avg_ms": 500.0}
    e2e = s["end_to_end_ms"]
    assert e2e["samples"] == 3 and e2e["avg_ms"] == 40.0
    assert e2e["min_ms"] == 30.0 and e2e["max_ms"] == 50.0
    assert s["net_overhead_ms"] == 31.0             # 40 − (8+10)/2


def test_batch_summary_empty():
    s = batch_summary([])
    assert s["total_queries"] == 0
    assert s["latency_ms"]["samples"] == 0
    assert s["stage_cost_ms"] == {"samples": 0}
    assert s["end_to_end_ms"]["samples"] == 0
    assert s["net_overhead_ms"] is None
    assert s["execution"]["success_rate"] is None


def test_batch_summary_time_from_timestamps():
    traces = [_trace("q1", lat=10.0, ts=1000), _trace("q2", lat=10.0, ts=5000)]
    s = batch_summary(traces)   # 未传 total_time_s → 用时间戳跨度 (5000-1000)/1000 = 4s
    assert s["total_time_s"] == 4.0
    assert s["throughput_qps"] == 0.5


def test_format_batch_summary():
    s = batch_summary([_trace("q1", lat=10.0, baseline_lat=50.0, assessment="improved",
                              overhead={"analysis_ms": 2.0, "retrieval_ms": 0.5,
                                        "decision_ms": 0.3, "execution_stage_ms": 30.0,
                                        "monitoring_ms": 0.1, "knowledge_update_ms": 0.1,
                                        "pipeline_overhead_ms": 3.0,
                                        "end_to_end_ms": 40.0,
                                        "llm_backfill_ms": None}),
                       _trace("q2", lat=20.0, assessment="improved")],
                      total_time_s=2.0)
    text = format_batch_summary(s)
    assert "执行汇总" in text
    assert "P50" in text and "P95" in text and "P99" in text
    assert "吞吐" in text and "成功率" in text and "baseline" in text
    assert "决策开销·各阶段" in text
    assert "分析" in text and "检索" in text and "决策" in text
    assert "端到端（进入→结果" in text
    assert "净开销" in text
