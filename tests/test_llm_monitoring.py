"""Execution Monitoring Agent LLM 增强的单元测试。

覆盖：正常复述填充、**数字级防虚构校验（编造数字被拒）**、无异常时 anomaly_summary
为 null、失败兜底、禁用时事实输出不变、Runner 接入。
运行：pytest tests/test_llm_monitoring.py -v
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.execution_monitoring import monitor  # noqa: E402
from mataof.agents.execution_monitoring.llm_enhance import (  # noqa: E402
    number_consistency_check,
)
from mataof.runner import PipelineRunner, RunnerConfig  # noqa: E402

METRICS = {"response_time_ms": 12.3, "cpu_utilization": 0.42,
           "memory_utilization": 0.35}
BASELINE = {"response_time_ms": 20.0, "cpu_utilization": 0.45}


def _input(**kwargs):
    inp = {
        "query_id": "q1", "strategy_id": "s",
        "execution_status": "success",
        "metrics": METRICS, "baseline_metrics": BASELINE,
    }
    inp.update(kwargs)
    return inp


class FakeLLM:
    name = "fake"

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc

    def chat(self, messages, max_tokens=2048, timeout_s=60.0):
        if self.exc:
            raise self.exc
        return self.response


GOOD_JSON = json.dumps({
    "performance_summary": "本次执行延迟 12.3 ms，相对 baseline 降低 38.5%，CPU 利用率 0.42。",
    "anomaly_summary": None,
}, ensure_ascii=False)

FABRICATED_JSON = json.dumps({
    "performance_summary": "本次执行延迟 99.9 ms，是历史平均 3 倍。",   # 99.9 与 3 均不在给定数据中
    "anomaly_summary": "延迟飙升到 150 ms。",                          # 150 也不在
}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 数字级防虚构校验
# ---------------------------------------------------------------------------

def test_number_consistency_check():
    facts = {12.3, 20.0, -38.5, 0.42, 0.45}
    assert number_consistency_check("延迟 12.3 ms，相对 20.0 降低 38.5%", facts) is True
    assert number_consistency_check("CPU 利用率 42%", facts) is True      # ×100 单位换算
    assert number_consistency_check("延迟 99.9 ms", facts) is False
    assert number_consistency_check("是 3 倍", facts) is False
    assert number_consistency_check("纯文字描述无数字", facts) is True


# ---------------------------------------------------------------------------
# 增强与兜底
# ---------------------------------------------------------------------------

def test_llm_analysis_filled():
    r = monitor(_input(), llm=FakeLLM(response=GOOD_JSON))
    la = r["llm_analysis"]
    assert la["available"] is True
    assert "12.3" in la["performance_summary"]
    assert la["anomaly_summary"] is None   # 无异常 → null
    # 事实字段不受影响
    assert r["metrics"]["response_time_ms"] == 12.3
    assert r["performance_assessment"] == "improved"


def test_fabricated_numbers_rejected():
    r = monitor(_input(), llm=FakeLLM(response=FABRICATED_JSON))
    la = r["llm_analysis"]
    assert la["performance_summary"] is None
    assert la["anomaly_summary"] is None
    assert any("疑似编造" in n for n in la["notes"])
    # 事实输出完整不变
    assert r["baseline_comparison"]["latency_change_percent"] == -38.5


def test_anomaly_summary_for_anomalous_run():
    anomaly_json = json.dumps({
        "performance_summary": "执行失败。",
        "anomaly_summary": "执行失败与超时观测同时发生。",
    }, ensure_ascii=False)
    r = monitor(_input(execution_status="failed", failure_reason="超时",
                       metrics={}, baseline_metrics=None),
                llm=FakeLLM(response=anomaly_json))
    assert r["llm_analysis"]["anomaly_summary"] is not None
    assert "同时" in r["llm_analysis"]["anomaly_summary"]


def test_llm_failure_falls_back():
    r = monitor(_input(), llm=FakeLLM(exc=ConnectionError("refused")))
    assert r["llm_analysis"]["available"] is False
    assert any("回退" in n for n in r["llm_analysis"]["notes"])
    assert r["performance_assessment"] == "improved"


def test_llm_invalid_json_falls_back():
    r = monitor(_input(), llm=FakeLLM(response="不是JSON"))
    assert r["llm_analysis"]["available"] is False


def test_llm_disabled_matches_old_shape():
    r1 = monitor(_input())
    assert r1["llm_analysis"]["available"] is False
    core1 = {k: v for k, v in r1.items() if k != "llm_analysis"}
    r2 = monitor(_input())
    core2 = {k: v for k, v in r2.items() if k != "llm_analysis"}
    assert core1 == core2


def test_runner_wires_llm(tmp_path):
    runner = PipelineRunner(RunnerConfig(results_dir=str(tmp_path),
                                         llm={"provider": "none"}))
    assert runner.llm is None
    trace = runner.run_query(
        "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000",
        query_id="q1")
    assert trace["monitoring"]["llm_analysis"]["available"] is False
