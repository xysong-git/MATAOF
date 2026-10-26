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

from time import perf_counter
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


def _counts_of(rec: dict) -> tuple:
    """记录的执行计数分布（兼容两种形态）。

    签名合成记录（execution_count 扩展字段）→ 聚合分布；
    原始逐条记录（json/jsonl 后端）→ (1, 逐条事实)。
    返回 (total, improved, failure, failed_timeout, degraded, critical_counts)。
    """
    n = rec.get("execution_count")
    if isinstance(n, int) and n > 0:
        return (n, rec.get("improved_count") or 0, rec.get("failure_count") or 0,
                rec.get("failed_timeout_count") or 0,
                rec.get("degraded_count") or 0,
                dict(rec.get("critical_counts") or {}))
    improved = 1 if rec.get("execution_result") == "improved" else 0
    failure = 1 if _is_failure(rec) else 0
    ft = 1 if failure and (rec.get("execution") or {}).get("status") in (
        "failed", "timeout") else 0
    dg = 1 if rec.get("execution_result") == "degraded" else 0
    crit: dict = {}
    if failure:
        for an in rec.get("anomalies") or []:
            if an.get("severity") == "critical":
                t = an.get("type") or "anomaly"
                crit[t] = crit.get(t, 0) + 1
    return (1, improved, failure, ft, dg, crit)


def _latency_tuple_of(rec: dict) -> Optional[tuple]:
    """记录的延迟元组 (sum, count, min, max)——签名记录用聚合值。"""
    if isinstance(rec.get("execution_count"), int) and rec.get("execution_count", 0) > 0:
        n = rec["execution_count"]
        avg = rec.get("latency_avg_ms")
        if avg is None or n == 0:
            return None
        return (float(avg) * n, n, rec.get("latency_min_ms"), rec.get("latency_max_ms"))
    lat = _latency_of(rec)
    if lat is None:
        return None
    return (lat, 1, lat, lat)


def _finalize_agg(agg: dict) -> tuple:
    """把每策略聚合结果定稿为 (successful_strategies, failed_strategies,
    strategy_statistics)——与优化前逐字段一致（等价性由测试保证）。"""
    successful_strategies: list[dict] = []
    failed_strategies: list[dict] = []
    strategy_statistics: dict[str, dict] = {}
    for sid in sorted(agg):
        a = agg[sid]
        total = a["total"]
        ratio = round(a["success"] / total, 4) if total else 0.0
        lat_sum, lat_count = a["lat_sum"], a["lat_count"]
        strategy_statistics[sid] = {
            "total": total,
            "success": a["success"],
            "failure": a["failure"],
            "success_ratio": ratio,
            "avg_latency_ms": round(lat_sum / lat_count, 2)
                              if lat_count else None,
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
                    "avg_latency_ms": round(lat_sum / lat_count, 2)
                                      if lat_count else None,
                    "min_latency_ms": a["lat_min"],
                    "max_latency_ms": a["lat_max"],
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
    return successful_strategies, failed_strategies, strategy_statistics


def _agg_rows(store) -> list:
    """按记录顺序的轻量投影行（惰性构建 + 增量追加；与向量矩阵记录顺序对齐）。

    每条记录一次性提取聚合所需字段（消除重复模式提取）：(strategy_id,
    qtype, range_level, db_keys, 执行计数分布, 延迟元组)。
    记录 append-only 不可变 → 缓存始终有效；增量只处理新增条目。
    """
    sig_counter = getattr(store, "signature_count", None)
    n = sig_counter() if sig_counter is not None else store.count()
    rows = getattr(store, "_agg_rows", None)
    if rows is not None and rows["_len"] == n:
        return rows["rows"]
    start = rows["_len"] if rows is not None else 0
    out = list(rows["rows"]) if rows is not None else []
    records = store.all(start=start) if start else store.all()
    for rec in records:
        strat = rec.get("strategy") or {}
        sid = strat.get("strategy_id") or "unknown"
        qf = (rec.get("query_record") or {}).get("query_features") or {}
        qtype = qf.get("query_type") or "unknown"
        rl = (qf.get("time") or {}).get("range_level") or "unknown"
        dbk = tuple((rec.get("context") or {}).get("database_state") or {})
        out.append((sid, qtype, rl, dbk,
                    _counts_of(rec), _latency_tuple_of(rec)))
    store._agg_rows = {"_len": n, "rows": out}
    return out


def _aggregate_rows(rows: list, order, overall, q_s, db_s, sy_s,
                    records: list, top_k, aggregate: bool = True,
                    totals=None) -> tuple:
    """向量化路径（优化）：投影行 + 降序索引单遍聚合 + 只物化 top-K。

    aggregate=True（展示/API 默认）：分层计数（签名 + 执行双口径）+ 每策略
    聚合统计（按 execution_count 加权——与逐条存储精确一致）+ top-K 物化。
    aggregate=False（决策路径，runner 使用）：分层计数直接用 numpy 计算
    （totals 加权），只物化 top-K——O(1) 条目的 Python 循环。
    返回 (matched_top, n_strong, n_moderate, n_weak, w_strong, w_moderate,
          w_weak, successful_strategies, failed_strategies, strategy_statistics)。
    """
    from collections import Counter

    if not aggregate:
        import numpy as np
        n_strong = int(np.count_nonzero(overall >= 0.8))
        n_moderate = int(np.count_nonzero(overall >= 0.5)) - n_strong
        n_weak = int(np.count_nonzero(overall >= 0.3)) - n_strong - n_moderate
        if totals is None:
            w_strong = w_moderate = w_weak = 0
        else:
            w_strong = int(totals[overall >= 0.8].sum())
            w_moderate = int(totals[(overall >= 0.5) & (overall < 0.8)].sum())
            w_weak = int(totals[(overall >= 0.3) & (overall < 0.5)].sum())
        matched_top: list[dict] = []
        for idx in order:
            ov = float(overall[idx])
            if ov < WEAK_MATCH_THRESHOLD:
                break   # 降序：后续均低于阈值
            if top_k is not None and len(matched_top) >= top_k:
                break
            matched_top.append({
                "record": records[idx],
                "level": match_level(ov),
                "query": round(float(q_s[idx]), 4),
                "database": round(float(db_s[idx]), 4),
                "system": round(float(sy_s[idx]), 4),
                "overall": round(ov, 4),
            })
        return (matched_top, n_strong, n_moderate, n_weak,
                w_strong, w_moderate, w_weak, [], [], {})

    matched_top: list[dict] = []
    n_strong = n_moderate = n_weak = 0
    w_strong = w_moderate = w_weak = 0
    agg: dict[str, dict] = {}
    for idx in order:
        ov = float(overall[idx])
        level = match_level(ov)
        if level is None:
            break   # 降序：后续均低于阈值
        sid, qtype, rl, dbk, counts, lat = rows[idx]
        total, improved, failure, ft, dg, crit = counts
        if level == "strong_match":
            n_strong += 1
            w_strong += total
        elif level == "moderate_match":
            n_moderate += 1
            w_moderate += total
        else:
            n_weak += 1
            w_weak += total
        a = agg.get(sid)
        if a is None:
            a = agg[sid] = {
                "total": 0, "success": 0, "failure": 0,
                "lat_sum": 0.0, "lat_count": 0,
                "lat_min": None, "lat_max": None,
                "qtypes": Counter(), "levels": Counter(), "db_keys": set(),
                "ftypes": Counter(),
            }
        a["total"] += total
        a["success"] += improved
        a["failure"] += failure
        if ft:
            a["ftypes"]["execution_failed"] += ft
        if dg:
            a["ftypes"]["degraded"] += dg
        for t, c in crit.items():
            a["ftypes"][t] += c
        if lat is not None:
            a["lat_sum"] += lat[0]
            a["lat_count"] += lat[1]
            if lat[2] is not None:
                a["lat_min"] = lat[2] if a["lat_min"] is None else min(a["lat_min"], lat[2])
            if lat[3] is not None:
                a["lat_max"] = lat[3] if a["lat_max"] is None else max(a["lat_max"], lat[3])
        a["qtypes"][qtype] += total
        a["levels"][rl] += total
        a["db_keys"].update(dbk)
        # top-K 物化（仅前 K 条；其余匹配记录不构建临时条目）
        if top_k is None or len(matched_top) < top_k:
            matched_top.append({
                "record": records[idx],
                "level": level,
                "query": round(float(q_s[idx]), 4),
                "database": round(float(db_s[idx]), 4),
                "system": round(float(sy_s[idx]), 4),
                "overall": round(ov, 4),
            })
    successful, failed, stats = _finalize_agg(agg)
    return (matched_top, n_strong, n_moderate, n_weak,
            w_strong, w_moderate, w_weak, successful, failed, stats)


def _aggregate_matches(items, top_k, aggregate: bool = True):
    """单遍融合（优化：聚合 + 分层计数 + top-K 物化一次完成）。

    items ：按相似度降序（同分按 record_id 升序）的可迭代对象，元素为
            (record, overall, query, database, system)；query/database/system
            仅在物化 top-K 条目时取用（可传原始 numpy 值，物化时统一取整）。
    top_k ：物化条目上限（None → 全部物化）。
    aggregate=False（决策路径）：跳过每策略聚合（historical_summary 置空），
    其余输出与完整路径逐字段一致。
    返回 ：(matched_top, n_strong, n_moderate, n_weak, successful_strategies,
           failed_strategies, strategy_statistics)——与优化前逐字段一致
           （等价性由 tests/test_knowledge_memory_agent.py 保证）。
    """
    from collections import Counter
    matched_top: list[dict] = []
    n_strong = n_moderate = n_weak = 0
    w_strong = w_moderate = w_weak = 0
    # 每策略聚合：total/success/failure/延迟元组/qtypes/levels/db_keys/ftypes
    agg: dict[str, dict] = {}
    for rec, ov, q, db, sy in items:
        level = match_level(ov)
        if level is None:
            break   # 输入已降序：后续均低于阈值
        total, improved, failure, ft, dg, crit = _counts_of(rec)
        if level == "strong_match":
            n_strong += 1
            w_strong += total
        elif level == "moderate_match":
            n_moderate += 1
            w_moderate += total
        else:
            n_weak += 1
            w_weak += total
        # top-K 物化（仅前 K 条；其余匹配记录不构建临时条目）
        if top_k is None or len(matched_top) < top_k:
            matched_top.append({
                "record": rec,
                "level": level,
                "query": round(float(q), 4),
                "database": round(float(db), 4),
                "system": round(float(sy), 4),
                "overall": round(float(ov), 4),
            })
        if not aggregate:
            continue   # 决策路径：跳过每策略聚合（summary 只用于展示/报告）
        sid = (rec.get("strategy") or {}).get("strategy_id") or "unknown"
        a = agg.get(sid)
        if a is None:   # 两步创建：避免 setdefault 每条记录都构造默认值（含 3 个 Counter）
            a = agg[sid] = {
                "total": 0, "success": 0, "failure": 0,
                "lat_sum": 0.0, "lat_count": 0,
                "lat_min": None, "lat_max": None,
                "qtypes": Counter(), "levels": Counter(), "db_keys": set(),
                "ftypes": Counter(),
            }
        a["total"] += total
        a["success"] += improved
        a["failure"] += failure
        if ft:
            a["ftypes"]["execution_failed"] += ft
        if dg:
            a["ftypes"]["degraded"] += dg
        for t, c in crit.items():
            a["ftypes"][t] += c
        lat = _latency_tuple_of(rec)
        if lat is not None:
            a["lat_sum"] += lat[0]
            a["lat_count"] += lat[1]
            if lat[2] is not None:
                a["lat_min"] = lat[2] if a["lat_min"] is None else min(a["lat_min"], lat[2])
            if lat[3] is not None:
                a["lat_max"] = lat[3] if a["lat_max"] is None else max(a["lat_max"], lat[3])
        qf = (rec.get("query_record") or {}).get("query_features") or {}
        a["qtypes"][qf.get("query_type") or "unknown"] += total
        a["levels"][(qf.get("time") or {}).get("range_level") or "unknown"] += total
        a["db_keys"].update((rec.get("context") or {}).get("database_state") or {})

    # ---- 成功/失败模式提取与策略统计（共享定稿逻辑，与逐条口径一致）----
    successful_strategies, failed_strategies, strategy_statistics = _finalize_agg(agg)
    return (matched_top, n_strong, n_moderate, n_weak,
            w_strong, w_moderate, w_weak,
            successful_strategies, failed_strategies, strategy_statistics)


def retrieve(store, query_analysis: dict, database_state: Optional[dict],
             system_state: Optional[dict], query_id: str = "",
             max_records: Optional[int] = None,
             timing_out: Optional[dict] = None,
             summary: bool = True) -> dict:
    """检索与当前查询上下文相关的历史知识。

    store          ：MemoryStore
    query_analysis ：Query Analysis Agent 输出（提供查询特征）
    database_state / system_state：当前上下文（可选）
    max_records    ：返回的匹配记录上限（按相似度 top-K）。
                     None → 不截断（API 默认）；runner 默认 100（控制证据规模）。
                     historical_summary 的统计始终基于全部匹配记录计算，
                     截断只影响 matched_records / matched_records_for_decision 列表。
    timing_out     ：可选 dict——传入时填充细粒度计时（perf_counter 墙钟，逐阶段
                     定位耗时环节）。返回值保持确定性（计时不进输出）。
    summary        ：False = 决策路径——跳过全匹配集的每策略聚合
                     （historical_summary 置空、summary_skipped=True）；分层计数
                     用 numpy 直接计算、只物化 top-K，检索成本与库大小近乎解耦。
                     决策消费的 matched_records / matched_records_for_decision /
                     match_counts / strategy_evaluations 与完整路径逐字段一致
                     （等价性由测试保证）。
    """
    from mataof.similarity import compact_features

    t_retrieve = perf_counter()
    current_compact = compact_features(query_analysis) if isinstance(query_analysis, dict) else {}
    current_db = database_state if isinstance(database_state, dict) else {}
    current_sys = system_state if isinstance(system_state, dict) else {}
    t_compact = perf_counter()

    matched_top: list[dict] = []
    n_strong = n_moderate = n_weak = 0
    w_strong = w_moderate = w_weak = 0
    successful_strategies: list[dict] = []
    failed_strategies: list[dict] = []
    strategy_statistics: dict[str, dict] = {}
    vectorized = False
    top_k = int(max_records) if max_records is not None and int(max_records) > 0 else None
    try:
        import numpy  # noqa: F401
        from mataof.similarity import build_vector_matrix, vectorized_similarities
        mat = build_vector_matrix(store)
        t_matrix = perf_counter()
        if mat is not None:
            # 向量化路径（路线 2）：矩阵运算替代逐条 Python 循环，语义与逐条完全一致
            q_s, db_s, sy_s, overall = vectorized_similarities(
                current_compact, current_db, current_sys, mat)
            t_scan = perf_counter()
            # stable argsort：等分记录保持知识库顺序（record_id 升序），
            # 与 sorted(-overall, record_id) 一致——后续不再重复排序
            order = numpy.argsort(-overall, kind="stable")
            t_argsort = perf_counter()

            # 投影行快路径：聚合字段一次性提取（惰性增量缓存），单遍完成
            # 分层计数（签名 + 执行双口径）+ 策略聚合（execution_count 加权）
            # + top-K 物化（与逐条路径语义一致）；
            # summary=False（决策路径）：跳过聚合，numpy 计数 + 只物化 top-K
            rows = _agg_rows(store) if summary else None
            (matched_top, n_strong, n_moderate, n_weak,
             w_strong, w_moderate, w_weak,
             successful_strategies, failed_strategies,
             strategy_statistics) = _aggregate_rows(
                 rows, order, overall, q_s, db_s, sy_s, mat["records"], top_k,
                 aggregate=summary, totals=mat.get("totals"))
            vectorized = (n_strong + n_moderate + n_weak) > 0
            t_match_loop = perf_counter()
    except ImportError:
        t_matrix = t_scan = t_argsort = t_match_loop = perf_counter()
        pass   # 无 numpy → 回退逐条路径
    if not vectorized:
        # 逐条路径（小库/无 numpy/向量化零匹配）
        matched_plain: list[dict] = []
        for record in store.all():
            # 直接传原始记录：相似度模块兼容 KM 布局（免重建 view，大库扫描更快）
            sims = component_similarities(current_compact, current_db, current_sys, record)
            level = match_level(sims["overall"])
            if level is None:
                continue
            matched_plain.append({"record": record, "level": level, **sims})
        t_scan = perf_counter()
        t_argsort = t_scan    # 逐条路径无 argsort（小库/无 numpy/空库）
        matched_plain.sort(key=lambda m: (-m["overall"], m["record"]["record_id"]))
        t_sort = perf_counter()
        (matched_top, n_strong, n_moderate, n_weak,
         w_strong, w_moderate, w_weak,
         successful_strategies, failed_strategies,
         strategy_statistics) = _aggregate_matches(
            ((m["record"], m["overall"], m["query"], m["database"], m["system"])
             for m in matched_plain),
            top_k, aggregate=summary)
        t_match_loop = perf_counter()
    else:
        t_sort = t_match_loop   # 向量化路径跳过冗余排序（argsort 已保序）

    # ---- top-K 截断（summary 统计已基于全部匹配）----
    n_matched = n_strong + n_moderate + n_weak
    truncated: Optional[dict] = None
    if top_k is not None and n_matched > top_k:
        truncated = {"listed": len(matched_top), "total_matched": n_matched}

    # ---- matched_records（规格输出；matched_top 即 listed，只物化 top-K）----
    matched_records: list[dict] = []
    for m in matched_top:
        rec = m["record"]
        strat = rec.get("strategy") or {}
        metrics = (rec.get("execution") or {}).get("metrics") or {}
        entry = {
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
        }
        n_exec = rec.get("execution_count")
        if isinstance(n_exec, int) and n_exec > 0:
            # 签名聚合条目（存储 v2）：携带覆盖执行数与聚合统计
            entry["execution_count"] = n_exec
            entry["success_ratio"] = rec.get("success_ratio")
            entry["latency_min_ms"] = rec.get("latency_min_ms")
            entry["latency_max_ms"] = rec.get("latency_max_ms")
        matched_records.append(entry)
    t_serialize = perf_counter()

    # ---- 知识置信度：强/中/弱匹配按执行数加权（封顶 1.0；逐条存储时 w==n）----
    knowledge_confidence = round(min(1.0, 0.25 * w_strong + 0.15 * w_moderate
                                     + 0.05 * w_weak), 2)
    t_aggregate = perf_counter()   # 聚合已融合进匹配单遍（计时键名保留，便于前后对比）

    # ---- 决策 Agent 兼容记录（扩展：可直接作为 historical_records 输入）----
    # 同样按 top-K 截断（决策证据规模有界）；summary 统计不受影响。
    # 每条携带 similarity（H1：决策端直接复用，不再重算相似度）；
    # 签名聚合条目携带 count（覆盖执行数，决策端按 count 加权）。
    records_for_decision: list[dict] = []
    for m in matched_top:
        rec = m["record"]
        lat = _latency_of(rec)
        records_for_decision.append({
            "record_id": rec["record_id"],
            "record_time": rec.get("timestamp"),  # 签名条目 = 最近执行时间（衰减口径）
            "count": rec.get("execution_count", 1),
            "similarity": m["overall"],          # 预计算相似度（决策端跳过重算）
            "query_features": (rec.get("query_record") or {}).get("query_features") or {},
            "database_state": (rec.get("context") or {}).get("database_state") or {},
            "system_state": (rec.get("context") or {}).get("system_state") or {},
            "strategy": (rec.get("strategy") or {}).get("by_dimension") or {},
            "execution_feedback": {
                "latency_ms": lat,               # 签名条目 = 平均延迟（聚合口径）
                "effect": rec.get("execution_result"),
            },
        })

    t_evidence = perf_counter()
    evaluations = store.evaluations()
    t_evaluations = perf_counter()

    # ---- 细粒度计时（诊断用：写入调用方传入的 timing_out，返回值保持确定性）----
    def _ms(a: float, b: float) -> float:
        return round((b - a) * 1000.0, 2)

    if timing_out is not None:
        timing_out.update({
            "compact_ms": _ms(t_retrieve, t_compact),
            "matrix_ms": _ms(t_compact, t_matrix),
            "scan_ms": _ms(t_matrix, t_scan),
            "argsort_ms": _ms(t_scan, t_argsort),
            "match_loop_ms": _ms(t_argsort, t_match_loop),
            "sort_ms": _ms(t_match_loop, t_sort),
            "serialize_ms": _ms(t_sort, t_serialize),
            "aggregate_ms": _ms(t_serialize, t_aggregate),
            "evidence_ms": _ms(t_aggregate, t_evidence),
            "evaluations_ms": _ms(t_evidence, t_evaluations),
            "total_ms": _ms(t_retrieve, t_evaluations),
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
        "match_counts": {"strong": n_strong, "moderate": n_moderate, "weak": n_weak},  # 扩展：签名/记录数
        "match_executions": {"strong": w_strong, "moderate": w_moderate,
                             "weak": w_weak},                    # 扩展：覆盖执行数（加权口径）
        "truncated": truncated,                                  # 扩展：top-K 截断说明（未截断为 null）
        "strategy_evaluations": evaluations,                     # 扩展：策略评价表（D1 动态权重）
        "summary_skipped": not summary,                          # 扩展：决策路径跳过展示聚合
    }
