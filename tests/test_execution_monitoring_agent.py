"""Execution Monitoring Agent 单元测试。

覆盖：事实采集不虚构、baseline 比较、无 baseline 不比较、无效指标 null、
效果判断各分支、异常识别、置信度上限、确定性、schema 契约、不越权（不改策略）。
运行：pytest tests/test_execution_monitoring_agent.py -v
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.execution_monitoring import monitor  # noqa: E402

REQUIRED_TOP_KEYS = {
    "query_id", "strategy_id", "execution_status", "metrics",
    "baseline_comparison", "performance_assessment", "anomalies", "feedback",
}
METRIC_KEYS = {
    "response_time_ms", "p50_latency_ms", "p95_latency_ms", "p99_latency_ms",
    "throughput", "cpu_utilization", "memory_utilization", "io_throughput",
}


def _check_schema(r: dict) -> None:
    assert set(REQUIRED_TOP_KEYS) <= set(r.keys())
    assert set(METRIC_KEYS) == set(r["metrics"].keys())
    assert set(r["feedback"].keys()) >= {"strategy_effective", "confidence"}
    assert 0.0 <= r["feedback"]["confidence"] <= 1.0
    json.dumps(r)


FULL_METRICS = {
    "response_time_ms": 12.3, "p50_latency_ms": 11.0, "p95_latency_ms": 14.0,
    "p99_latency_ms": 15.5, "throughput": 320.5, "cpu_utilization": 0.42,
    "memory_utilization": 0.35, "io_throughput": 120.0,
}
BASELINE_METRICS = {
    "response_time_ms": 20.0, "p95_latency_ms": 24.0, "p99_latency_ms": 26.0,
    "throughput": 250.0, "cpu_utilization": 0.45, "memory_utilization": 0.38,
    "io_throughput": 130.0,
}


# ---------------------------------------------------------------------------
# 事实采集与 baseline 比较
# ---------------------------------------------------------------------------

def test_success_with_baseline_improved():
    r = monitor({
        "query_id": "q1", "strategy_id": "time_pruning=partition_pruning|...",
        "execution_status": "success", "metrics": FULL_METRICS,
        "baseline_metrics": BASELINE_METRICS,
    })
    _check_schema(r)
    c = r["baseline_comparison"]
    assert c["available"] is True
    assert c["latency_change_percent"] == -38.5          # (12.3-20)/20
    assert c["throughput_change_percent"] == 28.2        # (320.5-250)/250
    assert "latency_reduction" in r["performance_change"]
    assert "throughput_improvement" in r["performance_change"]
    assert r["performance_assessment"] == "improved"
    assert r["feedback"]["strategy_effective"] is True
    assert "实测 12.3 ms vs baseline" in r["feedback"]["assessment_basis"][0]


def test_no_baseline_no_comparison():
    r = monitor({
        "query_id": "q1", "strategy_id": "s",
        "execution_status": "success", "metrics": FULL_METRICS,
    })
    _check_schema(r)
    assert r["baseline_comparison"]["available"] is False
    assert r["baseline_comparison"]["latency_change_percent"] is None
    assert r["performance_change"] == []
    # 无 baseline 且无历史 → 判断依据不足，不虚构比较
    assert r["performance_assessment"] == "insufficient_evidence"
    assert r["feedback"]["confidence"] == 0.0


def test_no_baseline_uses_historical_reference():
    r = monitor({
        "query_id": "q1", "strategy_id": "s",
        "execution_status": "success",
        "metrics": {"response_time_ms": 12.0},
        "historical_reference": {"response_time_ms": 20.0},
    })
    assert r["performance_assessment"] == "improved"
    assert "历史典型值" in r["feedback"]["assessment_basis"][0]


def test_baseline_zero_division_safe():
    r = monitor({
        "query_id": "q1", "strategy_id": "s",
        "execution_status": "success",
        "metrics": {"response_time_ms": 5.0},
        "baseline_metrics": {"response_time_ms": 0.0},
    })
    assert r["baseline_comparison"]["latency_change_percent"] is None
    assert any("为 0" in n for n in r["notes"])


def test_invalid_metrics_become_null():
    r = monitor({
        "query_id": "q1", "strategy_id": "s",
        "execution_status": "success",
        "metrics": {"response_time_ms": "fast", "cpu_utilization": -1, "throughput": True},
    })
    assert r["metrics"]["response_time_ms"] is None
    assert r["metrics"]["cpu_utilization"] is None
    assert r["metrics"]["throughput"] is None
    assert any("非数值" in n for n in r["notes"])


def test_utilization_percent_normalized():
    r = monitor({
        "query_id": "q1", "strategy_id": "s",
        "execution_status": "success",
        "metrics": {"cpu_utilization": 85, "memory_utilization": 0.35},
    })
    assert r["metrics"]["cpu_utilization"] == 0.85
    assert r["metrics"]["memory_utilization"] == 0.35


def test_missing_metrics_are_null():
    r = monitor({"query_id": "q1", "strategy_id": "s", "execution_status": "success"})
    assert all(v is None for v in r["metrics"].values())
    assert any("未提供执行指标" in n for n in r["notes"])


# ---------------------------------------------------------------------------
# 执行状态与效果判断
# ---------------------------------------------------------------------------

def test_failed_with_reason():
    r = monitor({
        "query_id": "q1", "strategy_id": "s",
        "execution_status": "failed", "failure_reason": "chunk 元数据缺失导致执行错误",
        "metrics": {},
    })
    assert r["performance_assessment"] == "failed"
    assert r["feedback"]["strategy_effective"] is False
    assert r["feedback"]["confidence"] == 0.7
    types = [a["type"] for a in r["anomalies"]]
    assert "execution_failed" in types
    assert any("chunk 元数据缺失" in a["description"] for a in r["anomalies"])


def test_failed_without_reason_recorded():
    r = monitor({"query_id": "q1", "strategy_id": "s", "execution_status": "failed"})
    assert r["performance_assessment"] == "failed"
    assert any("未提供失败原因" in n for n in r["notes"])


def test_timeout():
    r = monitor({"query_id": "q1", "strategy_id": "s", "execution_status": "timeout"})
    assert r["performance_assessment"] == "failed"
    assert any(a["type"] == "execution_timeout" for a in r["anomalies"])


def test_cancelled_insufficient_evidence():
    r = monitor({"query_id": "q1", "strategy_id": "s", "execution_status": "cancelled"})
    assert r["performance_assessment"] == "insufficient_evidence"
    assert r["feedback"]["strategy_effective"] is None
    assert any(a["type"] == "execution_cancelled" for a in r["anomalies"])


def test_unknown_status():
    r = monitor({"query_id": "q1", "strategy_id": "s"})
    assert r["execution_status"] == "unknown"
    assert r["performance_assessment"] == "insufficient_evidence"


def test_unchanged_within_threshold():
    r = monitor({
        "query_id": "q1", "strategy_id": "s", "execution_status": "success",
        "metrics": {"response_time_ms": 19.5},
        "baseline_metrics": {"response_time_ms": 20.0},
    })
    assert r["performance_assessment"] == "unchanged"   # -2.5% 在 ±5% 内
    assert r["feedback"]["strategy_effective"] is None


def test_degraded():
    r = monitor({
        "query_id": "q1", "strategy_id": "s", "execution_status": "success",
        "metrics": {"response_time_ms": 24.0},
        "baseline_metrics": {"response_time_ms": 20.0},
    })
    assert r["performance_assessment"] == "degraded"    # +20%
    assert r["feedback"]["strategy_effective"] is False


def test_p95_fallback_when_response_time_missing():
    r = monitor({
        "query_id": "q1", "strategy_id": "s", "execution_status": "success",
        "metrics": {"p95_latency_ms": 10.0},
        "baseline_metrics": {"p95_latency_ms": 20.0},
    })
    assert r["performance_assessment"] == "improved"
    assert "p95_latency_ms" in r["feedback"]["assessment_basis"][0]


def test_confidence_capped_at_08():
    r = monitor({
        "query_id": "q1", "strategy_id": "s", "execution_status": "success",
        "metrics": {"response_time_ms": 12.3, "p95_latency_ms": 14.0, "p99_latency_ms": 15.5},
        "baseline_metrics": {"response_time_ms": 20.0, "p95_latency_ms": 24.0,
                             "p99_latency_ms": 26.0},
    })
    # baseline 来源 +0.1，p95/p99 同向佐证 +0.1 → 0.8（上限）
    assert r["feedback"]["confidence"] == 0.8


# ---------------------------------------------------------------------------
# 异常识别（只记录）
# ---------------------------------------------------------------------------

def test_latency_high_anomaly():
    r = monitor({
        "query_id": "q1", "strategy_id": "s", "execution_status": "success",
        "metrics": {"response_time_ms": 15000.0},
    })
    assert any(a["type"] == "latency_high" and a["severity"] == "critical"
               for a in r["anomalies"])


def test_resource_anomalies():
    r = monitor({
        "query_id": "q1", "strategy_id": "s", "execution_status": "success",
        "metrics": {"cpu_utilization": 0.95, "memory_utilization": 0.93,
                    "response_time_ms": 100.0},
    })
    types = {a["type"] for a in r["anomalies"]}
    assert "cpu_high" in types and "memory_high" in types


def test_latency_deviation_vs_baseline():
    r = monitor({
        "query_id": "q1", "strategy_id": "s", "execution_status": "success",
        "metrics": {"response_time_ms": 100.0},
        "baseline_metrics": {"response_time_ms": 20.0},
    })
    assert any(a["type"] == "latency_deviation" for a in r["anomalies"])


def test_historical_deviation_anomaly():
    r = monitor({
        "query_id": "q1", "strategy_id": "s", "execution_status": "success",
        "metrics": {"response_time_ms": 60.0},
        "historical_reference": {"response_time_ms": 10.0},
    })
    assert any(a["type"] == "historical_deviation" for a in r["anomalies"])


def test_no_reference_no_relative_anomaly():
    # 无 baseline、无历史参照 → 不生成相对偏离类异常（不猜测"正常"水平）
    r = monitor({
        "query_id": "q1", "strategy_id": "s", "execution_status": "success",
        "metrics": {"response_time_ms": 100.0},
    })
    types = {a["type"] for a in r["anomalies"]}
    assert "latency_deviation" not in types
    assert "historical_deviation" not in types


def test_custom_thresholds():
    r = monitor({
        "query_id": "q1", "strategy_id": "s", "execution_status": "success",
        "metrics": {"cpu_utilization": 0.55, "response_time_ms": 50.0},
        "thresholds": {"cpu_threshold": 0.5, "response_time_timeout_ms": 10000.0},
    })
    assert any(a["type"] == "cpu_high" for a in r["anomalies"])


# ---------------------------------------------------------------------------
# 核心原则与契约
# ---------------------------------------------------------------------------

def test_never_fabricates():
    # 只有执行状态，没有任何指标 → 输出全部 null/unknown，绝不填数字
    r = monitor({"query_id": "q1", "strategy_id": "s", "execution_status": "success"})
    assert all(v is None for v in r["metrics"].values())
    assert r["baseline_comparison"]["available"] is False
    assert r["performance_assessment"] == "insufficient_evidence"
    assert r["feedback"]["confidence"] == 0.0


def test_no_strategy_advice():
    # 事实采集 Agent：输出不得包含策略建议（策略调整交给决策/知识 Agent）
    r = monitor({
        "query_id": "q1", "strategy_id": "s", "execution_status": "success",
        "metrics": {"response_time_ms": 24.0},
        "baseline_metrics": {"response_time_ms": 20.0},
    })
    blob = json.dumps(r, ensure_ascii=False)
    for bad in ("建议", "应使用", "应改为", "下次", "改用"):
        assert bad not in blob, f"输出出现策略建议：{bad}"


def test_single_execution_scope():
    r = monitor({"query_id": "q1", "strategy_id": "s", "execution_status": "success",
                 "metrics": {"response_time_ms": 10.0},
                 "baseline_metrics": {"response_time_ms": 20.0}})
    assert r["feedback"]["scope"] == "single_execution"
    assert r["performance_assessment"] == "improved"
    # 单次观察 ≠ 永久有效：scope 字段标记，不出现"永久/始终有效"
    assert "永久" not in json.dumps(r, ensure_ascii=False)


def test_determinism():
    inp = {
        "query_id": "q1", "strategy_id": "s", "execution_status": "success",
        "metrics": FULL_METRICS, "baseline_metrics": BASELINE_METRICS,
    }
    assert monitor(inp) == monitor(inp)
