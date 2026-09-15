"""Execution Monitoring Agent（执行监控智能体）。

核心职责：
    监控当前查询的实际执行情况，采集真实运行指标，将实际执行结果与所采用的
    优化策略建立对应关系（Query + Selected Strategy + Actual Execution Result），
    并生成结构化执行反馈。

职责边界：
    - 事实采集 Agent：输出回答"实际执行发生了什么"，不回答"下一步该用什么策略"；
    - 不重新选择优化策略、不修改策略；
    - 所有指标必须来自真实执行环境或监控接口；无法获得 → null/unknown，
      绝不估计延迟、猜测 CPU/内存、虚构吞吐量与 P95/P99；
    - 无 baseline 时不生成比较结果；单次执行观察不推断策略永久有效；
    - 异常只记录，不处理。

输入：
    {
      "query_id": "...",
      "strategy_id": "...",            # 实际执行的策略组合标识
      "execution_status": "success",   # 可选：success/timeout/failed/cancelled/unknown
      "failure_reason": "...",         # failed 时的原因（可选）
      "metrics": { ... },              # 实测指标（全部可选；无效值 → null）
      "baseline_metrics": { ... },     # 可选：系统提供的 baseline 执行结果
      "historical_reference": {...},   # 可选：历史典型表现（偏离检测/效果判断参照）
      "thresholds": { ... }            # 可选：异常阈值覆盖
    }

输出：始终为 JSON 可序列化字典（见 schemas.execution_monitoring_output_template）。
确定性纯函数：相同输入 → 相同输出。
"""

from __future__ import annotations

from typing import Optional

from mataof.schemas import execution_monitoring_output_template
from mataof.agents.execution_monitoring.normalize import (
    normalize_metrics,
    normalize_status,
    resolve_thresholds,
)
from mataof.agents.execution_monitoring.comparison import compare_with_baseline
from mataof.agents.execution_monitoring.assessment import (
    assess,
    strategy_effective_mapping,
)
from mataof.agents.execution_monitoring.anomalies import detect_anomalies


class ExecutionMonitoringAgent:
    """执行监控智能体。

    用法：
        agent = ExecutionMonitoringAgent()
        result = agent.monitor({
            "query_id": "q1",
            "strategy_id": "time_pruning=partition_pruning|...",
            "execution_status": "success",
            "metrics": {"response_time_ms": 12.3, ...},
            "baseline_metrics": {...},   # 可选
        })
    """

    name = "execution_monitoring"
    description = "采集查询的真实执行指标，关联执行策略，生成结构化执行反馈。"

    def monitor(self, monitoring_input: dict) -> dict:
        output = execution_monitoring_output_template()
        inp = monitoring_input if isinstance(monitoring_input, dict) else {}
        notes: list[str] = []

        output["query_id"] = str(inp.get("query_id") or "")
        output["strategy_id"] = str(inp.get("strategy_id") or "")

        # ---- 事实采集：状态与指标 ----
        status, failure_reason = normalize_status(
            inp.get("execution_status"), inp.get("failure_reason"), notes
        )
        output["execution_status"] = status
        output["failure_reason"] = failure_reason

        thresholds = resolve_thresholds(inp.get("thresholds"), notes)
        metrics = normalize_metrics(inp.get("metrics"), notes)
        output["metrics"] = metrics

        # ---- Baseline 比较（无 baseline 不生成比较）----
        comparison, labels = compare_with_baseline(
            metrics, inp.get("baseline_metrics"), thresholds, notes
        )
        output["baseline_comparison"] = comparison
        output["performance_change"] = labels

        # ---- 策略效果判断（基于实测，单次观察不夸大）----
        assessment, conf, basis = assess(
            status, metrics, comparison, inp.get("historical_reference"),
            thresholds, notes,
        )
        output["performance_assessment"] = assessment
        output["feedback"]["strategy_effective"] = strategy_effective_mapping(assessment)
        output["feedback"]["confidence"] = conf
        output["feedback"]["assessment_basis"] = basis

        # ---- 异常识别（只记录）----
        baseline = normalize_metrics(inp.get("baseline_metrics"), notes, prefix="baseline.")
        output["anomalies"] = detect_anomalies(
            status, failure_reason, metrics, baseline,
            inp.get("historical_reference"), thresholds,
        )

        output["notes"] = notes
        return output


# 默认单例，便于直接调用
default_agent = ExecutionMonitoringAgent()


def monitor(monitoring_input: dict) -> dict:
    """Execution Monitoring Agent 的便捷入口。"""
    return default_agent.monitor(monitoring_input)
