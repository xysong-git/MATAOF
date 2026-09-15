"""Knowledge Memory Agent 使用示例：四 Agent 完整闭环。

流程：分析 → 决策 →（执行层采集）→ 监控 → 知识入库 → 新查询检索 → 决策复用经验。

运行：python3 examples/knowledge_example.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.query_analysis import analyze  # noqa: E402
from mataof.agents.optimization_decision import decide  # noqa: E402
from mataof.agents.execution_monitoring import monitor  # noqa: E402
from mataof.agents.knowledge_memory import KnowledgeMemoryAgent  # noqa: E402


def run_execution(km: KnowledgeMemoryAgent, query: str, query_id: str,
                  database_state: dict, metrics: dict, baseline: dict,
                  timestamp: int, history: list = None) -> dict:
    """一次完整执行闭环：分析 → 决策 → 监控 → 知识入库。"""
    analysis = analyze(query, query_id=query_id)
    decision = decide({"query_analysis": analysis, "database_state": database_state,
                       "historical_records": history or []})
    strategy_id = decision["selected_strategy"]["strategy_id"]
    # 执行层返回实测指标（真实环境采集；示例中为演示数据）
    feedback = monitor({
        "query_id": query_id, "strategy_id": strategy_id,
        "execution_status": "success",
        "metrics": metrics, "baseline_metrics": baseline,
    })
    km.update({
        "query_id": query_id, "query": query,
        "query_analysis": analysis, "decision": decision, "monitoring": feedback,
        "database_state": database_state, "timestamp": timestamp,
    })
    return {"analysis": analysis, "decision": decision, "monitoring": feedback}


def main() -> None:
    km = KnowledgeMemoryAgent(store_path=os.path.join(tempfile.mkdtemp(), "knowledge.json"))

    narrow_q = "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000"
    db = {"partition_info": {"total_partitions": 128, "covered_partitions": 2}}

    # ---- 第一批执行（积累经验：partition_pruning 表现好）----
    print("== 第一批执行（3 次，积累经验）==")
    for i in range(3):
        out = run_execution(km, narrow_q, f"q{i}", db,
                            {"response_time_ms": 10.0 + i}, {"response_time_ms": 50.0},
                            1700000000000 + i * 1000)
        print(f"  q{i}: {out['decision']['decision']['time_pruning']['strategy']} "
              f"→ {out['monitoring']['performance_assessment']}")

    # ---- 新查询（medium 范围，无分区信息）：检索历史 → 决策复用经验 ----
    print("\n== 新查询：medium 范围、无数据库状态 ==")
    medium_q = "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1644566405000"
    analysis_new = analyze(medium_q, query_id="new")

    decision_no_hist = decide({"query_analysis": analysis_new})
    print("  无历史：", decision_no_hist["decision"]["time_pruning"]["strategy"])

    km_result = km.retrieve(analysis_new, query_id="new")
    print(f"  检索：{len(km_result['matched_records'])} 条匹配（knowledge_confidence="
          f"{km_result['knowledge_confidence']}）")
    decision_with_hist = decide({
        "query_analysis": analysis_new,
        "historical_records": km_result["matched_records_for_decision"],
    })
    print("  有历史：", decision_with_hist["decision"]["time_pruning"]["strategy"])
    print("  成功模式：", json.dumps(
        km_result["historical_summary"]["successful_strategies"], ensure_ascii=False))

    # ---- 知识库统计 ----
    print("\n== 知识库累计统计 ==")
    print(json.dumps(km.stats(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
