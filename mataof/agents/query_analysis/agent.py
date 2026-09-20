"""Query Analysis Agent（查询分析智能体）。

唯一核心职责：
    对输入的时序数据库查询进行语义解析、结构分析和优化相关特征提取，
    为后续 Optimization Decision Agent 提供结构化查询上下文。

职责边界（严格遵守）：
    - 不选择优化策略、不修改 SQL、不生成执行计划、不执行查询；
    - 不虚构数据库统计信息 / 设备数量 / 数据量 / 历史经验；
    - 无法确定的信息一律 null / [] / "unknown"，并在 unknown_features 记录原因；
    - 优化相关性只陈述"属于相关候选优化维度"，不声称任何策略更优。

输入（结构化 JSON 字典或独立参数）：
    query          ：查询文本（必填）
    query_id       ：查询标识（可选，贯穿全流程的追踪键）
    database_state ：数据库提供的状态/统计信息（可选）。当前识别两个键：
                     - statistics.selectivity   ：{列引用: 选择率(0~1)}，用于 filter.selectivity
                     - scan_estimate            ：{"time_range": [start_ms, end_ms],
                                                   "estimated_points": 点数}，用于 scan 节
                     未提供的字段一律 unknown，绝不估计。

输出：见 schemas.query_analysis_output_template()，始终为 JSON 可序列化字典。
Agent 是确定性纯函数：相同输入 → 相同输出（便于实验、消融与错误分析）。
"""

from __future__ import annotations

from typing import Optional

from mataof.schemas import query_analysis_output_template
from mataof.agents.query_analysis.llm_enhance import build_llm_analysis
from mataof.agents.query_analysis.parser import parse_query, ParseContext
from mataof.agents.query_analysis.features import (
    time as time_features,
    device as device_features,
    filter as filter_features,
    aggregation as aggregation_features,
    scan as scan_features,
    query_type as query_type_features,
)
from mataof.agents.query_analysis.features.base import structural_flags
from mataof.agents.query_analysis.relevance import build_optimization_relevance
from mataof.agents.query_analysis.confidence import compute_confidence


class QueryAnalysisAgent:
    """查询分析智能体。

    用法：
        agent = QueryAnalysisAgent()
        result = agent.analyze(query="SELECT ...", query_id="q1", database_state={...})
    """

    name = "query_analysis"
    description = "对时序数据库查询进行语义解析与优化相关特征提取，产出结构化查询上下文。"

    def analyze(self, query: str, query_id: str = "",
                database_state: Optional[dict] = None,
                llm: Optional[object] = None) -> dict:
        """分析一条查询，返回结构化 JSON（字典）。任何输入都不抛异常。

        llm：可选的 LLMClient（mataof.llm）。混合增强模式：
        - 确定性提取字段始终权威，LLM 只填充 llm_analysis 节；
        - llm=None / 调用失败 / 输出非法 → llm_analysis.available=false，
          确定性输出与不启用 LLM 时完全一致（确定性兜底）。
        """
        output = query_analysis_output_template()
        output["query_id"] = str(query_id or "")
        output["query"] = query

        notes: list[str] = []
        ctx = parse_query(query)
        if ctx.multi_statement:
            notes.append("输入包含多条语句，仅分析第一条")

        # ---- 特征提取 ----
        time_section = time_features.extract_time_features(ctx, notes)
        device_section = device_features.extract_device_features(ctx, notes)
        filter_section = filter_features.extract_filter_features(ctx, database_state, notes)
        structural = structural_flags(ctx.ast)

        non_time_count = sum(v for k, v in filter_section["type_counts"].items() if k != "time")
        aggregation_section = aggregation_features.extract_aggregation_features(
            ctx, device_section["device_paths"],
            (time_section["start_time"], time_section["end_time"]),
            has_non_time_filters=non_time_count > 0,
            notes=notes,
        )
        scan_section = scan_features.extract_scan_features(
            ctx, database_state, time_section, device_section,
            aggregation_section, filter_section, structural, notes,
        )

        # ---- 查询类型 ----
        query_type, type_note = query_type_features.classify_query_type(
            ctx, structural, time_section, filter_section, aggregation_section,
        )
        if type_note:
            notes.append(f"查询类型：unknown（{type_note}）")

        # ---- 组装 ----
        output["query_type"] = query_type
        output["query_features"] = {
            "time": time_section,
            "device": device_section,
            "filter": filter_section,
            "aggregation": aggregation_section,
            "scan": scan_section,
        }
        output["optimization_relevant_features"] = build_optimization_relevance(
            time_section, filter_section, aggregation_section,
        )
        output["unknown_features"] = notes
        output["analysis_confidence"] = compute_confidence(
            ctx, query_type, time_section, device_section,
            filter_section, scan_section, aggregation_section,
        )

        # ---- LLM 语义增强（混合增强：只进 llm_analysis 节，失败不影响确定性结果）----
        output["llm_analysis"] = build_llm_analysis(query, output, llm)
        return output


# 默认单例，便于直接调用
default_agent = QueryAnalysisAgent()


def analyze(query: str, query_id: str = "",
            database_state: Optional[dict] = None,
            llm: Optional[object] = None) -> dict:
    """Query Analysis Agent 的便捷入口。llm 为可选的 LLMClient（混合增强）。"""
    return default_agent.analyze(query=query, query_id=query_id,
                                 database_state=database_state, llm=llm)
