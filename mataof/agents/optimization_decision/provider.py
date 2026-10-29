"""决策 Agent 的内置候选生成器：按查询生成 equivalent_sql / 执行参数。

原则不变：决策 Agent 只选择、不生成 SQL；候选生成与选择同属 Agent 内部。
为真实可送达执行层的杠杆生成改写/参数（全部有实测依据，见
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
from .plans.sql import has_full_path_filter, has_last

FETCH_LARGE = 10000


def _append_align_by_device(query: str, analysis: Optional[dict]) -> Optional[str]:
    """在查询尾部追加 ALIGN BY DEVICE（引擎约束校验后）。

    实测约束（与早期假设相反，已用真实 IoTDB 2.0.6 复核）：
    - **显式设备列表**（root.db.g_0.d_0, root.db.g_0.d_1）**可以**使用
      ALIGN BY DEVICE；
    - WHERE 中含完整测点路径谓词（root.db.g_0.d_0.s_0）时引擎返回 701；
    - LAST 查询、已有 ALIGN BY DEVICE 的查询不改写；
    - 单设备查询不做该改写（无收益）。
    """
    text = query.strip().rstrip(";").strip()
    if not text or "ALIGN BY DEVICE" in text.upper():
        return None
    qf = (analysis or {}).get("query_features") or {}
    if not alignment_applies(qf):
        return None   # 单设备 → 对齐方式无差异
    if has_last(text) or has_full_path_filter(text):
        return None   # 引擎拒绝/语义不适用
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


def build_plans(query: str, analysis: Optional[dict],
                device_inventory: Any = None,
                max_parallelism: int = 8) -> list[dict]:
    """计划候选（统一入口）：编译为**可执行**的计划描述符列表。

    与 build_candidates 同属 Agent 内置候选生成；成本模型只做选择。生成器保证：
    只有能编译出真实子查询 + 合并规格的计划才会返回（不可确定的结构直接
    不生成，而不是生成后失败）。
    """
    from .plans.generator import build_plan_candidates
    return build_plan_candidates(query, analysis, device_inventory, max_parallelism)


def plan_generation_notes(query: str, analysis: Optional[dict],
                          device_inventory: Any = None) -> list[str]:
    """计划生成器的事实说明（用于决策 notes；不含策略建议）。"""
    from .plans.generator import plan_generation_notes as _notes
    return _notes(query, analysis, device_inventory)
