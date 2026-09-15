"""时间特征提取（任务 2）。

提取规则：
- 时间过滤条件 = WHERE 中列名为 time/timestamp 的谓词；
- 起止时间只来自 SQL 字面量（epoch 毫秒整数或可解析的时间字符串），解析失败 → null；
- 多条 UNION 分支的时间范围仅当完全一致时输出，否则标记 unknown（不猜测交集/并集）；
- 时间范围等级由跨度按 schemas 阈值确定，点查询（跨度 0）为 narrow。
"""

from __future__ import annotations

from typing import Optional

from sqlglot import exp

from mataof.schemas import classify_range_level
from mataof.agents.query_analysis.parser import ParseContext
from mataof.agents.query_analysis.features.base import (
    condition_operator,
    decompose_where,
    first_column,
    is_time_column,
    leaf_under_or,
    time_predicate_value,
    time_value_to_ms,
    select_blocks,
)


def _time_leaf_indices(leaves: list[exp.Expression]) -> list[int]:
    idxs = []
    for i, leaf in enumerate(leaves):
        col = first_column(leaf)
        if col is not None and is_time_column(col):
            idxs.append(i)
    return idxs


def _block_bounds(block: exp.Select) -> Optional[dict]:
    """提取单个 SELECT 分支的时间边界。返回 None 表示该分支无时间过滤。"""
    where = block.args.get("where")
    leaves, tree = decompose_where(where.this if where else None)
    time_idx = _time_leaf_indices(leaves)
    if not time_idx:
        return None

    forms: list[str] = []
    lowers: list[int] = []   # (value_ms, inclusive) 的已解析值先收集
    uppers: list[int] = []
    lower_incl = True
    upper_incl = True
    point: Optional[int] = None
    in_list_range: Optional[tuple[int, int]] = None   # 时间点集合（IN 列表）的观测边界
    has_unresolved = False

    for i in time_idx:
        leaf = leaves[i]
        op = condition_operator(leaf)
        raw = time_predicate_value(leaf)

        if leaf_under_or(tree, i):
            # 位于 OR 分支下的时间条件：无法构成单一连续范围
            has_unresolved = True
            forms.append(f"or:{op}")
            continue

        if op == "eq" or op == "is":
            ms = time_value_to_ms(raw)
            if ms is None:
                has_unresolved = True
                forms.append("eq:unparseable")
            elif point is not None and point != ms:
                has_unresolved = True  # 多个不等的时间等值条件互相矛盾/不构成范围
                forms.append("eq:conflict")
            else:
                point = ms
                forms.append("eq")
        elif op == "between":
            if isinstance(raw, dict):
                lo, hi = time_value_to_ms(raw.get("low")), time_value_to_ms(raw.get("high"))
                if lo is None or hi is None:
                    has_unresolved = True
                    forms.append("between:unparseable")
                else:
                    lowers.append(lo)
                    uppers.append(hi)
                    forms.append("between")
            else:
                has_unresolved = True
        elif op == "in":
            if raw == "subquery":
                forms.append("in_subquery")
                has_unresolved = True
            elif isinstance(raw, list):
                values = [time_value_to_ms(v) for v in raw]
                if any(v is None for v in values):
                    has_unresolved = True
                    forms.append("in:unparseable")
                elif len(values) == 1:
                    point = values[0]
                    forms.append("eq")
                else:
                    in_list_range = (min(values), max(values))
                    forms.append("in_list")
            else:
                has_unresolved = True
        elif op in ("gte", "gt"):
            ms = time_value_to_ms(raw)
            if ms is None:
                has_unresolved = True
                forms.append(f"{op}:unparseable")
            else:
                lowers.append(ms)
                if op == "gt":
                    lower_incl = False
                forms.append(op)
        elif op in ("lte", "lt"):
            ms = time_value_to_ms(raw)
            if ms is None:
                has_unresolved = True
                forms.append(f"{op}:unparseable")
            else:
                uppers.append(ms)
                if op == "lt":
                    upper_incl = False
                forms.append(op)
        else:
            has_unresolved = True
            forms.append(op)

    # 组合判定
    if point is not None and not lowers and not uppers and in_list_range is None and not has_unresolved:
        return {
            "start": point, "end": point, "span": 0,
            "form": "equality", "level": "narrow",
        }
    if in_list_range is not None and not lowers and not uppers and point is None and not has_unresolved:
        lo, hi = in_list_range
        span = hi - lo
        return {
            "start": lo, "end": hi, "span": span,
            "form": "in_list", "level": classify_range_level(span),
        }
    if in_list_range is not None and (lowers or uppers or point is not None):
        has_unresolved = True  # 时间点集合与范围/等值条件混合：不合并猜测
        forms.append("in_list:mixed")
    if has_unresolved:
        return {
            "start": None, "end": None, "span": None,
            "form": "+".join(sorted(set(forms))) or "unknown", "level": "unknown",
        }
    if lowers and uppers:
        start, end = max(lowers), min(uppers)
        span = end - start
        if start > end:
            return {"start": None, "end": None, "span": None, "form": "conflicting_range", "level": "unknown"}
        if lower_incl and upper_incl:
            form = "closed_range"
        elif lower_incl:
            form = "half_open_right"
        elif upper_incl:
            form = "half_open_left"
        else:
            form = "open_range"
        return {"start": start, "end": end, "span": span, "form": form, "level": classify_range_level(span)}
    if lowers:
        start = max(lowers)
        return {"start": start, "end": None, "span": None, "form": "lower_only", "level": "unknown"}
    if uppers:
        end = min(uppers)
        return {"start": None, "end": end, "span": None, "form": "upper_only", "level": "unknown"}
    return {"start": None, "end": None, "span": None, "form": "+".join(sorted(set(forms))), "level": "unknown"}


def extract_time_features(ctx: ParseContext, notes: list[str]) -> dict:
    """返回 query_features.time 节。所有未确定字段为 null / unknown，并写入 notes。"""
    section = {
        "has_time_filter": False,
        "start_time": None,
        "end_time": None,
        "time_span": None,
        "range_level": "unknown",
        "time_condition_form": None,
        "time_filter_expressions": [],
    }

    blocks = select_blocks(ctx.ast)
    if not blocks:
        return section

    # 收集各分支的时间谓词原文（可验证性）
    for block in blocks:
        where = block.args.get("where")
        leaves, _ = decompose_where(where.this if where else None)
        for i in _time_leaf_indices(leaves):
            section["time_filter_expressions"].append(leaves[i].sql(dialect="mysql"))

    if not section["time_filter_expressions"]:
        return section  # 无时间过滤条件，全部保持默认（unknown）

    section["has_time_filter"] = True

    # 多分支（UNION）合并：仅当各分支结果完全一致时取该结果
    bounds_list = [b for b in (_block_bounds(block) for block in blocks) if b is not None]
    if len(bounds_list) != len(blocks):
        # 部分分支无时间过滤 → 整体时间范围不可确定
        notes.append("起止时间：unknown（部分 UNION 分支缺少时间过滤条件，无法确定整体时间范围）")
        return section

    merged = bounds_list[0]
    consistent = all(
        b.get("start") == merged.get("start") and b.get("end") == merged.get("end")
        for b in bounds_list[1:]
    )
    if not consistent:
        notes.append("起止时间：unknown（UNION 各分支时间范围不一致，不猜测整体范围）")
        return section

    section["start_time"] = merged["start"]
    section["end_time"] = merged["end"]
    section["time_span"] = merged["span"]
    section["range_level"] = merged["level"]
    section["time_condition_form"] = merged["form"]

    if merged["start"] is None or merged["end"] is None:
        notes.append(
            f"起止时间：unknown（时间过滤形式为 {merged['form']}，无法从 SQL 确定完整起止范围）"
        )
    return section
