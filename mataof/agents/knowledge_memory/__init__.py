"""Knowledge Memory Agent：知识记忆智能体。

历史查询、查询特征、优化策略、数据库状态、系统状态与实际执行结果的
结构化存储、检索、关联与更新；为 Optimization Decision Agent 提供历史决策依据。
不直接决定优化策略；不编造、不修改、不删除历史记录。

用法：
    from mataof.agents.knowledge_memory import KnowledgeMemoryAgent

    km = KnowledgeMemoryAgent(store_path="data/knowledge_store.json")   # 可持久化
    km.update({"query_analysis": ..., "decision": ..., "monitoring": ...})
    result = km.retrieve(query_analysis, database_state=..., system_state=...)
"""

from mataof.agents.knowledge_memory.agent import (
    KnowledgeMemoryAgent,
    default_agent,
    retrieve,
    update,
)

__all__ = ["KnowledgeMemoryAgent", "retrieve", "update", "default_agent"]
