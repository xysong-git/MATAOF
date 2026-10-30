"""SQLite 签名聚合存储：append-only 执行日志 + 签名聚合索引（存储设计 v2）。

设计（知识库去冗余 + 检索 O(唯一经验模式)）：
- **executions 表（append-only 原始执行日志）**：逐条执行事实
  （延迟/效果/状态/异常/时间戳），只 INSERT 不 UPDATE/DELETE——可审计性不变；
- **signatures 表（经验模式，静态部分 append-only）**：签名 = 内容哈希
  （紧凑查询特征 + 策略 + db/sys 上下文）；同质查询归并为一个签名条目，
  其聚合统计（count/成功率/延迟分布/首末时间戳）是**派生数据**（与评价表
  同性质，可重算/重写）——"各种类型的大量查询积累可迁移经验"的存储形态；
- **meta 表**：next_seq / evaluations / strategy_stats 快照。

收益：库体积随"经验多样性"增长而非查询量；检索扫描 O(唯一签名数)；
写入 O(1)（签名计数器更新 + 单行执行日志 INSERT）。
迁移：旧 v1 格式（records 表）首次加载时自动压缩迁移为新布局。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from typing import Optional

from mataof.agents.knowledge_memory.store import MemoryStore


def signature_of(record: dict) -> str:
    """记录的签名（内容哈希）：相似度依赖的全部静态字段。

    只取查询特征/策略/db-sys 上下文——执行指标不参与签名
    （同质查询的执行差异体现在聚合统计里，不产生新经验模式）。
    """
    static = {
        "query_record": {
            "query_features": (record.get("query_record") or {}).get("query_features"),
        },
        "strategy": record.get("strategy"),
        "context": record.get("context"),
    }
    canonical = json.dumps(static, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SQLiteStore(MemoryStore):
    """append-only 执行日志 + 签名聚合索引（线程安全 + 批量提交）。"""

    def __init__(self, path: Optional[str] = None, save_interval: int = 1):
        self._conn: Optional[sqlite3.Connection] = None
        self._pending: list[tuple[str, str, dict]] = []      # (sig_id, exec_id, execution 行)
        # 签名聚合（派生数据；sig_id → 聚合状态）
        self._sigs: dict[str, dict] = {}
        self._exec_total = 0                                  # 执行总数
        # 已存在签名被追加执行 → 聚合统计变化，但签名数不变；
        # 检索侧的向量矩阵按签名数缓存，必须据此刷新受影响的行。
        self._dirty_sigs: set[str] = set()
        super().__init__(path=None, save_interval=save_interval)
        self.path = path
        if path and os.path.exists(path):
            self._load()

    # ---- 连接与表 ----
    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS signatures "
                "(sig_id TEXT PRIMARY KEY, payload TEXT NOT NULL, first_ts INTEGER)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS executions "
                "(exec_id TEXT PRIMARY KEY, sig_id TEXT NOT NULL, latency_ms REAL, "
                "effect TEXT, status TEXT, anomalies TEXT, ts INTEGER, llm_note TEXT)")
            # v5 → v6：逐执行保存 LLM 经验总结（llm_note）。签名聚合库不保存
            # 逐条静态记录，但经验总结随执行事实审计，仍需可按 record_id 取回。
            cols = {row[1] for row in
                    self._conn.execute("PRAGMA table_info(executions)")}
            if "llm_note" not in cols:
                self._conn.execute(
                    "ALTER TABLE executions ADD COLUMN llm_note TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_exec_sig ON executions(sig_id)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ---- 加载（含 v1 → v2 迁移）----
    def _load(self) -> None:
        try:
            conn = self._connect()
            has_v1 = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='records'"
            ).fetchone() is not None
            if has_v1:
                self._migrate_v1(conn)
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
            meta = {k: json.loads(v) for k, v in rows}
            version = int(meta.get("version") or 0)
            self._next_seq = int(meta.get("next_seq", 0) or 1)
            self._evaluations = meta.get("evaluations") or {}
            self._strategy_stats = meta.get("strategy_stats") or {}
            self._sigs = meta.get("sigs") or {}
            self._exec_total = int(meta.get("exec_total") or 0)
            # payload/first_ts 从 signatures 表补入（meta 快照不重复存）
            if self._sigs:
                for sig_id, payload, first_ts in conn.execute(
                        "SELECT sig_id, payload, first_ts FROM signatures"):
                    agg = self._sigs.get(sig_id)
                    if agg is not None:
                        agg["payload"] = json.loads(payload)
                        agg["first_ts"] = first_ts
                        agg.setdefault("last_ts", first_ts)
            # 防御：meta 缺 sigs 快照（旧 v2 无快照/损坏）→ 从表重建
            if not self._sigs:
                self._rebuild_sigs(conn)
                self._exec_total = int(meta.get("exec_total")
                                       or conn.execute(
                                           "SELECT COUNT(*) FROM executions").fetchone()[0])
            # v3→v4：评价口径改为"已评估执行"分母（insufficient 不计入）；
            # v4→v5：unchanged 是中性结果，成功率分母改为方向明确的执行
            # （improved + degraded + failed）。旧快照无法区分，必须从
            # executions 逐条事实重建一次。
            needs_rebuild = bool(self._exec_total) and (
                version < 5
                or not self._evaluations or not self._strategy_stats
                or any(not isinstance(e, dict)
                       or any(k not in e for k in
                              ("evaluated", "failure", "unchanged"))
                       for e in self._evaluations.values())
                or any(not isinstance(s, dict)
                       or any(k not in s for k in
                              ("evaluated", "failure", "unchanged"))
                       for s in self._strategy_stats.values())
            )
            if needs_rebuild:
                self._rebuild_derived(conn)
        except (sqlite3.DatabaseError, json.JSONDecodeError, OSError,
                TypeError, ValueError):
            # 损坏的存储文件 → 从空库开始（不覆盖原文件，直到下一次保存）
            self.close()
            self._records = {}
            self._sigs = {}
            self._evaluations = {}
            self._strategy_stats = {}
            self._next_seq = 1
            self._exec_total = 0

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        """v1（records 表：每执行一条完整记录）→ v2 压缩迁移。

        迁移时同步重建派生表（评价表/策略统计——聚合口径与逐条一致）。
        """
        rows = conn.execute("SELECT record_id, payload FROM records").fetchall()
        for record_id, payload in rows:
            rec = json.loads(payload)
            self._ingest_signature(rec, record_id)
            self._record_strategy_stats_inplace(rec)
            self._record_evaluation_inplace(rec)
        conn.execute("DROP TABLE records")
        # 迁移后立即落盘新布局（含聚合快照与派生表），并压缩文件（一次性成本）
        self._pending_saves = 0
        payload = {"pending": list(self._pending),
                   "meta": self._meta_payload()}
        self._pending.clear()
        self._write_payload(payload)
        conn.execute("VACUUM")

    def _backfill_from_sigs(self) -> None:
        """派生表缺失时从签名聚合重建（防御路径；口径与逐条一致——
        计数按执行数加权，延迟用签名均值）。"""
        for sid, agg in self._sigs.items():
            rec = self._signature_record(sid, agg)
            qf = (rec.get("query_record") or {}).get("query_features") or {}
            query_type = qf.get("query_type") or "unknown"
            by_dim = (rec.get("strategy") or {}).get("by_dimension") or {}
            n = agg["count"]
            # 已评估执行 = 有明确效果判断的执行；insufficient/cancelled 不计入。
            evaluated = (agg["improved"] + agg["degraded"]
                         + agg["failed_timeout"] + agg.get("unchanged", 0))
            failure = agg["degraded"] + agg["failed_timeout"]
            unchanged = agg.get("unchanged", 0)
            lat = rec["execution"]["metrics"].get("response_time_ms")
            for dim, strategy_name in by_dim.items():
                if not strategy_name or strategy_name == "not_applicable":
                    continue
                key = self._evaluation_key(query_type, dim, strategy_name)
                e = self._evaluations.setdefault(
                    key, {"count": 0, "evaluated": 0, "success": 0,
                          "failure": 0, "unchanged": 0, "latencies": []})
                e["count"] += n
                e["evaluated"] += evaluated
                e["success"] += agg["improved"]
                e["failure"] += failure
                e["unchanged"] += unchanged
                if lat is not None:
                    e["latencies"].append(lat)   # 签名均值（加权口径）
            sid_key = (rec.get("strategy") or {}).get("strategy_id") or "unknown"
            s = self._strategy_stats.setdefault(
                sid_key, {"total": 0, "evaluated": 0, "success": 0,
                          "failure": 0, "unchanged": 0, "latencies": []})
            s["total"] += n
            s["evaluated"] += evaluated
            s["success"] += agg["improved"]
            s["failure"] += failure
            s["unchanged"] += unchanged
            if lat is not None:
                s["latencies"].append(lat)
        self._save()

    def _rebuild_derived(self, conn: sqlite3.Connection) -> None:
        """从 executions 逐条事实重建签名聚合与派生表（旧版本迁移/防御路径）。"""
        self._sigs = {}
        self._evaluations = {}
        self._strategy_stats = {}
        self._rebuild_sigs(conn)
        self._backfill_from_sigs()

    def _rebuild_sigs(self, conn: sqlite3.Connection) -> None:
        """从 executions + signatures 表重建内存聚合（meta 快照缺失时）。"""
        self._sigs = {}
        for sig_id, payload, first_ts in conn.execute(
                "SELECT sig_id, payload, first_ts FROM signatures"):
            self._sigs[sig_id] = {
                "payload": json.loads(payload), "first_ts": first_ts,
                "last_ts": first_ts, "count": 0, "improved": 0,
                "failed_timeout": 0, "degraded": 0, "unchanged": 0,
                "critical": {}, "lat_sum": 0.0, "lat_min": None,
                "lat_max": None,
            }
        for sig_id, latency, effect, status, anomalies, ts in conn.execute(
                "SELECT sig_id, latency_ms, effect, status, anomalies, ts "
                "FROM executions ORDER BY exec_id"):
            agg = self._sigs.get(sig_id)
            if agg is None:
                continue
            self._apply_execution(agg, latency, effect, status,
                                  json.loads(anomalies) if anomalies else [], ts)
        self._dirty_sigs.clear()   # 全量重建后无增量待刷新

    # ---- 聚合归并（O(1)/执行）----
    def _apply_execution(self, agg: dict, latency, effect: str, status: str,
                         critical: list, ts) -> None:
        agg["count"] += 1
        if ts:
            if agg["last_ts"] is None or ts > agg["last_ts"]:
                agg["last_ts"] = ts
            if agg["first_ts"] is None or ts < agg["first_ts"]:
                agg["first_ts"] = ts
        if effect == "improved":
            agg["improved"] += 1
        if status in ("failed", "timeout"):
            agg["failed_timeout"] += 1
        if effect == "degraded":
            agg["degraded"] += 1
        if effect == "unchanged":
            agg["unchanged"] = agg.get("unchanged", 0) + 1
        for t in critical:
            agg["critical"][t] = agg["critical"].get(t, 0) + 1
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            agg["lat_sum"] += float(latency)
            agg["lat_min"] = float(latency) if agg["lat_min"] is None \
                else min(agg["lat_min"], float(latency))
            agg["lat_max"] = float(latency) if agg["lat_max"] is None \
                else max(agg["lat_max"], float(latency))

    def _ingest_signature(self, rec: dict, record_id: str) -> None:
        """把一条完整 KM 记录归并进签名聚合（不落盘）。"""
        sig_id = signature_of(rec)
        agg = self._sigs.get(sig_id)
        if agg is None:
            payload = {
                "query_record": {
                    "query_id": (rec.get("query_record") or {}).get("query_id"),
                    "query_features": (rec.get("query_record") or {}).get("query_features"),
                },
                "strategy": rec.get("strategy"),
                "context": rec.get("context"),
            }
            agg = self._sigs[sig_id] = {
                "payload": payload, "first_ts": rec.get("timestamp"),
                "last_ts": None, "count": 0, "improved": 0,
                "failed_timeout": 0, "degraded": 0, "unchanged": 0,
                "critical": {}, "lat_sum": 0.0, "lat_min": None,
                "lat_max": None,
            }
        metrics = (rec.get("execution") or {}).get("metrics") or {}
        latency = metrics.get("response_time_ms")
        status = (rec.get("execution") or {}).get("status")
        effect = rec.get("execution_result") or ""
        critical = [a.get("type") or "anomaly" for a in (rec.get("anomalies") or [])
                    if a.get("severity") == "critical"]
        is_new = agg["count"] == 0
        self._apply_execution(agg, latency, effect, status, critical,
                              rec.get("timestamp"))
        if not is_new:
            self._dirty_sigs.add(sig_id)   # 已有签名统计变化 → 检索缓存需刷新
        note = rec.get("llm_note")
        self._pending.append((sig_id, record_id, {
            "latency_ms": latency,
            "effect": effect,
            "status": status,
            "anomalies": json.dumps(critical, ensure_ascii=False)
                         if critical else None,
            "ts": rec.get("timestamp"),
            "llm_note": note if isinstance(note, str) and note else None,
        }))
        self._exec_total += 1

    # ---- 追加（继承 add：评价表/统计照旧；签名归并 O(1)）----
    def add(self, record: dict) -> str:
        record_id = super().add(record)     # _records 内存字典 + 增量统计
        if self.path:
            self._ingest_signature(record, record_id)
        return record_id

    # ---- 落盘：执行日志批量 INSERT + 签名/元数据快照 ----
    def _meta_payload(self) -> dict:
        import copy as _copy
        # sigs 快照只存聚合计数器（payload/first_ts 在 signatures 表，不重复存储）
        sigs = {sid: {k: (dict(v) if k == "critical" else v)
                      for k, v in agg.items() if k != "payload" and k != "first_ts"}
                for sid, agg in self._sigs.items()}
        return {
            "version": 6,
            "next_seq": self._next_seq,
            "exec_total": self._exec_total,
            "evaluations": _copy.deepcopy(self._evaluations),
            "strategy_stats": _copy.deepcopy(self._strategy_stats),
            "sigs": sigs,
        }

    def _save(self) -> None:
        """全量保存（_backfill_derived 等路径）：增量提交 + 元数据。"""
        if not self.path:
            return
        payload = {"pending": list(self._pending), "meta": self._meta_payload()}
        self._pending.clear()
        self._pending_saves = 0
        self._write_payload(payload)

    def _maybe_save_locked(self) -> Optional[dict]:
        """锁内调用：按 save_interval 决定是否产出提交载荷（增量 + 元数据）。"""
        if not self.path:
            return None
        self._pending_saves += 1
        if self._pending_saves >= self.save_interval:
            self._pending_saves = 0
            payload = {"pending": list(self._pending),
                       "meta": self._meta_payload()}
            self._pending.clear()
            return payload
        return None

    def flush(self) -> None:
        """强制提交（批次结束/进程退出前调用）。"""
        if not self.path:
            return
        payload = None
        with self._lock:
            if self._pending_saves > 0 or self._pending:
                self._pending_saves = 0
                payload = {"pending": list(self._pending),
                           "meta": self._meta_payload()}
                self._pending.clear()
        if payload is not None:
            self._write_payload(payload)

    def _write_payload(self, payload: dict) -> None:
        if not self.path:
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            for sig_id, exec_id, ex in payload.get("pending") or []:
                agg = self._sigs.get(sig_id)
                payload_json = json.dumps(agg["payload"], ensure_ascii=False,
                                          separators=(",", ":")) if agg else None
                if payload_json is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO signatures (sig_id, payload, first_ts) "
                        "VALUES (?, ?, ?)",
                        (sig_id, payload_json, agg["first_ts"]))
                conn.execute(
                    "INSERT OR IGNORE INTO executions "
                    "(exec_id, sig_id, latency_ms, effect, status, anomalies, ts, "
                    "llm_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (exec_id, sig_id, ex.get("latency_ms"), ex.get("effect"),
                     ex.get("status"), ex.get("anomalies"), ex.get("ts"),
                     ex.get("llm_note")))
            meta = payload["meta"]
            conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                [(k, json.dumps(v, ensure_ascii=False, separators=(",", ":")))
                 for k, v in meta.items()])
            conn.execute("COMMIT")
        except sqlite3.DatabaseError:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise

    # ---- 只读：签名聚合视图 ----
    def _dominant_effect(self, agg: dict) -> str:
        if agg["count"] == 0:
            return "unchanged"
        improved = agg["improved"]
        worse = agg["degraded"] + agg["failed_timeout"]
        if improved >= worse:
            return "improved" if improved > 0 else "unchanged"
        return "degraded"

    def pop_dirty_signatures(self) -> set[str]:
        """取出并清空"已有签名统计发生变化"的集合（检索缓存增量刷新用）。"""
        with self._lock:
            dirty = set(self._dirty_sigs)
            self._dirty_sigs.clear()
        return dirty

    def signature_record(self, sig_id: str) -> Optional[dict]:
        """按 sig_id 合成当前聚合记录（签名不存在 → None）。"""
        with self._lock:
            agg = self._sigs.get(sig_id)
            return self._signature_record(sig_id, agg) if agg is not None else None

    def _signature_record(self, sig_id: str, agg: dict) -> dict:
        """合成签名记录（KM 布局 + 聚合扩展字段；检索与统计的消费形态）。"""
        payload = agg["payload"]
        qr = payload.get("query_record") or {}
        n = agg["count"]
        lat_avg = round(agg["lat_sum"] / n, 2) if n and agg["lat_sum"] else None
        status = "failed" if (agg["failed_timeout"] / n) >= 0.5 and n else "success"
        return {
            "record_id": sig_id,
            "timestamp": agg["last_ts"],        # 时效衰减口径：最近一次执行
            "query_record": qr,
            "strategy": payload.get("strategy"),
            "context": payload.get("context"),
            "execution": {
                "metrics": {"response_time_ms": lat_avg},
                "status": status,
            },
            "execution_result": self._dominant_effect(agg),
            "anomalies": [{"type": t, "severity": "critical"}
                          for t in agg["critical"]],
            # ---- 聚合扩展字段（检索/证据按 count 加权使用）----
            "execution_count": n,
            "evaluated_count": (agg["improved"] + agg["degraded"]
                                + agg["failed_timeout"]
                                + agg.get("unchanged", 0)),
            "improved_count": agg["improved"],
            "failure_count": agg["failed_timeout"] + agg["degraded"]
                            + sum(agg["critical"].values()),
            "failed_timeout_count": agg["failed_timeout"],
            "degraded_count": agg["degraded"],
            "unchanged_count": agg.get("unchanged", 0),
            "critical_counts": dict(agg["critical"]),
            "success_ratio": round(agg["improved"] / n, 4) if n else 0.0,
            "latency_avg_ms": lat_avg,
            "latency_min_ms": agg["lat_min"],
            "latency_max_ms": agg["lat_max"],
            "first_ts": agg["first_ts"],
        }

    def all(self, start: int = 0) -> list[dict]:
        """全部经验模式（签名合成记录；检索扫描的集合——O(唯一签名数)）。

        start>0 → 只构建第 start 个签名之后的合成记录（增量重建用，
        避免每次新增签名都重建全部合成字典）。
        """
        with self._lock:
            items = list(self._sigs.items())[start:]
            return [self._signature_record(sid, agg) for sid, agg in items]

    def get(self, record_id: str) -> Optional[dict]:
        """按执行 record_id 查询：返回所属签名合成记录（无则 None）。"""
        if not self.path and not self._sigs:
            return None
        conn = self._connect()
        row = conn.execute(
            "SELECT sig_id, llm_note FROM executions WHERE exec_id = ?",
            (record_id,)).fetchone()
        if row is None:
            return None
        agg = self._sigs.get(row[0])
        if not agg:
            return None
        record = self._signature_record(row[0], agg)
        if row[1]:
            record["llm_note"] = row[1]
        return record

    def count(self) -> int:
        """执行总数（原始事实条数）。"""
        with self._lock:
            return self._exec_total

    def signature_count(self) -> int:
        """唯一经验模式数（签名数）。"""
        with self._lock:
            return len(self._sigs)
