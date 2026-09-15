"""优化维度相关性映射。

只做一件事：把已确认的查询特征映射为"某个优化维度属于相关候选"的陈述。
严格界限：
- 只陈述"相关性 / 候选维度"，不选择策略、不声称某策略更优；
- 相关性陈述只基于已确认的特征；特征为 unknown 时不产生对应陈述；
- 每条陈述都可追溯到具体特征字段（用于实验与错误分析）。
"""

from __future__ import annotations

from typing import Optional


def _time_pruning_statements(time_section: dict, group_by_details: Optional[dict]) -> list[str]:
    stmts: list[str] = []
    if not time_section["has_time_filter"] and group_by_details is None:
        return stmts

    if time_section["has_time_filter"]:
        form = time_section["time_condition_form"]
        start, end, span = (time_section["start_time"], time_section["end_time"],
                            time_section["time_span"])
        if start is not None and end is not None:
            stmts.append(
                f"查询包含明确时间过滤条件（形式 {form}，时间跨度 {span} ms，"
                f"范围等级 {time_section['range_level']}），"
                f"Time Pruning 属于相关候选优化维度。"
            )
        else:
            stmts.append(
                f"查询包含时间过滤条件（形式 {form}），但起止时间无法从 SQL 确定；"
                f"Time Pruning 仍属于相关候选优化维度，裁剪范围取决于可获得的时间信息。"
            )

    if group_by_details is not None and group_by_details["type"] == "time_window":
        w = group_by_details["window"]
        stmts.append(
            f"GROUP BY 时间窗口范围明确（[{w['start_time']}, {w['end_time']})），"
            f"窗口边界可作为时间裁剪依据，Time Pruning 属于相关候选优化维度。"
        )
    return stmts


def _filter_order_statements(filter_section: dict) -> list[str]:
    stmts: list[str] = []
    non_time = {k: v for k, v in filter_section["type_counts"].items() if k != "time"}
    n = sum(non_time.values())
    if n >= 2:
        dist = "、".join(f"{k}×{v}" for k, v in non_time.items())
        stmts.append(
            f"存在 {n} 个非时间过滤条件（类型分布：{dist}），"
            f"Filter Order 属于相关候选优化维度。"
        )
    if filter_section["selectivity"]:
        k = len(filter_section["selectivity"])
        total = filter_section["filter_count"]
        stmts.append(
            f"数据库为 {k}/{total} 个过滤条件提供了选择率数据，"
            f"可作为 Filter Order 候选评估的依据；其余条件的选择率未知，不做估计。"
        )
    return stmts


def _aggregation_placement_statements(aggregation_section: dict) -> list[str]:
    stmts: list[str] = []
    if not aggregation_section["has_aggregation"]:
        return stmts

    names = sorted({r["function"] for r in aggregation_section["aggregation_functions"]})
    stmts.append(
        f"查询包含聚合（{', '.join(names)}），"
        f"Aggregation Placement 属于相关候选优化维度。"
    )
    details = aggregation_section["group_by_details"]
    if details is not None and details["type"] == "time_window":
        w = details["window"]
        slide = f"，滑动步长 {w['sliding_step']}" if w.get("sliding_step") else ""
        stmts.append(
            f"聚合按时间窗口分组（间隔 {w['interval']}{slide}），"
            f"属于 Aggregation Placement 的窗口聚合候选场景。"
        )
    elif details is not None and details["type"] == "time_function":
        stmts.append(
            f"聚合按时间函数分组（GROUP BY TIME({details['interval']})），"
            f"属于 Aggregation Placement 的窗口聚合候选场景。"
        )
    elif not aggregation_section["has_group_by"]:
        stmts.append(
            "聚合未使用 GROUP BY，在其访问范围内整体执行，"
            "属于 Aggregation Placement 候选场景。"
        )
    return stmts


def build_optimization_relevance(time_section: dict, filter_section: dict,
                                 aggregation_section: dict) -> dict:
    """生成 optimization_relevant_features 节（三个维度各自的陈述列表）。"""
    return {
        "time_pruning": _time_pruning_statements(time_section, aggregation_section["group_by_details"]),
        "filter_order": _filter_order_statements(filter_section),
        "aggregation_placement": _aggregation_placement_statements(aggregation_section),
    }
