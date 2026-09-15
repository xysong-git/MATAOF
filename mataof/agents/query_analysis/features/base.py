"""特征提取公共工具：AST 遍历、条件分解、路径引用重建、时间值解析。"""

from __future__ import annotations

from typing import Any, Optional

from sqlglot import exp

from mataof.schemas import TIME_COLUMN_NAMES, parse_time_literal_to_ms


# ---------------------------------------------------------------------------
# 引用重建
# ---------------------------------------------------------------------------

def _ident_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, exp.Identifier):
        return v.this
    return str(v)


def full_ref(e: exp.Expression) -> str:
    """重建列/表的完整引用路径。

    路径列 `root.db800.g_0.d_0.s_1666` 在 sqlglot 中拆分为
    catalog=root, db=db800, table=g_0, name=s_1666，按序拼接还原。
    """
    parts = [_ident_text(e.args.get(k)) for k in ("catalog", "db", "table")] + [e.name]
    return ".".join(p for p in parts if p)


def is_time_column(c: exp.Column) -> bool:
    return bool(c.name) and c.name.lower() in TIME_COLUMN_NAMES


def first_column(expr: exp.Expression) -> Optional[exp.Column]:
    """返回表达式中（深度优先）第一个列引用；无则 None。"""
    cols = list(expr.find_all(exp.Column))
    return cols[0] if cols else None


# ---------------------------------------------------------------------------
# WHERE 条件分解：叶子条件列表 + 组合关系树
# ---------------------------------------------------------------------------

def decompose_where(where: Optional[exp.Expression]) -> tuple[list[exp.Expression], Optional[dict]]:
    """把 WHERE 表达式分解为叶子条件列表与 AND/OR 组合树。

    组合树叶子节点为 {"op": "leaf", "index": i}（i 为 conditions 中的下标）；
    内部节点为 {"op": "and"|"or", "left": ..., "right": ...}。
    无 WHERE 时返回 ([], None)。
    """
    if where is None:
        return [], None

    leaves: list[exp.Expression] = []

    def build(node: exp.Expression) -> dict:
        if isinstance(node, (exp.And, exp.Or)):
            return {
                "op": "and" if isinstance(node, exp.And) else "or",
                "left": build(node.this),
                "right": build(node.args.get("expression")),
            }
        leaves.append(node)
        return {"op": "leaf", "index": len(leaves) - 1}

    tree = build(where)
    return leaves, tree


def leaf_under_or(tree: Optional[dict], index: int) -> bool:
    """判断叶子条件 index 是否位于组合树中某个 OR 节点之下。"""
    if tree is None:
        return False
    if tree["op"] == "leaf":
        return False
    if tree["op"] == "or":
        if _contains_leaf(tree["left"], index) or _contains_leaf(tree["right"], index):
            return True
    return leaf_under_or(tree["left"], index) or leaf_under_or(tree["right"], index)


def _contains_leaf(node: dict, index: int) -> bool:
    if node["op"] == "leaf":
        return node["index"] == index
    return _contains_leaf(node["left"], index) or _contains_leaf(node["right"], index)


# ---------------------------------------------------------------------------
# 条件记录
# ---------------------------------------------------------------------------

def condition_operator(expr: exp.Expression) -> str:
    return type(expr).__name__.lower()


def condition_value(expr: exp.Expression) -> Any:
    """提取条件的值部分（可验证的原文渲染），Between/In 特殊处理。"""
    if isinstance(expr, exp.Between):
        low = expr.args.get("low")
        high = expr.args.get("high")
        return {
            "low": low.sql(dialect="mysql") if low is not None else None,
            "high": high.sql(dialect="mysql") if high is not None else None,
        }
    if isinstance(expr, exp.In):
        if isinstance(expr.args.get("query"), exp.Subquery):
            return "subquery"
        items = list(expr.expressions or [])
        return [i.sql(dialect="mysql") for i in items]
    if isinstance(expr, exp.Exists):
        return None
    right = expr.args.get("expression")
    return right.sql(dialect="mysql") if right is not None else None


def time_predicate_value(expr: exp.Expression) -> Any:
    """时间谓词的原始值：等值/比较谓词返回字面量原始值，Between 返回 {low, high}，In 返回列表。"""
    if isinstance(expr, exp.Between):
        return {
            "low": _literal_raw(expr.args.get("low")),
            "high": _literal_raw(expr.args.get("high")),
        }
    if isinstance(expr, exp.In):
        if isinstance(expr.args.get("query"), exp.Subquery):
            return "subquery"
        return [_literal_raw(i) for i in (expr.expressions or [])]
    return _literal_raw(expr.args.get("expression"))


def _literal_raw(node: Optional[exp.Expression]) -> Any:
    if node is None:
        return None
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.this
        # sqlglot 30 起数值字面量以字符串存储（is_string=False），还原为数值
        raw = node.this
        try:
            return int(raw)
        except (TypeError, ValueError):
            try:
                return float(raw)
            except (TypeError, ValueError):
                return raw
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        v = _literal_raw(node.this)
        return -v if isinstance(v, (int, float)) else node.sql(dialect="mysql")
    return node.sql(dialect="mysql")


def time_value_to_ms(value: Any) -> Optional[int]:
    """把时间谓词的原始值转为 epoch 毫秒；无法解析返回 None。"""
    if isinstance(value, dict):   # Between 的 {low, high} 不在此处处理
        return None
    if isinstance(value, list):
        return None
    return parse_time_literal_to_ms(value)


# ---------------------------------------------------------------------------
# SELECT 结构
# ---------------------------------------------------------------------------

def select_blocks(ast: Optional[exp.Expression]) -> list[exp.Select]:
    """返回顶层 SELECT 分支列表（UNION 拆分为各分支；子查询不展开）。"""
    if ast is None:
        return []
    if isinstance(ast, exp.Select):
        return [ast]
    if ast.key == "union":
        blocks: list[exp.Select] = []
        for part in (ast.args.get("this"), ast.args.get("expression")):
            blocks.extend(select_blocks(part))
        return blocks
    return []


def structural_flags(ast: Optional[exp.Expression]) -> dict:
    """查询结构信号：是否含 UNION、子查询、窗口函数（OVER）。"""
    flags = {"has_union": False, "has_subquery": False, "has_window_function": False}
    if ast is None:
        return flags
    flags["has_union"] = ast.key == "union"
    flags["has_subquery"] = len(list(ast.find_all(exp.Subquery))) > 0
    flags["has_window_function"] = len(list(ast.find_all(exp.Window))) > 0
    return flags
