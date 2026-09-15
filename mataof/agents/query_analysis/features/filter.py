"""过滤条件特征提取（任务 4）。

提取规则：
- 遍历 WHERE 的叶子条件，逐条记录表达式原文、类型、列引用、操作符与值；
- 条件类型判定：
    time             → 列名为 time/timestamp；
    subquery         → IN 子查询 / EXISTS；
    value            → 完整路径测点列，或裸列名出现在 SELECT/聚合参数中；
    tag_or_attribute → 裸列名未出现在 SELECT/聚合参数中（SQL 无法区分标签与属性）；
- 选择率只从 database_state 中拷贝（键为列引用），绝不自行估计；
  未提供选择率时 selectivity 为空并在 unknown_features 记录原因；
- 组合关系树（AND/OR）由分解树给出，叶子指向 conditions 下标。
"""

from __future__ import annotations

from typing import Any, Optional

from sqlglot import exp

from mataof.agents.query_analysis.parser import ParseContext
from mataof.agents.query_analysis.features.base import (
    condition_operator,
    condition_value,
    decompose_where,
    is_time_column,
    reference_of_leaf,
    select_blocks,
)


def collect_select_column_names(blocks: list[exp.Select]) -> set[str]:
    """收集 SELECT 表达式中引用的裸列名（用于 value/tag_or_attribute 判定）。

    注意：sqlglot 的 find_all 不包含节点自身，裸列 SELECT 需单独处理。
    """
    names: set[str] = set()
    for block in blocks:
        for e in block.expressions:
            if isinstance(e, exp.Column) and not is_time_column(e) and not e.table and e.name:
                names.add(e.name)
                continue
            for c in e.find_all(exp.Column):
                if not c.table and not is_time_column(c) and c.name:
                    names.add(c.name)
    return names


def _classify(leaf: exp.Expression, select_names: set[str]) -> str:
    if isinstance(leaf, exp.Exists):
        return "subquery"
    if isinstance(leaf, exp.In) and isinstance(leaf.args.get("query"), exp.Subquery):
        return "subquery"
    ref = reference_of_leaf(leaf)
    if ref is None:
        return "tag_or_attribute"  # 无法识别列 → 保守标记，记录原因由调用方处理
    if ref.lower() in {"time", "timestamp"}:
        return "time"
    if "." in ref:
        return "value"          # 完整路径引用（root.sg.d.s）→ 测点值条件
    if ref in select_names:
        return "value"          # 出现在 SELECT/聚合参数中的裸列名 → 测点值条件
    return "tag_or_attribute"   # SQL 无法区分标签/属性


def _record_condition(leaf: exp.Expression, ctype: str, notes: list[str]) -> dict:
    record = {
        "expression": leaf.sql(dialect="mysql"),
        "type": ctype,
        "column": None,
        "operator": condition_operator(leaf),
        "value": condition_value(leaf),
    }
    ref = reference_of_leaf(leaf)
    if ref is not None:
        record["column"] = ref
    else:
        notes.append(f"过滤条件无法识别列引用：{record['expression']}（标记为 tag_or_attribute，原因：无法确定列）")
    return record


def _get_selectivity_mapping(database_state: Optional[dict]) -> Optional[dict]:
    """从 database_state 读取选择率映射。未提供返回 None（绝不估计）。"""
    if not isinstance(database_state, dict):
        return None
    stats = database_state.get("statistics", database_state)
    if not isinstance(stats, dict):
        return None
    sel = stats.get("selectivity")
    return sel if isinstance(sel, dict) else None


def _valid_selectivity(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 1.0


def extract_filter_features(ctx: ParseContext, database_state: Optional[dict], notes: list[str]) -> dict:
    """返回 query_features.filter 节。"""
    section = {
        "filter_count": 0,
        "filter_types": [],
        "selectivity": {},
        "conditions": [],
        "type_counts": {},
        "combination": None,
    }

    blocks = select_blocks(ctx.ast)
    if not blocks:
        return section

    select_names = collect_select_column_names(blocks)

    # 各分支条件与组合树
    branch_conditions: list[list[dict]] = []
    branch_trees: list[Optional[dict]] = []
    for block in blocks:
        where = block.args.get("where")
        leaves, tree = decompose_where(where.this if where else None)
        recs = [_record_condition(l, _classify(l, select_names), notes) for l in leaves]
        branch_conditions.append(recs)
        branch_trees.append(tree)

    # 标签/属性歧义的可追踪说明（无法仅凭 SQL 区分）
    for rec in branch_conditions[0]:
        if rec["type"] == "tag_or_attribute" and rec["column"] is not None:
            notes.append(
                f"标签/属性条件无法仅凭 SQL 区分：{rec['column']} "
                f"（未出现在 SELECT/聚合参数中，标记为 tag_or_attribute）"
            )

    # 单分支：直接使用；多分支（UNION）：按分支分别记录组合树（叶子下标偏移到全局列表）
    section["conditions"] = branch_conditions[0]
    section["combination"] = branch_trees[0]
    if len(branch_conditions) > 1:
        def _remap(tree: Optional[dict], offset: int) -> Optional[dict]:
            if tree is None:
                return None
            if tree["op"] == "leaf":
                return {"op": "leaf", "index": tree["index"] + offset}
            return {
                "op": tree["op"],
                "left": _remap(tree["left"], offset),
                "right": _remap(tree["right"], offset),
            }

        remapped_trees: list[Optional[dict]] = [branch_trees[0]]
        offset = len(branch_conditions[0])
        for i, recs in enumerate(branch_conditions[1:], start=1):
            remapped_trees.append(_remap(branch_trees[i], offset))
            offset += len(recs)
        section["combination"] = {"op": "union_branches", "branches": remapped_trees}
        for recs in branch_conditions[1:]:
            section["conditions"].extend(recs)

    section["filter_count"] = len(section["conditions"])
    section["filter_types"] = [c["type"] for c in section["conditions"]]
    for t in section["filter_types"]:
        section["type_counts"][t] = section["type_counts"].get(t, 0) + 1

    # 选择率：仅拷贝数据库提供的数据
    sel_map = _get_selectivity_mapping(database_state)
    if sel_map:
        for c in section["conditions"]:
            key = c["column"]
            if key is None:
                continue
            if key in sel_map and _valid_selectivity(sel_map[key]):
                section["selectivity"][key] = float(sel_map[key])
        if not section["selectivity"]:
            notes.append("选择率：数据库未提供与过滤条件匹配的选择率数据，不做估计")
    else:
        notes.append("选择率：数据库未提供选择率数据，不做估计")

    return section
