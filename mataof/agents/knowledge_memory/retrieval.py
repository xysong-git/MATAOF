"""知识检索：三层匹配、历史汇总与成功/失败经验模式提取。

匹配分层（综合相似度，多维特征而非 SQL 文本匹配）：
- Strong Match   ：overall ≥ 0.8
- Moderate Match ：0.5 ≤ overall < 0.8
- Weak Match     ：0.3 ≤ overall < 0.5
低于 0.3 的记录不返回。优先参考"查询特征相似 + 数据库状态相似 + 系统状态相似"的记录。

成功经验模式（successful strategy pattern）：
- 在匹配记录中，某策略累计次数 ≥ MIN_PATTERN_COUNT（默认 3）且成功率 ≥
  MIN_SUCCESS_RATIO（默认 0.6）→ 形成模式条目：策略、查询条件摘要、数据库状态摘要、
  性能表现、次数、成功比例。

失败经验：保留并汇总（failed/degraded 或含 critical 异常的执行），
单次异常不会删除/否定历史知识——历史样本始终保留，评价随累计结果更新。

历史记录不是绝对规则：检索结果仅供 Optimization Decision Agent 作为证据参考。
"""

from __future__ import annotations

from typing import Any, Optional

from mataof.similarity import component_similarities

# 匹配分层阈值（可配置）
STRONG_MATCH_THRESHOLD = 0.8
MODERATE_MATCH_THRESHOLD = 0.5
WEAK_MATCH_THRESHOLD = 0.3

# 成功模式判定
MIN_PATTERN_COUNT = 3
MIN_SUCCESS_RATIO = 0.6


def match_level(overall: float) -> Optional[str]:
    if overall >= STRONG_MATCH_THRESHOLD:
        return "strong_match"
    if overall >= MODERATE_MATCH_THRESHOLD:
        return "moderate_match"
    if overall >= WEAK_MATCH_THRESHOLD:
        return "weak_match"
    return None


def _latency_of(record: dict) -> Optional[float]:
    lat = ((record.get("execution") or {}).get("metrics") or {}).get("response_time_ms")
    if isinstance(lat, (int, float)) and not isinstance(lat, bool):
        return float(lat)
    return None


def _is_failure(record: dict) -> bool:
    if record.get("execution_result") in ("degraded", "failed"):
        return True
    if record.get("execution", {}).get("status") in ("failed", "timeout"):
        return True
    if any(a.get("severity") == "critical" for a in record.get("anomalies") or []):
        return True
    return False


def _query_condition_summary(records: list) -> str:
    """匹配记录中该策略的查询条件摘要（出现最多的查询类型与时间范围等级）。"""
    from collections import Counter
    qtypes = Counter()
    levels = Counter()
    for r in records:
        qf = r.get("query_record", {}).get("query_features") or {}
        qtypes[qf.get("query_type") or "unknown"] += 1
        levels[(qf.get("time") or {}).get("range_level") or "unknown"] += 1
    parts = [qtypes.most_common(1)[0][0] if qtypes else "unknown"]
    if levels:
        parts.append(f"时间范围 {levels.most_common(1)[0][0]}")
    return " · ".join(parts)


def _database_state_summary(records: list) -> str:
    keys: set = set()
    for r in records:
        keys.update((r.get("context", {}).get("database_state") or {}).keys())
    return "、".join(sorted(keys)) if keys else "（无数据库状态记录）"


def _strategy_records(matched: list, strategy_id: str) -> list:
    return [m["record"] for m in matched
            if (m["record"].get("strategy") or {}).get("strategy_id") == strategy_id]


def retrieve(store, query_analysis: dict, database_state: Optional[dict],
             system_state: Optional[dict], query_id: str = "",
             max_records: Optional[int] = None) -> dict:
    """检索与当前查询上下文相关的历史知识。

    store          ：MemoryStore
    query_analysis ：Query Analysis Agent 输出（提供查询特征）
    database_state / system_state：当前上下文（可选）
    max_records    ：返回的匹配记录上限（按相似度 top-K）。
                     None → 不截断（API 默认）；runner 默认 100（控制证据规模）。
                     historical_summary 的统计始终基于全部匹配记录计算，
                     截断只影响 matched_records / matched_records_for_decision 列表。
    """
    from mataof.similarity import compact_features

    current_compact = compact_features(query_analysis) if isinstance(query_analysis, dict) else {}
    current_db = database_state if isinstance(database_state, dict) else {}
    current_sys = system_state if isinstance(system_state, dict) else {}

    matched: list[dict] = []
    vectorized = False
    try:
        import numpy  # noqa: F401
        from mataof.similarity import build_vector_matrix, vectorized_similarities
        mat = build_vector_matrix(store)
        if mat is not None:
            # 向量化路径（路线 2）：矩阵运算替代逐条 Python 循环，语义与逐条完全一致
            q_s, db_s, sy_s, overall = vectorized_similarities(
                current_compact, current_db, current_sys, mat)
            order = numpy.argsort(-overall, kind="stable")   # 降序；stable 保持记录顺序
            for idx in order:
                ov = float(overall[idx])
                level = match_level(ov)
                if level is None:
                    break   # 降序：后续都低于阈值
                matched.append({
                    "record": mat["records"][idx],
                    "level": level,
                    "query": round(float(q_s[idx]), 4),
                    "database": round(float(db_s[idx]), 4),
                    "system": round(float(sy_s[idx]), 4),
                    "overall": round(ov, 4),
                })
            vectorized = bool(matched)
    except ImportError:
        pass   # 无 numpy → 回退逐条路径
    if not vectorized:
        # 逐条路径（小库或无 numpy）
        for record in store.all():
            # 直接传原始记录：相似度模块兼容 KM 布局（免重建 view，大库扫描更快）
            sims = component_similarities(current_compact, current_db, current_sys, record)
            level = match_level(sims["overall"])
            if level is None:
                continue
            matched.append({"record": record, "level": level, **sims})

    # 排序：综合相似度降序，record_id 升序（确定性；向量化路径已降序，此排序幂等）
    matched.sort(key=lambda m: (-m["overall"], m["record"]["record_id"]))

    # ---- top-K 截断（summary 统计仍基于全部匹配）----
    listed = matched
    truncated: Optional[dict] = None
    if max_records is not None and int(max_records) > 0 and len(matched) > int(max_records):
        listed = matched[: int(max_records)]
        truncated = {"listed": len(listed), "total_matched": len(matched)}

    # ---- matched_records（规格输出）----
    matched_records: list[dict] = []
    for m in listed:
        rec = m["record"]
        strat = rec.get("strategy") or {}
        metrics = (rec.get("execution") or {}).get("metrics") or {}
        matched_records.append({
            "record_id": rec["record_id"],
            "similarity": m["overall"],
            "query_similarity": m["query"],
            "database_similarity": m["database"],
            "system_similarity": m["system"],
            "match_level": m["level"],           # 扩展：匹配分层
            "strategy_id": strat.get("strategy_id") or "",
            "execution_result": rec.get("execution_result") or "",
            "performance": {
                "response_time_ms": metrics.get("response_time_ms"),
                "execution_status": (rec.get("execution") or {}).get("status"),
                "effect": (rec.get("experience") or {}).get("effect"),
            },
        })

    # ---- 汇总：成功/失败模式与策略统计（单遍遍历，O(N)，大库友好）----
    from collections import Counter
    successful_strategies: list[dict] = []
    failed_strategies: list[dict] = []
    strategy_statistics: dict[str, dict] = {}
    n_strong = n_moderate = n_weak = 0

    # 每策略聚合：total/success/failure/lats/qtype/levels/db_keys/ftypes
    agg: dict[str, dict] = {}
    for m in matched:
        if m["level"] == "strong_match":
            n_strong += 1
        elif m["level"] == "moderate_match":
            n_moderate += 1
        else:
            n_weak += 1
        rec = m["record"]
        sid = (rec.get("strategy") or {}).get("strategy_id") or "unknown"
        a = agg.setdefault(sid, {
            "total": 0, "success": 0, "failure": 0, "lats": [],
            "qtypes": Counter(), "levels": Counter(), "db_keys": set(),
            "ftypes": Counter(),
        })
        a["total"] += 1
        if rec.get("execution_result") == "improved":
            a["success"] += 1
        if _is_failure(rec):
            a["failure"] += 1
            if (rec.get("execution") or {}).get("status") in ("failed", "timeout"):
                a["ftypes"]["execution_failed"] += 1
            if rec.get("execution_result") == "degraded":
                a["ftypes"]["degraded"] += 1
            for an in rec.get("anomalies") or []:
                if an.get("severity") == "critical":
                    a["ftypes"][an.get("type") or "anomaly"] += 1
        lat = _latency_of(rec)
        if lat is not None:
            a["lats"].append(lat)
        qf = (rec.get("query_record") or {}).get("query_features") or {}
        a["qtypes"][qf.get("query_type") or "unknown"] += 1
        a["levels"][(qf.get("time") or {}).get("range_level") or "unknown"] += 1
        a["db_keys"].update((rec.get("context") or {}).get("database_state") or {})

    for sid in sorted(agg):
        a = agg[sid]
        total = a["total"]
        ratio = round(a["success"] / total, 4) if total else 0.0
        lats = a["lats"]
        strategy_statistics[sid] = {
            "total": total,
            "success": a["success"],
            "failure": a["failure"],
            "success_ratio": ratio,
            "avg_latency_ms": round(sum(lats) / len(lats), 2) if lats else None,
        }
        if total >= MIN_PATTERN_COUNT and ratio >= MIN_SUCCESS_RATIO:
            qs = a["qtypes"].most_common(1)[0][0] if a["qtypes"] else "unknown"
            ls = a["levels"].most_common(1)[0][0] if a["levels"] else "unknown"
            successful_strategies.append({
                "strategy_id": sid,
                "query_condition_summary": f"{qs} · 时间范围 {ls}",
                "database_state_summary": "、".join(sorted(a["db_keys"]))
                                          if a["db_keys"] else "（无数据库状态记录）",
                "performance": {
                    "avg_latency_ms": round(sum(lats) / len(lats), 2) if lats else None,
                    "min_latency_ms": min(lats) if lats else None,
                    "max_latency_ms": max(lats) if lats else None,
                },
                "count": total,
                "success_ratio": ratio,
            })
        if a["failure"] > 0:
            failed_strategies.append({
                "strategy_id": sid,
                "count": a["failure"],
                "failure_ratio": round(a["failure"] / total, 4) if total else 0.0,
                "failure_types": dict(a["ftypes"]),
            })

    # ---- 知识置信度：强/中/弱匹配加权，封顶 1.0 ----
    knowledge_confidence = round(min(1.0, 0.25 * n_strong + 0.15 * n_moderate + 0.05 * n_weak), 2)

    # ---- 决策 Agent 兼容记录（扩展：可直接作为 historical_records 输入）----
    # 同样按 top-K 截断（决策证据规模有界）；summary 统计不受影响。
    # 每条携带 similarity（H1：决策端直接复用，不再重算相似度）。
    records_for_decision: list[dict] = []
    for m in listed:
        rec = m["record"]
        lat = _latency_of(rec)
        records_for_decision.append({
            "record_id": rec["record_id"],
            "record_time": rec.get("timestamp"),
            "similarity": m["overall"],          # 预计算相似度（决策端跳过重算）
            "query_features": (rec.get("query_record") or {}).get("query_features") or {},
            "database_state": (rec.get("context") or {}).get("database_state") or {},
            "system_state": (rec.get("context") or {}).get("system_state") or {},
            "strategy": (rec.get("strategy") or {}).get("by_dimension") or {},
            "execution_feedback": {
                "latency_ms": lat,
                "effect": rec.get("execution_result"),
            },
        })

    return {
        "query_id": query_id,
        "matched_records": matched_records,
        "historical_summary": {
            "successful_strategies": successful_strategies,
            "failed_strategies": failed_strategies,
            "strategy_statistics": strategy_statistics,
        },
        "knowledge_confidence": knowledge_confidence,
        "matched_records_for_decision": records_for_decision,   # 扩展
        "match_counts": {"strong": n_strong, "moderate": n_moderate, "weak": n_weak},  # 扩展
        "truncated": truncated,                                  # 扩展：top-K 截断说明（未截断为 null）
        "strategy_evaluations": store.evaluations(),             # 扩展：策略评价表（D1 动态权重）
    }
