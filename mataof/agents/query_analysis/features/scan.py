"""扫描相关特征提取（任务 6）。

提取规则：
- estimated_scan_range 只能来自数据库统计信息（database_state.scan_estimate），
  未提供时一律 null 并记录 unknown 原因；绝不自行估计数据量；
- scan_level 同样只在数据库提供扫描范围时输出，否则 unknown；
- possible_large_scan / possible_large_intermediate_result 是仅基于 SQL 结构的
  定性信号（不是代价估算），依据规则见下。
"""

from __future__ import annotations

from typing import Any, Optional

from mataof.schemas import classify_range_level
from mataof.agents.query_analysis.parser import ParseContext


def _get_scan_estimate(database_state: Optional[dict]) -> Optional[dict]:
    if not isinstance(database_state, dict):
        return None
    est = database_state.get("scan_estimate")
    return est if isinstance(est, dict) else None


def _valid_int(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def extract_scan_features(ctx: ParseContext, database_state: Optional[dict],
                          time_section: dict, device_section: dict,
                          aggregation_section: dict, filter_section: dict,
                          structural: dict, notes: list[str]) -> dict:
    """返回 query_features.scan 节。其余特征节与结构信号只读使用。"""
    section = {
        "estimated_scan_range": None,
        "scan_level": "unknown",
        "possible_large_scan": None,
        "has_obvious_filter_conditions": False,
        "possible_large_intermediate_result": None,
    }

    # ---- 预计扫描范围：仅来自数据库统计信息 ----
    est = _get_scan_estimate(database_state)
    if est is not None:
        tr = est.get("time_range")
        pts = est.get("estimated_points")
        valid_tr = isinstance(tr, (list, tuple)) and len(tr) == 2 and all(_valid_int(v) for v in tr)
        valid_pts = _valid_int(pts)
        section["estimated_scan_range"] = {
            "time_range": [int(tr[0]), int(tr[1])] if valid_tr else None,
            "estimated_points": int(pts) if valid_pts else None,
        }
        if valid_tr and valid_pts:
            span = int(tr[1]) - int(tr[0])
            section["scan_level"] = classify_range_level(span)
        else:
            notes.append("扫描范围等级：unknown（数据库提供的扫描估计信息不完整）")
    else:
        notes.append("预计扫描范围：unknown（未提供数据库统计信息，不自行估计）")

    # ---- 定性结构信号（仅依据 SQL 结构，非代价估算）----
    section["has_obvious_filter_conditions"] = (
        time_section["has_time_filter"] or filter_section["filter_count"] > 0
    )

    has_time = time_section["has_time_filter"]
    span = time_section["time_span"]
    range_level = time_section["range_level"]

    if not has_time:
        # 无时间约束：访问范围不受时间限制
        section["possible_large_scan"] = True
    elif range_level == "narrow" and span is not None:
        section["possible_large_scan"] = False
    elif range_level == "wide":
        section["possible_large_scan"] = True
    else:
        section["possible_large_scan"] = None  # 中等范围/未知：是否"大"取决于数据分布（无统计信息）

    multi_device = bool(device_section["multi_device"]) or (
        device_section["device_count"] is None and device_section["has_device_filter"]
    )
    agg = aggregation_section
    large_intermediate: Optional[bool] = None
    if structural["has_union"]:
        large_intermediate = True
    elif structural["has_subquery"]:
        large_intermediate = True
    elif agg["has_aggregation"] and not agg["has_group_by"] and not has_time:
        large_intermediate = True   # 全范围聚合，无分组裁剪
    elif multi_device and not has_time:
        large_intermediate = True   # 多设备且无时间约束
    elif has_time and not structural["has_union"] and not structural["has_subquery"]:
        large_intermediate = False
    section["possible_large_intermediate_result"] = large_intermediate

    return section
