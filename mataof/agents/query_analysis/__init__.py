"""Query Analysis Agent：查询分析智能体。

对输入的时序数据库查询进行语义解析、结构分析和优化相关特征提取，
输出结构化查询上下文（JSON），供 Optimization Decision Agent 使用。
不做策略选择、不执行查询、不修改数据库。

用法：
    from mataof.agents.query_analysis import analyze, QueryAnalysisAgent

    result = analyze("SELECT s1 FROM root.sg1.d1 WHERE time >= 1640966400000", query_id="q1")
"""

from mataof.agents.query_analysis.agent import QueryAnalysisAgent, analyze, default_agent

__all__ = ["QueryAnalysisAgent", "analyze", "default_agent"]
