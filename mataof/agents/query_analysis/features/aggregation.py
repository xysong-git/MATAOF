"""聚合特征提取（任务 5）。

提取规则：
- 聚合函数 = sqlglot AggFunc 类，或 IoTDB 特有聚合函数名（schemas.IOTDB_AGG_FUNCTIONS）；
- 只统计主查询 SELECT 中的聚合（子查询内的聚合属于子查询，不并入）；
- GROUP BY 形式：
    time_window   → IoTDB 窗口语法 GROUP BY ([start, end), interval[, step])（预处理层提取）
    time_function → GROUP BY TIME(interval)
    path_level    → GROUP BY LEVEL = n
    columns       → 普通列分组
- 聚合涉及的数据范围（aggregation_scope）只由 SQL 中可验证的信息构成。
"""

from __future__ import annotations

from typing import Any, Optional

from sqlglot import exp

from mataof.schemas import IOTDB_AGG_FUNCTIONS
from mataof.agents.query_analysis.parser import ParseContext
from mataof.agents.query_analysis.features.base import first_column, select_blocks


def _sql_name(e: exp.Expression) -> str:
    name = getattr(e, "name", None)   # exp.Anonymous 等节点的函数名在 .name 上
    if name:
        return str(name).lower()
    try:
        return (e.sql_name() or type(e).__name__).lower()
    except Exception:
        return type(e).__name__.lower()


def is_aggregate(e: exp.Expression) -> bool:
    if isinstance(e, exp.AggFunc):
        return True
    if isinstance(e, exp.Func) and _sql_name(e) in IOTDB_AGG_FUNCTIONS:
        return True
    return False


# 聚合函数 args 中属于语法结构（非参数）的键
_AGG_STRUCTURAL_ARGS = {
    "distinct", "order", "limit", "filter", "within_group",
    "ignore_nulls", "respect_nulls", "separator", "having", "keep",
}


def _aggregate_record(e: exp.Expression, nested_in_window: bool) -> dict:
    args: list[str] = []
    for k, v in e.args.items():
        if k in _AGG_STRUCTURAL_ARGS:
            continue
        if isinstance(v, exp.Expression):
            args.append(v.sql(dialect="mysql"))
        elif isinstance(v, list):
            args.extend(x.sql(dialect="mysql") for x in v if isinstance(x, exp.Expression))
    return {
        "function": _sql_name(e),
        "args": args,
        "distinct": bool(e.args.get("distinct")),
        "nested_in_window": nested_in_window,
    }


def _collect_aggregates(block: exp.Select) -> tuple[list[dict], bool]:
    """返回 (聚合记录列表, 是否含 OVER 窗口函数)。"""
    records: list[dict] = []
    has_window_func = False
    for e in block.expressions:
        if isinstance(e, exp.Window):
            has_window_func = True
            if is_aggregate(e.this):
                records.append(_aggregate_record(e.this, nested_in_window=True))
        elif is_aggregate(e):
            records.append(_aggregate_record(e, nested_in_window=False))
    return records, has_window_func


def _dedupe(records: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for r in records:
        key = (r["function"], tuple(r["args"]), r["distinct"], r["nested_in_window"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _group_by_details(ctx: ParseContext, blocks: list[exp.Select], notes: list[str]) -> tuple[Optional[dict], bool, bool]:
    """返回 (group_by_details, has_group_by, has_time_window)。"""
    # 预处理层已提取 IoTDB 窗口 GROUP BY
    if ctx.group_by_window is not None:
        w = ctx.group_by_window
        details = {
            "type": "time_window",
            "window": {
                "start_time": w["start"],
                "end_time": w["end"],
                "interval": w["interval"],
                "interval_ms": w["interval_ms"],
                "sliding_step": w["sliding_step"],
                "sliding_step_ms": w["sliding_step_ms"],
            },
        }
        return details, True, True

    for block in blocks:
        group = block.args.get("group")
        if group is None:
            continue
        exprs = list(group.expressions or [])
        if not exprs:
            return None, True, False

        # 单表达式分支判定
        if len(exprs) == 1:
            e = exprs[0]
            if isinstance(e, exp.Time):
                interval_raw = None
                idents = list(e.find_all(exp.Identifier))
                if idents:
                    interval_raw = idents[0].this
                else:
                    lits = list(e.find_all(exp.Literal))
                    if lits:
                        interval_raw = str(lits[0].this)
                return {
                    "type": "time_function",
                    "interval": interval_raw,
                    "interval_ms": _interval_ms(interval_raw),
                }, True, True
            if isinstance(e, exp.EQ):
                left_col = first_column(e)
                right = e.args.get("expression")
                if (left_col is not None and left_col.name.lower() == "level"
                        and isinstance(right, exp.Literal)):
                    return {
                        "type": "path_level",
                        "level": right.this,
                    }, True, False

        columns = [x.sql(dialect="mysql") for x in exprs]
        return {"type": "columns", "columns": columns}, True, False

    return None, False, False


def _interval_ms(raw: Optional[str]) -> Optional[int]:
    from mataof.schemas import parse_interval_to_ms
    return parse_interval_to_ms(raw)


def extract_aggregation_features(ctx: ParseContext, device_paths: list[str],
                                 time_bounds: tuple[Optional[int], Optional[int]],
                                 has_non_time_filters: bool,
                                 notes: list[str]) -> dict:
    """返回 query_features.aggregation 节。

    device_paths / time_bounds / has_non_time_filters 来自其他提取器的结果，
    仅用于组装聚合范围描述，不引入任何新信息。
    """
    section = {
        "has_aggregation": False,
        "aggregation_functions": [],
        "has_group_by": False,
        "has_window": False,
        "group_by_details": None,
        "aggregation_scope": {},
    }

    blocks = select_blocks(ctx.ast)
    if not blocks:
        return section

    records: list[dict] = []
    over_seen = False
    for block in blocks:
        recs, has_over = _collect_aggregates(block)
        records.extend(recs)
        over_seen = over_seen or has_over

    section["aggregation_functions"] = _dedupe(records)
    section["has_aggregation"] = len(section["aggregation_functions"]) > 0

    details, has_group_by, has_time_window = _group_by_details(ctx, blocks, notes)
    section["group_by_details"] = details
    section["has_group_by"] = has_group_by
    section["has_window"] = has_time_window or over_seen

    # 聚合范围描述（仅可验证信息）
    scope: dict[str, Any] = {
        "from_paths": list(device_paths),
        "time_bounds": [time_bounds[0], time_bounds[1]],
        "window_range": None,
        "has_non_time_filters": has_non_time_filters,
    }
    if details and details["type"] == "time_window":
        w = details["window"]
        scope["window_range"] = [w["start_time"], w["end_time"]]

    parts: list[str] = []
    if section["aggregation_functions"]:
        parts.append("聚合函数：" + ", ".join(r["function"] for r in section["aggregation_functions"]))
    if device_paths:
        parts.append("设备范围：" + ", ".join(device_paths))
    if details and details["type"] == "time_window":
        w = details["window"]
        slide = f"，滑动步长 {w['sliding_step']}" if w["sliding_step"] else ""
        parts.append(f"时间窗口：[{w['start_time']}, {w['end_time']})，间隔 {w['interval']}{slide}")
    elif time_bounds[0] is not None and time_bounds[1] is not None:
        parts.append(f"时间范围：[{time_bounds[0]}, {time_bounds[1]}]（毫秒）")
    if section["has_group_by"] and not (details and details["type"] == "time_window"):
        parts.append("分组方式：" + (details or {}).get("type", "unknown"))
    scope["description"] = "；".join(parts) if parts else "无可验证的聚合范围信息"
    section["aggregation_scope"] = scope

    return section
