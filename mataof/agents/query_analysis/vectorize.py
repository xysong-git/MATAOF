"""查询特征向量化（开题报告 3.3：高维特征向量表示）。

设计契约：
- 维度固定、顺序固定（FEATURE_VECTOR_DIMS），保证跨查询可比、可喂学习模型；
- 向量只由确定性结构化特征编码——无独立事实来源，缺失/未知一律编码为 0
  （显式的 unknown 位，不编造任何数值）；
- 数值维度做有界归一化；类别维度做独热编码。

维度构成（共 FEATURE_VECTOR_DIM 维）：
  [0]  time_span_log        ：时间跨度 log10 归一化（0=未知/无时间过滤）
  [1]  device_count_norm    ：设备数归一化（0=未知）
  [2]  filter_time_count    ：时间过滤条件数（/10 封顶 1）
  [3]  filter_value_count   ：值条件数（/10 封顶 1）
  [4]  filter_tag_count     ：标签/属性条件数（/10 封顶 1）
  [5]  aggregation_count    ：聚合函数数（/10 封顶 1）
  [6]  selectivity_avg      ：数据库提供的选择率均值（0=未提供）
  [7]  scan_level_norm      ：扫描估计等级序数（unknown=0）
  [8..13]   query_type 独热（6 类，含 unknown）
  [14..17]  range_level 独热（4 类，含 unknown）
  [18]  has_time_filter     ：0/1
  [19]  has_device_filter   ：0/1
  [20]  multi_device        ：0/1（未知=0）
  [21]  has_aggregation     ：0/1
  [22]  has_group_by        ：0/1
  [23]  has_window          ：0/1
"""

from __future__ import annotations

import math
from typing import Any, Optional

QUERY_TYPES = ("point_query", "range_query", "predicate_filter_query",
               "window_aggregation_query", "complex_composite_query", "unknown")
RANGE_LEVELS = ("narrow", "medium", "wide", "unknown")

FEATURE_VECTOR_DIMS: list = (
    ["time_span_log", "device_count_norm",
     "filter_time_count", "filter_value_count", "filter_tag_count",
     "aggregation_count", "selectivity_avg", "scan_level_norm"]
    + [f"query_type={t}" for t in QUERY_TYPES]
    + [f"range_level={r}" for r in RANGE_LEVELS]
    + ["has_time_filter", "has_device_filter", "multi_device",
       "has_aggregation", "has_group_by", "has_window"]
)
FEATURE_VECTOR_DIM: int = len(FEATURE_VECTOR_DIMS)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def encode_features(analysis_output: dict) -> dict:
    """把分析输出编码为固定维度特征向量。

    返回 {"dims": FEATURE_VECTOR_DIMS 副本, "values": [...]}；
    所有取值只来自确定性特征，未知 → 0。
    """
    qf = analysis_output.get("query_features") or {}
    t = qf.get("time") or {}
    d = qf.get("device") or {}
    fl = qf.get("filter") or {}
    agg = qf.get("aggregation") or {}
    scan = qf.get("scan") or {}

    # ---- 数值维度 ----
    span = t.get("time_span")
    time_span_log = _clamp(math.log10(span + 1) / 13.0) \
        if isinstance(span, (int, float)) and not isinstance(span, bool) and span is not None \
        else 0.0

    dc = d.get("device_count")
    device_count_norm = _clamp(float(dc) / 1000.0) if isinstance(dc, (int, float)) and not isinstance(dc, bool) else 0.0

    counts = fl.get("type_counts") or {}
    filter_time_count = _clamp(float(counts.get("time", 0)) / 10.0)
    filter_value_count = _clamp(float(counts.get("value", 0)) / 10.0)
    filter_tag_count = _clamp(float(sum(v for k, v in counts.items()
                                          if k not in ("time", "value"))) / 10.0)

    agg_funcs = agg.get("aggregation_functions") or []
    aggregation_count = _clamp(float(len(agg_funcs)) / 10.0)

    sel = fl.get("selectivity") or {}
    sel_vals = [v for v in sel.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    selectivity_avg = _clamp(sum(sel_vals) / len(sel_vals)) if sel_vals else 0.0

    scan_level_ord = {"unknown": 0.0, "narrow": 0.25, "medium": 0.5, "wide": 1.0}
    scan_level_norm = scan_level_ord.get(scan.get("scan_level") or "unknown", 0.0)

    # ---- 独热维度 ----
    query_type = analysis_output.get("query_type") or "unknown"
    query_type_onehot = [1.0 if query_type == x else 0.0 for x in QUERY_TYPES]
    range_level = t.get("range_level") or "unknown"
    range_level_onehot = [1.0 if range_level == x else 0.0 for x in RANGE_LEVELS]

    flags = [
        1.0 if t.get("has_time_filter") else 0.0,
        1.0 if d.get("has_device_filter") else 0.0,
        1.0 if d.get("multi_device") is True else 0.0,
        1.0 if agg.get("has_aggregation") else 0.0,
        1.0 if agg.get("has_group_by") else 0.0,
        1.0 if agg.get("has_window") else 0.0,
    ]

    values = ([round(time_span_log, 6), round(device_count_norm, 6),
               round(filter_time_count, 6), round(filter_value_count, 6),
               round(filter_tag_count, 6), round(aggregation_count, 6),
               round(selectivity_avg, 6), round(scan_level_norm, 6)]
              + query_type_onehot + range_level_onehot
              + [round(x, 6) for x in flags])

    assert len(values) == FEATURE_VECTOR_DIM, "向量维度契约被破坏"
    return {"dims": list(FEATURE_VECTOR_DIMS), "values": values}
