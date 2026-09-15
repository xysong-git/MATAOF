"""Optimization Decision Agent：优化决策智能体。

根据 Query Analysis Agent 的查询特征、数据库状态、系统状态与历史执行经验，
在预定义的候选策略空间中选择当前查询的优化策略。

用法：
    from mataof.agents.optimization_decision import decide, OptimizationDecisionAgent

    result = decide({
        "query_id": "q1",
        "query_analysis": analysis_output,      # Query Analysis Agent 的输出
        "database_state": {...},               # 可选
        "historical_records": [...],           # 可选
    })
"""

from mataof.agents.optimization_decision.agent import (
    OptimizationDecisionAgent,
    decide,
    default_agent,
)

__all__ = ["OptimizationDecisionAgent", "decide", "default_agent"]
