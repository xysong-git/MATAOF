"""决策上下文规范化：输入解析、校验与派生字段。

输入契约（decision_input）：
    query_id            查询标识
    query_analysis      Query Analysis Agent 的完整输出（必填；决策不得仅依据 SQL 文本）
    database_state      数据库状态（数据规模/分布/分区信息/统计信息等；缺失 → unknown）
    system_state        系统状态（CPU/内存/IO 利用率等；缺失 → unknown）
    historical_records  Knowledge Memory Agent 的历史记录列表（缺失 → 无历史证据）
    candidate_strategies 候选策略集合（按维度 dict 或扁平名称列表；缺失 → 使用目录全集）
    baseline_strategy   安全 baseline（按维度 dict 或单一策略名；缺失 → 内置默认）
    reference_time      可选：历史时效衰减的参考时间（epoch 毫秒）

任何输入问题都不抛异常：记录到 errors/notes，降级处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from mataof.agents.optimization_decision.catalog import (
    BASELINE_STRATEGIES,
    DIMENSIONS,
    STRATEGY_CATALOG,
    catalog_strategy,
    dimension_of_strategy,
)


@dataclass
class DecisionContext:
    query_id: str
    analysis: Optional[dict] = None          # 分析 Agent 原始输出
    query_type: Optional[str] = None
    features: dict = field(default_factory=dict)   # analysis["query_features"]
    compact_features: dict = field(default_factory=dict)  # 历史相似度用摘要
    analysis_confidence: Optional[float] = None
    database_state: dict = field(default_factory=dict)
    system_state: dict = field(default_factory=dict)
    historical_records: list = field(default_factory=list)
    candidates: dict = field(default_factory=dict)   # 维度 → 合法候选名列表
    baseline: dict = field(default_factory=dict)     # 维度 → baseline 策略名
    reference_time: Optional[int] = None
    errors: list = field(default_factory=list)       # 输入问题（invalid_input 依据）
    notes: list = field(default_factory=list)        # 可追踪说明（输出 notes 字段）

    # ---- 派生信号 ----
    @property
    def non_time_filter_count(self) -> int:
        counts = (self.features.get("filter") or {}).get("type_counts") or {}
        return sum(v for k, v in counts.items() if k != "time")

    @property
    def load_level(self) -> str:
        """由 system_state 的 CPU/内存/IO 利用率推导负载等级。

        数值约定：0~1 的小数或 0~100 的百分数；>0.7 视为高负载，全部 <0.3 视为低负载。
        未提供 → unknown（不做任何假设）。
        """
        keys = ("cpu_utilization", "memory_utilization", "io_utilization")
        vals = []
        for k in keys:
            v = (self.system_state or {}).get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(v / 100.0 if v > 1 else v)
        if not vals:
            return "unknown"
        if any(v > 0.7 for v in vals):
            return "high"
        if all(v < 0.3 for v in vals):
            return "low"
        return "normal"

    @property
    def has_time_filter(self) -> bool:
        return bool((self.features.get("time") or {}).get("has_time_filter"))

    @property
    def time_window_group_by(self) -> bool:
        gbd = (self.features.get("aggregation") or {}).get("group_by_details") or {}
        return gbd.get("type") == "time_window"


def compact_features(analysis: dict) -> dict:
    """把分析输出压缩为历史记录契约中的 query_features 摘要（相似度计算用）。"""
    f = analysis.get("query_features") or {}
    t = f.get("time") or {}
    d = f.get("device") or {}
    fl = f.get("filter") or {}
    agg = f.get("aggregation") or {}
    counts = fl.get("type_counts") or {}
    return {
        "query_type": analysis.get("query_type") or "unknown",
        "time": {
            "has_time_filter": bool(t.get("has_time_filter")),
            "time_span": t.get("time_span"),
            "range_level": t.get("range_level") or "unknown",
        },
        "device": {
            "device_count": d.get("device_count"),
            "multi_device": d.get("multi_device"),
        },
        "filter": {
            "non_time_filter_count": sum(v for k, v in counts.items() if k != "time"),
        },
        "aggregation": {
            "has_aggregation": bool(agg.get("has_aggregation")),
            "has_group_by": bool(agg.get("has_group_by")),
            "has_window": bool(agg.get("has_window")),
            "functions": sorted({r.get("function") for r in agg.get("aggregation_functions") or []}),
        },
    }


def _normalize_candidates(raw: Any, notes: list) -> dict[str, list[str]]:
    """候选集合归一化：按维度 dict 或扁平名称列表 → 目录内合法候选。"""
    out: dict[str, list[str]] = {}
    if raw is None:
        for d in DIMENSIONS:
            out[d] = list(STRATEGY_CATALOG[d].keys())
        return out
    if isinstance(raw, dict):
        for d in DIMENSIONS:
            items = raw.get(d)
            if items is None:
                out[d] = list(STRATEGY_CATALOG[d].keys())  # 该维度未指定 → 目录全集
                continue
            if not isinstance(items, list):
                notes.append(f"候选策略维度 {d} 的值不是列表，忽略，使用目录全集")
                out[d] = list(STRATEGY_CATALOG[d].keys())
                continue
            valid, dropped = [], []
            for name in items:
                if catalog_strategy(d, name) is not None:
                    valid.append(name)
                else:
                    dropped.append(name)
            if dropped:
                notes.append(f"候选策略中被忽略的未知名称（维度 {d}）：{dropped}（不在策略目录中）")
            out[d] = valid if valid else list(STRATEGY_CATALOG[d].keys())
        return out
    if isinstance(raw, list):
        for name in raw:
            d = dimension_of_strategy(name)
            if d is None:
                notes.append(f"候选策略中被忽略的未知名称：{name}（不在策略目录中）")
                continue
            out.setdefault(d, []).append(name)
        for d in DIMENSIONS:
            if d not in out:
                out[d] = list(STRATEGY_CATALOG[d].keys())
        return out
    notes.append("candidate_strategies 格式无法识别，使用策略目录全集")
    return {d: list(STRATEGY_CATALOG[d].keys()) for d in DIMENSIONS}


def _normalize_baseline(raw: Any, notes: list) -> dict[str, str]:
    out = dict(BASELINE_STRATEGIES)
    if raw is None:
        return out
    if isinstance(raw, str):
        d = dimension_of_strategy(raw)
        if d is None:
            notes.append(f"baseline 策略名未知：{raw}，使用内置默认 baseline")
            return out
        for dim in DIMENSIONS:
            out[dim] = raw
        return out
    if isinstance(raw, dict):
        for dim in DIMENSIONS:
            name = raw.get(dim)
            if name is None:
                continue
            if catalog_strategy(dim, name) is None:
                notes.append(f"baseline 策略名未知（维度 {dim}）：{name}，该维度使用内置默认")
                continue
            out[dim] = name
        return out
    notes.append("baseline_strategy 格式无法识别，使用内置默认 baseline")
    return out


def normalize_input(decision_input: dict) -> DecisionContext:
    """把外部输入归一化为 DecisionContext；不抛异常。"""
    ctx = DecisionContext(query_id=str((decision_input or {}).get("query_id") or ""))
    if not isinstance(decision_input, dict):
        ctx.errors.append("decision_input 不是 JSON 对象")
        return ctx

    analysis = decision_input.get("query_analysis")
    if not isinstance(analysis, dict) or not (analysis.get("query_features")):
        ctx.errors.append("缺少 query_analysis（Query Analysis Agent 输出）；决策不得仅依据 SQL 文本")
        return ctx

    ctx.analysis = analysis
    ctx.features = analysis.get("query_features") or {}
    ctx.query_type = analysis.get("query_type") or "unknown"
    ctx.compact_features = compact_features(analysis)
    ac = analysis.get("analysis_confidence")
    ctx.analysis_confidence = float(ac) if isinstance(ac, (int, float)) and 0 <= ac <= 1 else None

    db = decision_input.get("database_state")
    ctx.database_state = db if isinstance(db, dict) else {}
    if db is not None and not isinstance(db, dict):
        ctx.notes.append("database_state 不是对象，视为 unknown")
    sy = decision_input.get("system_state")
    ctx.system_state = sy if isinstance(sy, dict) else {}
    if sy is not None and not isinstance(sy, dict):
        ctx.notes.append("system_state 不是对象，视为 unknown")

    hist = decision_input.get("historical_records")
    ctx.historical_records = hist if isinstance(hist, list) else []
    if hist is not None and not isinstance(hist, list):
        ctx.notes.append("historical_records 不是列表，视为无历史记录")

    ctx.candidates = _normalize_candidates(decision_input.get("candidate_strategies"), ctx.notes)
    ctx.baseline = _normalize_baseline(decision_input.get("baseline_strategy"), ctx.notes)

    rt = decision_input.get("reference_time")
    ctx.reference_time = int(rt) if isinstance(rt, (int, float)) and not isinstance(rt, bool) else None
    return ctx
