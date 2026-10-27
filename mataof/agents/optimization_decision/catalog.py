"""有界候选策略空间（策略目录）。

设计约束：
- 策略空间是封闭的：本目录 + 输入提供的候选策略集合，二者取交集；
  目录之外的策略名一律拒绝（不得选择语义未知或数据库不支持的策略）；
- 每个策略携带：需求（requirement，判断可评估性）、风险等级（risk，用于门控）、
  基础分与上下文加分规则（scoring.py 实现）；
- 内置 baseline 是各维度的安全默认（输入可覆盖）；回退永远指向安全策略。

风险等级语义（与 scoring.py 的门控一致）：
- low   ：无额外门控，结构依据充分即可选择；
- medium：需求满足且至少有一个支撑信号（数据库上下文加分 > 0 或历史证据 ≥ 1）；
- high  ：需求满足且历史证据 ≥ 2 条（相似度达标）才可选。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 维度与策略目录
# ---------------------------------------------------------------------------

DIMENSIONS = ("alignment", "fetch_size")

# requirement 条目：{"path": ..., "op": ...}
#   path 形如 "query_features.device.multi_device" / "query_features.aggregation.has_aggregation"
#   op   ∈ {"true", "known", "gte2"}（gte2 表示数值 >= 2）
STRATEGY_CATALOG: dict[str, dict[str, dict]] = {
    "alignment": {
        "time_aligned": {
            "name": "time_aligned",
            "dimension": "alignment",
            "description": "默认时间对齐（数据库默认；结果按时间对齐为宽表）。"
                           "实测：全设备扫描比 ALIGN BY DEVICE 快 15~25×；"
                           "两设备查询差异 ~12%。",
            "risk": "low",
            "sql_expressible": False,   # 数据库默认，SQL 不变
            "requirements": [],
        },
        "align_by_device": {
            "name": "align_by_device",
            "dimension": "alignment",
            "description": "按设备对齐（结果组织为设备×时间行，行数随设备数爆炸）。"
                           "实测方向随设备数变化——需实测反馈驱动选择。",
            "risk": "low",
            "sql_expressible": True,    # SQL 追加 ALIGN BY DEVICE（结果内容等价，组织形态不同）
            "requirements": [
                {"path": "query_features.device.multi_device", "op": "true"},
            ],
        },
    },
    "fetch_size": {
        "fetch_default": {
            "name": "fetch_default",
            "dimension": "fetch_size",
            "description": "默认结果批次大小（1024，数据库默认）。",
            "risk": "low",
            "sql_expressible": False,
            "requirements": [],
        },
        "fetch_large": {
            "name": "fetch_large",
            "dimension": "fetch_size",
            "description": "大结果批次（10000，减少分页往返；实测 ~15-20% 收益）。"
                           "执行级参数（非 SQL 改写）。",
            "risk": "low",
            "sql_expressible": False,   # 参数级杠杆：parameters {"fetch_size": 10000}
            "requirements": [],
        },
    },
}

# 内置安全 baseline（输入可用 baseline_strategy 覆盖；回退永远落到此处或输入覆盖值）
BASELINE_STRATEGIES: dict[str, str] = {
    "alignment": "time_aligned",
    "fetch_size": "fetch_default",
}

# 维度不适用时的标记（非策略选择，表示该维度与当前查询无关）
NOT_APPLICABLE = "not_applicable"

# 回退原因（维度级，落入 decision[dim].strategy 的 fallback 分支说明）
FALLBACK_DUE_TO_INSUFFICIENT_CONTEXT = "insufficient_context"


def alignment_applies(qf: dict) -> bool:
    """对齐方式维度是否适用：显式多设备 或 FROM 含一级通配（潜在多设备）。"""
    d = qf.get("device") or {}
    if d.get("multi_device") is True:
        return True
    return any("*" in str(p) for p in (d.get("device_paths") or []))


def catalog_strategy(dimension: str, name: str) -> dict | None:
    """返回目录中的策略元数据；未知策略返回 None。"""
    return STRATEGY_CATALOG.get(dimension, {}).get(name)


def dimension_of_strategy(name: str) -> str | None:
    """按策略名反查维度（用于扁平候选列表解析；同名策略只允许属于一个维度）。"""
    found = [d for d in DIMENSIONS if name in STRATEGY_CATALOG.get(d, {})]
    return found[0] if len(found) == 1 else None


def check_requirement(req: dict, context: dict) -> bool:
    """评估单个需求条目。context 为归一化后的决策上下文（context.py 产物）。

    取值语义：
      unknown/null 值对 "known" 为假、对 "true"/"gte2" 为假 —— 缺失数据不满足任何需求。
    """
    value = _get_path(context, req["path"])
    op = req["op"]
    if value is None:
        return False
    if op == "true":
        return value is True
    if op == "known":
        return True
    if op == "gte2":
        return isinstance(value, (int, float)) and value >= 2
    return False


def _get_path(obj: dict, path: str):
    cur: object = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur
