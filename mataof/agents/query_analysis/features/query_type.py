"""查询类型识别（任务 1）。

判定规则（按优先级，全部基于可验证的 SQL 结构；无法准确判断返回 unknown）：

1. 解析失败 / 非 SELECT（如 SHOW 命令）                 → unknown
2. 含 UNION / 子查询（FROM 子查询、IN 子查询、EXISTS）/
   窗口函数（OVER）/ ALIGN BY DEVICE                     → complex_composite_query
3. 含聚合：
   a. 时间窗口分组（GROUP BY 窗口 / GROUP BY TIME()）   → window_aggregation_query
   b. 无窗口分组，但含非时间谓词                        → complex_composite_query（聚合+谓词复合）
   c. 无窗口分组，仅有时间范围过滤                      → complex_composite_query（范围访问+聚合复合）
   d. 无窗口分组，且无任何过滤                          → unknown（纯全量聚合不属于五类中可确定的类别）
4. 不含聚合：
   a. 时间等值条件（单时间点）                          → point_query
   b. 时间范围/时间点集合过滤，且无非时间谓词            → range_query
   c. 存在非时间谓词（时间过滤可有可无）                 → predicate_filter_query
   d. 无任何过滤条件                                    → unknown（无法判定访问意图）

注意：任何类型判定都不得引用 SQL 之外的信息。
"""

from __future__ import annotations

from mataof.agents.query_analysis.parser import ParseContext


def classify_query_type(ctx: ParseContext, structural: dict,
                        time_section: dict, filter_section: dict,
                        aggregation_section: dict) -> tuple[str, str | None]:
    """返回 (query_type, unknown 原因或 None)。"""
    if not ctx.is_analyzed_query:
        if ctx.parse_error:
            return "unknown", f"SQL 解析失败：{ctx.parse_error}"
        return "unknown", "非 SELECT 语句（SHOW/DDL/DML 等），不在本 Agent 分析范围内"

    if (structural["has_union"] or structural["has_subquery"]
            or structural["has_window_function"] or ctx.align_by_device):
        return "complex_composite_query", None

    agg = aggregation_section
    if agg["has_aggregation"]:
        details = agg["group_by_details"]
        if details is not None and details["type"] in ("time_window", "time_function"):
            return "window_aggregation_query", None
        non_time_count = sum(
            v for k, v in filter_section["type_counts"].items() if k != "time"
        )
        if non_time_count > 0:
            return "complex_composite_query", None
        if time_section["has_time_filter"]:
            return "complex_composite_query", None
        return "unknown", "纯全量聚合查询（无窗口分组、无时间过滤、无非时间谓词），无法在五类中准确归类"

    # 不含聚合
    non_time_count = sum(
        v for k, v in filter_section["type_counts"].items() if k != "time"
    )
    if non_time_count > 0:
        return "predicate_filter_query", None

    if time_section["has_time_filter"]:
        form = time_section["time_condition_form"]
        if form in ("equality",):
            return "point_query", None
        if form in ("in_list",):
            return "range_query", None
        if form and form.startswith(("closed_range", "half_open", "open_range", "lower_only", "upper_only", "between")):
            return "range_query", None
        # 子查询/OR 析取/解析失败等形式 → 无法准确判断
        return "unknown", f"时间过滤形式为 {form}，无法准确判断查询类型"

    return "unknown", "无时间过滤且无非时间谓词，无法从 SQL 判定访问意图（point/range/full-scan）"
