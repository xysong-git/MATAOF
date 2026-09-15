"""MATAOF 统一数据对象与 Query Analysis Agent 的输入/输出契约。

统一数据对象（Agent 间通信的基本结构，本文件给出字段说明与默认模板）：
    {
      "query_id": "",                 # 查询标识，贯穿全流程，用于追踪与实验对齐
      "query": "",                    # 原始查询文本（回显，保证可追踪）
      "query_features": {},           # Query Analysis Agent 产出的结构化查询特征
      "database_state": {},           # 数据库提供的状态/统计信息（外部输入，禁止编造）
      "system_state": {},             # 系统运行状态（外部输入）
      "historical_context": {},       # 历史经验（外部输入，禁止编造）
      "candidate_strategies": [],     # Optimization Decision Agent 的候选策略
      "selected_strategy": {},        # Optimization Decision Agent 选中的策略
      "execution_feedback": {},       # 执行反馈（运行时观测，禁止编造）
      "confidence": 0.0,              # 当前决策置信度
      "fallback": {},                 # 回退方案
      "reason": ""                    # 决策依据（简洁、可追踪）
    }

Query Analysis Agent 的输出严格遵循下述模板（query_features 部分按用户规格逐字段对应，
带 `# 扩展` 注释的字段为规格中的分析任务明确要求、但基础模板未覆盖的补充字段，
见 docs/query-analysis-agent.md 的"schema 扩展说明"一节）。

所有字段只包含两类取值：
  1. 从 SQL 文本本身可验证地提取的信息；
  2. 数据库通过 database_state 显式提供的信息。
其余一律为 null / [] / "unknown"，禁止估计与编造。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 常量：分类枚举与默认阈值
# ---------------------------------------------------------------------------

# 查询类型（任务 1）
QUERY_TYPES = (
    "point_query",              # Point Query：时间等值（单时间点）访问，无聚合
    "range_query",              # Range Query：以时间范围过滤为主，无聚合、无非时间谓词
    "predicate_filter_query",   # Predicate Filter Query：存在非时间谓词，无聚合
    "window_aggregation_query", # Window Aggregation Query：聚合 + 时间窗口分组
    "complex_composite_query",  # Complex Composite Query：多类特征组合（聚合+谓词、UNION、子查询等）
    "unknown",                  # 无法准确判断（不得强行分类）
)

# 时间范围等级（任务 2，阈值可配置）
RANGE_LEVELS = ("narrow", "medium", "wide", "unknown")
NARROW_RANGE_MS = 3_600_000      # < 1 小时 → narrow
MEDIUM_RANGE_MS = 86_400_000     # < 1 天   → medium；>= 1 天 → wide

# 过滤条件类型（任务 4）
#   time             ：时间条件（列名为 time/timestamp）
#   value            ：值条件（测点列，含完整路径引用）
#   tag_or_attribute ：标签或属性条件（SQL 无法区分两者；该列未出现在 SELECT/聚合参数中）
#   subquery         ：子查询条件（IN 子查询 / EXISTS）
#   device           ：设备路径条件（保留值，当前实现中设备条件来自 FROM 路径，见 device 节）
FILTER_TYPES = ("time", "value", "tag_or_attribute", "subquery", "device")

# 时间列识别（大小写不敏感）
TIME_COLUMN_NAMES = ("time", "timestamp")

# IoTDB 特有聚合函数名（sqlglot 无法识别为 AggFunc 的）
IOTDB_AGG_FUNCTIONS = {
    "max_value", "min_value", "first_value", "last_value", "extreme",
    "max_by", "min_by", "count_if", "time_duration", "mode",
    "stddev", "stddev_pop", "stddev_samp", "variance", "var_pop", "var_samp",
    "avg_time", "max_time", "min_time",
}

_INTERVAL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w)?\s*$", re.IGNORECASE)
_INTERVAL_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}

_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)


def classify_range_level(span_ms: Optional[int]) -> str:
    """根据时间跨度（毫秒）确定范围等级。span_ms 为 None 时返回 unknown。"""
    if span_ms is None:
        return "unknown"
    if span_ms < NARROW_RANGE_MS:
        return "narrow"
    if span_ms < MEDIUM_RANGE_MS:
        return "medium"
    return "wide"


def parse_interval_to_ms(raw: Optional[str]) -> Optional[int]:
    """将 '1h' / '20000ms' / '30m' 等间隔字符串转为毫秒；无法解析返回 None。"""
    if raw is None:
        return None
    m = _INTERVAL_RE.match(str(raw).strip())
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "ms").lower()
    return int(value * _INTERVAL_MS[unit])


def parse_time_literal_to_ms(value: Any) -> Optional[int]:
    """将时间字面量转为 epoch 毫秒。整数/浮点数视为毫秒时间戳；字符串按常见时间格式解析。

    解析失败返回 None（调用方必须标记 unknown，不得猜测）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip().strip("'\"")
        try:
            return int(datetime.fromisoformat(text.replace(" ", "T")).timestamp() * 1000)
        except ValueError:
            pass
        for fmt in _TIME_FORMATS:
            try:
                return int(datetime.strptime(text, fmt).timestamp() * 1000)
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Query Analysis Agent 输出模板
# ---------------------------------------------------------------------------

def query_analysis_output_template() -> dict:
    """返回 Query Analysis Agent 输出模板的全新深拷贝。

    字段语义与"扩展字段"说明见 docs/query-analysis-agent.md。
    """
    return {
        "query_id": "",
        "query": "",                                        # 扩展：原始查询回显（可追踪性）
        "query_type": "unknown",
        "query_features": {
            "time": {
                "has_time_filter": False,
                "start_time": None,                         # epoch 毫秒；无法确定 → null
                "end_time": None,
                "time_span": None,                          # 毫秒；点查询为 0
                "range_level": "unknown",
                "time_condition_form": None,                # 扩展：时间条件形式
                "time_filter_expressions": [],              # 扩展：时间过滤条件原文（可验证性）
            },
            "device": {
                "has_device_filter": False,
                "device_count": None,                       # 无法从 SQL 确定 → null
                "multi_device": None,                       # 无法确定 → null
                "device_paths": [],                         # 扩展：FROM 中的设备路径原文
                "device_condition_count": 0,                # 扩展：设备条件数量（FROM 路径条目数）
            },
            "filter": {
                "filter_count": 0,
                "filter_types": [],                         # 与 conditions 一一对应
                "selectivity": {},                          # 仅来自 database_state，禁止估计
                "conditions": [],                           # 扩展：各过滤条件结构化记录
                "type_counts": {},                          # 扩展：各类型条件计数
                "combination": None,                        # 扩展：条件组合关系树（AND/OR）
            },
            "aggregation": {
                "has_aggregation": False,
                "aggregation_functions": [],
                "has_group_by": False,
                "has_window": False,
                "group_by_details": None,                   # 扩展：GROUP BY 细节
                "aggregation_scope": {},                    # 扩展：聚合涉及的数据范围
            },
            "scan": {
                "estimated_scan_range": None,               # 仅来自数据库统计信息，否则 null
                "scan_level": "unknown",
                "possible_large_scan": None,                # 扩展：结构性信号，非代价估算
                "has_obvious_filter_conditions": False,     # 扩展：是否存在明显过滤条件
                "possible_large_intermediate_result": None, # 扩展：结构性信号，非代价估算
            },
        },
        "optimization_relevant_features": {
            "time_pruning": [],             # 只陈述"属于相关候选优化维度"，不选策略
            "filter_order": [],
            "aggregation_placement": [],
        },
        "unknown_features": [],             # 所有标记为 unknown 的特征及其原因
        "analysis_confidence": 0.0,
        "schema_version": "1.0",            # 扩展：输出 schema 版本（实验对齐）
    }
