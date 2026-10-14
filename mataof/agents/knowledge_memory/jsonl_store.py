"""JSONL 知识存储：追加式 JSON Lines 持久化（持久化层改造方案 1）。

与 MemoryStore 同协议、同内存结构（append-only 记录 dict + 派生表），
只替换持久化机制：
- 记录：<path>.records 追加式 JSONL（每行一条，只追加、不重写）；
- 元数据：<path>（next_seq + evaluations + strategy_stats，小文件原子写）。

收益（相对整库 JSON dump）：写入 O(新增) 而非 O(全库)；落盘不再随库线性恶化。
兼容：<path> 为旧格式单文件 JSON 库时自动读取（首次落盘后迁移为新布局）。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Optional

from mataof.agents.knowledge_memory.store import MemoryStore


class JSONLStore(MemoryStore):
    """append-only JSONL 持久化存储（线程安全 + 批量落盘）。

    严格限制与派生表语义与 MemoryStore 完全一致（见 store.py 文档）。
    """

    def __init__(self, path: Optional[str] = None, save_interval: int = 1):
        self._pending_records: list[tuple[str, dict]] = []   # 未落盘的 (record_id, record)
        super().__init__(path=None, save_interval=save_interval)
        self.path = path
        if path and (os.path.exists(path)
                     or os.path.exists(self._records_path(path))):
            self._load()

    @staticmethod
    def _records_path(path: str) -> str:
        return f"{path}.records"

    # ---- 加载 ----
    def _load(self) -> None:
        try:
            meta: dict = {}
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    meta = json.load(f) or {}
            rec_path = self._records_path(self.path)
            legacy = False
            if not os.path.exists(rec_path):
                # 旧格式：path 本身是单文件 JSON 库（records 在顶层）
                if isinstance(meta.get("records"), dict):
                    self._records = meta["records"]
                    self._next_seq = int(meta.get("next_seq", len(self._records) + 1))
                    self._evaluations = meta.get("evaluations") or {}
                    self._strategy_stats = meta.get("strategy_stats") or {}
                    legacy = True
            else:
                with open(rec_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        self._records[rec["record_id"]] = rec
                self._next_seq = int(meta.get("next_seq", len(self._records) + 1))
                self._evaluations = meta.get("evaluations") or {}
                self._strategy_stats = meta.get("strategy_stats") or {}
            if legacy:
                # 迁移：把记录写入追加式文件（path 位置将让给元数据，避免数据丢失）
                os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".",
                            exist_ok=True)
                with open(rec_path, "w", encoding="utf-8") as f:
                    for rec in self._records.values():
                        f.write(json.dumps(rec, ensure_ascii=False,
                                           separators=(",", ":")) + "\n")
            if self._records and (not self._evaluations or not self._strategy_stats):
                self._backfill_derived()
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            # 损坏的存储文件 → 从空库开始（不覆盖原文件，直到下一次保存）
            self._records = {}
            self._evaluations = {}
            self._strategy_stats = {}
            self._next_seq = 1

    # ---- 追加（继承 add；记录待落盘清单）----
    def add(self, record: dict) -> str:
        record_id = super().add(record)
        if self.path:
            self._pending_records.append((record_id, self._records[record_id]))
        return record_id

    def _meta_payload(self) -> dict:
        """元数据快照（小文件：不含 records）。"""
        import copy as _copy
        return {
            "version": 2,
            "next_seq": self._next_seq,
            "evaluations": _copy.deepcopy(self._evaluations),
            "strategy_stats": _copy.deepcopy(self._strategy_stats),
        }

    def _save(self) -> None:
        """全量保存（_backfill_derived 等路径）：增量记录 + 元数据，不做全库重写。"""
        if not self.path:
            return
        payload = {"pending_records": list(self._pending_records),
                   "meta": self._meta_payload()}
        self._pending_records.clear()
        self._pending_saves = 0
        self._write_payload(payload)

    def _maybe_save_locked(self) -> Optional[dict]:
        """锁内调用：按 save_interval 决定是否产出落盘载荷（只含增量记录 + 元数据）。"""
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
        """强制落盘（批次结束/进程退出前调用）。"""
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

    # ---- 落盘：记录追加 + 元数据原子写 ----
    def _write_payload(self, payload: dict) -> None:
        if not self.path:
            return
        # 元数据与记录文件用同一把写锁串行（tmp 路径并发写保护）
        with self._write_lock:
            if payload.get("pending_records"):
                os.makedirs(os.path.dirname(os.path.abspath(self.path))
                            or ".", exist_ok=True)
                with open(self._records_path(self.path), "a", encoding="utf-8") as f:
                    for _rid, rec in payload["pending_records"]:
                        f.write(json.dumps(rec, ensure_ascii=False,
                                           separators=(",", ":")) + "\n")
            tmp = f"{self.path}.tmp.{os.getpid()}.{threading.get_ident()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload["meta"], f, ensure_ascii=False,
                          separators=(",", ":"))
            os.replace(tmp, self.path)
