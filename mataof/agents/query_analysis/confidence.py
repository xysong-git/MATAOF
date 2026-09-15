"""分析置信度计算（确定性公式，便于实验复现与消融）。

analysis_confidence 表达的是"本次结构化分析对下游决策的可信程度"，
只由可验证的分析完整性决定，与策略优劣无关。

公式（documented，结果截断到 [0, 1]，保留 2 位小数）：
    - SQL 解析失败                    → 0.1
    - 解析成功，从 1.0 起扣：
        query_type == unknown              -0.10
        有时间过滤但起止时间未知           -0.10
        有设备过滤但设备数量未知           -0.10
        存在无法区分标签/属性的条件        -0.10
        扫描范围无数据库统计信息           -0.05
        有 GROUP BY 但分组形式未识别       -0.05
"""

from __future__ import annotations

from mataof.agents.query_analysis.parser import ParseContext


def compute_confidence(ctx: ParseContext, query_type: str,
                       time_section: dict, device_section: dict,
                       filter_section: dict, scan_section: dict,
                       aggregation_section: dict) -> float:
    if ctx.parse_error:
        return 0.1

    c = 1.0
    if query_type == "unknown":
        c -= 0.10
    if time_section["has_time_filter"] and (
        time_section["start_time"] is None or time_section["end_time"] is None
    ):
        c -= 0.10
    if device_section["has_device_filter"] and device_section["device_count"] is None:
        c -= 0.10
    if filter_section["type_counts"].get("tag_or_attribute", 0) > 0:
        c -= 0.10
    if scan_section["estimated_scan_range"] is None:
        c -= 0.05
    if (aggregation_section["has_group_by"]
            and (aggregation_section["group_by_details"] is None
                 or aggregation_section["group_by_details"].get("type") == "unknown")):
        c -= 0.05

    return round(max(0.0, min(1.0, c)), 2)
