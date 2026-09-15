"""Execution Monitoring Agent：执行监控智能体。

监控查询的真实执行情况，采集运行指标，建立 Query + Selected Strategy +
Actual Execution Result 的关联，生成结构化执行反馈。

事实采集 Agent：只回答"实际执行发生了什么"，不选择/修改策略，绝不虚构实验结果。

用法：
    from mataof.agents.execution_monitoring import monitor, ExecutionMonitoringAgent

    result = monitor({
        "query_id": "q1",
        "strategy_id": "time_pruning=partition_pruning|...",
        "execution_status": "success",
        "metrics": {"response_time_ms": 12.3},
        "baseline_metrics": {"response_time_ms": 20.0},   # 可选
    })
"""

from mataof.agents.execution_monitoring.agent import (
    ExecutionMonitoringAgent,
    default_agent,
    monitor,
)

__all__ = ["ExecutionMonitoringAgent", "monitor", "default_agent"]
