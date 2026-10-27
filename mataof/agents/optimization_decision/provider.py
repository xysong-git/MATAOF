"""候选提供方（策略空间重设计）：按查询为目录内策略生成 equivalent_sql / 执行参数。

原则不变：决策 Agent 只选择、不生成 SQL。本模块是"候选提供方（外部系统）"的
内置实现——为真实可送达执行层的杠杆生成改写/参数（全部有实测依据，见
scripts/bench_sql_levers.py 与 catalog.py 注释）：
- alignment/align_by_device  ：SQL 追加 ALIGN BY DEVICE（结果内容等价、组织形态不同；
  实测方向随设备数变化——宽设备扫描默认对齐快 15~25×，少量设备按设备对齐略优）；
- fetch_size/fetch_large      ：执行级参数 fetch_size=10000（实测 ~15-20% 收益，
  非 SQL 改写——执行层按参数选择会话批次）。

生成失败/不适用 → 该候选不带 SQL/参数（执行层回退原查询，安全默认）。
"""

from __future__ import annotations

from typing import Any, Optional

from mataof.agents.optimization_decision.catalog import (
    DIMENSIONS,
    STRATEGY_CATALOG,
    alignment_applies,
)

FETCH_LARGE = 10000


def _append_align_by_device(query: str, analysis: Optional[dict]) -> Optional[str]:
    """在查询尾部追加 ALIGN BY DEVICE（引擎约束校验后）。

    本 IoTDB 构建的约束（实测）：ALIGN BY DEVICE 只接受测点级或**一级通配**
    路径（如 root.db.g_0.*）——显式设备路径列表（root.db.g_0.d_0, ...）
    会被引擎拒绝（701）。因此只有 FROM 含通配路径的查询才可改写；
    其余查询不提供该候选（避免执行失败与无效声明）。
    """
    text = query.strip().rstrip(";").strip()
    if not text or "ALIGN BY DEVICE" in text.upper():
        return None
    qf = (analysis or {}).get("query_features") or {}
    paths = (qf.get("device") or {}).get("device_paths") or []
    if not any("*" in str(p) for p in paths):
        return None   # 无通配路径 → 引擎不接受该子句 → 不提供改写
    return f"{text} ALIGN BY DEVICE"


def build_candidates(query: str, analysis: Optional[dict],
                     configured: Any = None) -> dict:
    """按查询生成每维度候选（富条目契约：{"strategy", "equivalent_sql", "parameters"}）。

    configured：外部提供的 candidate_strategies（非空则原样返回——外部候选优先）。
    """
    if configured:
        return configured
    qf = (analysis or {}).get("query_features") or {}

    candidates: dict = {}
    for dim in DIMENSIONS:
        out = []
        for name, meta in STRATEGY_CATALOG[dim].items():
            entry = {"strategy": name, "equivalent_sql": None, "parameters": None}
            if dim == "alignment" and name == "align_by_device":
                if not alignment_applies(qf):
                    continue   # 单设备/非通配 → 不提供
                sql = _append_align_by_device(query, analysis)
                if sql is None:
                    continue   # 引擎不接受该改写 → 不提供
                entry["equivalent_sql"] = sql
            elif dim == "fetch_size" and name == "fetch_large":
                entry["parameters"] = {"fetch_size": FETCH_LARGE}
            out.append(entry)
        candidates[dim] = out
    return candidates
