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

DIMENSIONS = ("time_pruning", "filter_order", "aggregation_placement")

# requirement 条目：{"path": ..., "op": ...}
#   path 形如 "query_features.time.has_time_filter" / "database_state.physical_organization.chunk_level"
#   op   ∈ {"true", "known", "gte2"}（gte2 表示数值 >= 2）
STRATEGY_CATALOG: dict[str, dict[str, dict]] = {
    "time_pruning": {
        "full_scan": {
            "name": "full_scan",
            "dimension": "time_pruning",
            "description": "不做时间裁剪优化，按查询范围完整扫描（安全默认，无额外要求）。",
            "risk": "low",
            "sql_expressible": False,
            "requirements": [],
        },
        "partition_pruning": {
            "name": "partition_pruning",
            "dimension": "time_pruning",
            "description": "依据查询时间边界跳过不相关分区。需求：时间过滤边界已知；"
                           "分区元数据可进一步支撑裁剪覆盖度评估。",
            "risk": "medium",
            "sql_expressible": False,   # 裁剪是执行层行为，SQL 文本不变（时间边界已在 WHERE 中）
            "requirements": [
                {"path": "query_features.time.has_time_filter", "op": "true"},
                {"path": "query_features.time.start_time", "op": "known"},
                {"path": "query_features.time.end_time", "op": "known"},
            ],
        },
        "chunk_level_filtering": {
            "name": "chunk_level_filtering",
            "dimension": "time_pruning",
            "description": "下沉到 chunk 级别的时间过滤。需求：时间边界已知 + 数据库提供 "
                           "chunk 级物理组织信息。高风险：依赖执行层能力与元数据完整性。",
            "risk": "high",
            "sql_expressible": False,   # chunk 级过滤为执行层能力，无对应 SQL 语法
            "requirements": [
                {"path": "query_features.time.has_time_filter", "op": "true"},
                {"path": "query_features.time.start_time", "op": "known"},
                {"path": "query_features.time.end_time", "op": "known"},
                {"path": "database_state.physical_organization.chunk_level", "op": "known"},
            ],
        },
    },
    "filter_order": {
        "time_first": {
            "name": "time_first",
            "dimension": "filter_order",
            "description": "优先评估时间过滤（时序库按时间维度物理组织，时间过滤可跳过"
                           "不相关时间块；无需选择率数据即可成立的结构性依据）。",
            "risk": "low",
            "sql_expressible": True,    # WHERE 谓词重排即可表达为等价 SQL
            "requirements": [
                {"path": "query_features.filter.non_time_filter_count", "op": "gte2"},
            ],
        },
        "device_tag_first": {
            "name": "device_tag_first",
            "dimension": "filter_order",
            "description": "优先评估设备/标签过滤。需求：存在多个非时间条件；"
                           "只有获得选择率或设备规模等数据支撑时才具备优势依据"
                           "（不得在缺乏数据时假设其选择性更高）。",
            "risk": "medium",
            "sql_expressible": True,    # WHERE 谓词重排即可表达为等价 SQL
            "requirements": [
                {"path": "query_features.filter.non_time_filter_count", "op": "gte2"},
            ],
        },
    },
    "aggregation_placement": {
        "final_level_aggregation": {
            "name": "final_level_aggregation",
            "dimension": "aggregation_placement",
            "description": "聚合在最终层执行（数据库默认行为，最小侵入，安全默认）。",
            "risk": "low",
            "sql_expressible": False,
            "requirements": [
                {"path": "query_features.aggregation.has_aggregation", "op": "true"},
            ],
        },
        "intermediate_level_aggregation": {
            "name": "intermediate_level_aggregation",
            "dimension": "aggregation_placement",
            "description": "聚合在中间层执行（分组后、最终归并前）。有 GROUP BY 或"
                           "中间结果规模较大时具有结构性依据。",
            "risk": "medium",
            "sql_expressible": False,   # 聚合位置为执行层概念，标准 SQL 无对应语法
            "requirements": [
                {"path": "query_features.aggregation.has_aggregation", "op": "true"},
            ],
        },
        "scan_level_aggregation": {
            "name": "scan_level_aggregation",
            "dimension": "aggregation_placement",
            "description": "聚合下推到扫描层。需求：外部系统将本策略列入候选（即声明执行层"
                           "支持）；并需要扫描量小/时间范围窄/窗口聚合等支撑信号之一。",
            "risk": "medium",
            "sql_expressible": False,   # 聚合下推为执行层能力，无对应 SQL 语法
            "requirements": [
                {"path": "query_features.aggregation.has_aggregation", "op": "true"},
            ],
        },
    },
}

# 内置安全 baseline（输入可用 baseline_strategy 覆盖；回退永远落到此处或输入覆盖值）
BASELINE_STRATEGIES: dict[str, str] = {
    "time_pruning": "full_scan",
    "filter_order": "time_first",
    "aggregation_placement": "final_level_aggregation",
}

# 维度不适用时的标记（非策略选择，表示该维度与当前查询无关）
NOT_APPLICABLE = "not_applicable"

# 回退原因（维度级，落入 decision[dim].strategy 的 fallback 分支说明）
FALLBACK_DUE_TO_INSUFFICIENT_CONTEXT = "insufficient_context"


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
