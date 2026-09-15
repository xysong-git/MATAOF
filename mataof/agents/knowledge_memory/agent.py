"""Knowledge Memory Agent（知识记忆智能体）。

核心职责：
    负责历史查询、查询特征、优化策略、数据库状态、系统状态以及实际执行结果的
    结构化存储、检索、关联与更新，为 Optimization Decision Agent 提供历史决策依据。
    职责不是直接决定优化策略。

严格限制（严格遵守）：
    - 禁止编造历史查询、编造执行结果；
    - 禁止修改真实历史数据（存储为 append-only，无修改/删除接口）；
    - 禁止删除与当前决策不一致的历史记录；
    - 不把单次实验结果当作绝对规律——历史样本保留，评价随累计结果更新；
    - 不直接决定当前优化策略（检索结果仅作为决策 Agent 的证据输入）。

用法：
    km = KnowledgeMemoryAgent(store_path="data/knowledge_store.json")  # 可持久化
    # 更新（Execution Monitoring Agent 反馈 → 知识入库）
    km.update({"query_analysis": ..., "decision": ..., "monitoring": ..., "timestamp": ...})
    # 检索（新查询 → 历史证据）
    result = km.retrieve(query_analysis, database_state=..., system_state=...)
    # 供决策 Agent 使用
    decide({"query_analysis": ..., "historical_records": result["matched_records_for_decision"]})
"""

from __future__ import annotations

from typing import Optional

from mataof.agents.knowledge_memory.store import MemoryStore
from mataof.agents.knowledge_memory.record import assemble_record
from mataof.agents.knowledge_memory.retrieval import retrieve as _retrieve

# 策略优先级变化判定阈值（累计成功率变化超过该值才报告方向）
PRIORITY_CHANGE_DELTA = 0.1


class KnowledgeMemoryAgent:
    """知识记忆智能体。"""

    name = "knowledge_memory"
    description = "历史查询/策略/执行结果的结构化存储、检索、关联与更新。"

    def __init__(self, store_path: Optional[str] = None):
        """store_path=None 时仅内存保存；给定路径时 JSON 持久化。"""
        self.store = MemoryStore(store_path)

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def retrieve(self, query_analysis: dict, database_state: Optional[dict] = None,
                 system_state: Optional[dict] = None, query_id: str = "") -> dict:
        """检索与当前查询上下文相关的历史知识（三层匹配 + 汇总 + 模式）。"""
        return _retrieve(self.store, query_analysis, database_state, system_state, query_id)

    # ------------------------------------------------------------------
    # 更新（反馈入库）
    # ------------------------------------------------------------------
    def update(self, feedback_input: dict) -> dict:
        """把一次完整执行（分析 + 决策 + 监控反馈）关联为历史记录并入库。

        返回规格输出（update_type / record_id / stored / strategy_effect /
        knowledge_update），notes 为扩展说明。
        """
        notes: list[str] = []
        record, rejected = assemble_record(feedback_input, notes)
        if record is None:
            return {
                "update_type": rejected or "rejected_invalid",
                "record_id": "",
                "stored": False,
                "strategy_effect": "",
                "knowledge_update": {
                    "success_record": False,
                    "failure_record": False,
                    "strategy_priority_change": None,
                },
                "notes": notes,
            }

        # 累计统计（入库前 vs 入库后）——历史样本保留，评价随累计更新
        sid = record["strategy"]["strategy_id"]
        before = self.store.statistics().get(sid, {})
        record_id = self.store.add(record)
        after = self.store.statistics().get(sid, {})
        notes.extend(self._post_store_notes(record_id))

        effect = record["execution_result"]
        failure_record = (
            effect in ("degraded", "failed")
            or record["execution"]["status"] in ("failed", "timeout")
            or any(a.get("severity") == "critical" for a in record["anomalies"])
        )
        success_record = effect == "improved"

        priority_change = None
        if sid and before and after:
            delta = after["success_ratio"] - before["success_ratio"]
            if delta >= PRIORITY_CHANGE_DELTA:
                priority_change = "increased"
            elif delta <= -PRIORITY_CHANGE_DELTA:
                priority_change = "decreased"
            if priority_change:
                notes.append(
                    f"策略 {sid} 累计成功率 {before['success_ratio']} → {after['success_ratio']}，"
                    f"优先级变化：{priority_change}"
                )

        return {
            "update_type": "new_record",
            "record_id": record_id,
            "stored": True,
            "strategy_effect": effect,
            "knowledge_update": {
                "success_record": success_record,
                "failure_record": failure_record,
                "strategy_priority_change": priority_change,
            },
            "notes": notes,
        }

    def _post_store_notes(self, record_id: str) -> list:
        return [f"历史记录已追加存储：{record_id}（append-only，不修改/删除既有记录）"]

    # ------------------------------------------------------------------
    # 全库统计
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return {
            "total_records": self.store.count(),
            "strategy_statistics": self.store.statistics(),
        }


# 默认单例（内存存储）
default_agent = KnowledgeMemoryAgent()


def retrieve(query_analysis: dict, database_state: Optional[dict] = None,
             system_state: Optional[dict] = None, query_id: str = "") -> dict:
    """Knowledge Memory Agent 检索的便捷入口。"""
    return default_agent.retrieve(query_analysis, database_state, system_state, query_id)


def update(feedback_input: dict) -> dict:
    """Knowledge Memory Agent 更新的便捷入口。"""
    return default_agent.update(feedback_input)
