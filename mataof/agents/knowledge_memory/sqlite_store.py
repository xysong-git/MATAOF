"""SQLite 知识存储：增量 INSERT 持久化（持久化层改造方案 2）。

与 MemoryStore 同协议、同内存结构（append-only 记录 dict + 派生表），
只替换持久化机制：
- 记录：records 表（record_id 主键 + payload JSON 文本），只 INSERT 不 UPDATE/DELETE；
- 元数据：meta 表（next_seq / evaluations / strategy_stats 的 JSON 快照）。

收益（相对整库 JSON dump）：写入 O(新增) 而非 O(全库)；事务提交原子；
加载可只读元数据 + 流式取行。连接线程安全（内部锁 + check_same_thread=False）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

from mataof.agents.knowledge_memory.store import MemoryStore


class SQLiteStore(MemoryStore):
    """append-only SQLite 持久化存储（线程安全 + 批量提交）。"""

    def __init__(self, path: Optional[str] = None, save_interval: int = 1):
        self._conn: Optional[sqlite3.Connection] = None
        self._pending_records: list[tuple[str, dict]] = []   # 未提交的 (record_id, record)
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
                "CREATE TABLE IF NOT EXISTS records "
                "(record_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
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

    # ---- 加载 ----
    def _load(self) -> None:
        try:
            conn = self._connect()
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
            meta = {k: json.loads(v) for k, v in rows}
            self._next_seq = int(meta.get("next_seq", 0) or 1)
            self._evaluations = meta.get("evaluations") or {}
            self._strategy_stats = meta.get("strategy_stats") or {}
            for record_id, payload in conn.execute(
                    "SELECT record_id, payload FROM records"):
                self._records[record_id] = json.loads(payload)
            if self._records and (not self._evaluations or not self._strategy_stats):
                self._backfill_derived()
        except (sqlite3.DatabaseError, json.JSONDecodeError, OSError,
                TypeError, ValueError):
            # 损坏的存储文件 → 从空库开始（不覆盖原文件，直到下一次保存）
            self.close()
            self._records = {}
            self._evaluations = {}
            self._strategy_stats = {}
            self._next_seq = 1

    # ---- 追加（继承 add；记录待提交清单）----
    def add(self, record: dict) -> str:
        record_id = super().add(record)
        if self.path:
            self._pending_records.append((record_id, self._records[record_id]))
        return record_id

    def _meta_payload(self) -> dict:
        """元数据快照（小：不含 records）。"""
        import copy as _copy
        return {
            "version": 2,
            "next_seq": self._next_seq,
            "evaluations": _copy.deepcopy(self._evaluations),
            "strategy_stats": _copy.deepcopy(self._strategy_stats),
        }

    def _save(self) -> None:
        """全量保存（_backfill_derived 等路径）：增量提交 + 元数据。"""
        if not self.path:
            return
        payload = {"pending_records": list(self._pending_records),
                   "meta": self._meta_payload()}
        self._pending_records.clear()
        self._pending_saves = 0
        self._write_payload(payload)

    def _maybe_save_locked(self) -> Optional[dict]:
        """锁内调用：按 save_interval 决定是否产出提交载荷（增量 + 元数据）。"""
        if not self.path:
            return None
        self._pending_saves += 1
        if self._pending_saves >= self.save_interval:
            self._pending_saves = 0
            payload = {"pending_records": list(self._pending_records),
                       "meta": self._meta_payload()}
            self._pending_records.clear()
            return payload
        return None

    def flush(self) -> None:
        """强制提交（批次结束/进程退出前调用）。"""
        if not self.path:
            return
        payload = None
        with self._lock:
            if self._pending_saves > 0 or self._pending_records:
                self._pending_saves = 0
                payload = {"pending_records": list(self._pending_records),
                           "meta": self._meta_payload()}
                self._pending_records.clear()
        if payload is not None:
            self._write_payload(payload)

    # ---- 落盘：事务提交（只 INSERT 新记录，绝不改写已有行）----
    def _write_payload(self, payload: dict) -> None:
        if not self.path:
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            if payload.get("pending_records"):
                conn.executemany(
                    "INSERT OR IGNORE INTO records (record_id, payload) VALUES (?, ?)",
                    [(rid, json.dumps(rec, ensure_ascii=False,
                                      separators=(",", ":")))
                     for rid, rec in payload["pending_records"]])
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
