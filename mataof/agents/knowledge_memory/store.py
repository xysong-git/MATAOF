"""知识存储：append-only 的 JSON 持久化 MemoryStore。

严格限制（对应规格）：
- 只允许追加（add），不提供修改/删除接口——禁止修改真实历史数据、
  禁止删除与当前决策不一致的历史记录；
- 历史样本永久保留，策略评价通过累计统计逐步更新；
- 持久化采用原子写（临时文件 + os.replace），避免损坏既有数据；
- record_id 由存储分配（单调递增序号），保证唯一且可追踪。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Optional


class MemoryStore:
    """append-only 知识存储 + 派生策略评价表（线程安全 + 批量落盘）。

    - 历史记录只追加（无修改/删除接口）；
    - 评价表（strategy evaluation table）是记录的**派生聚合**：
      键 = f"{query_type}|{dimension}|{strategy_name}"，
      值 = {count, success, success_ratio, avg_latency_ms}，
      随每次入库原子更新并持久化——原始记录不受影响，
      评价表可重写（派生数据），历史样本永久保留；
    - 线程安全：读写均加锁（支持查询级并行运行）；
    - save_interval>1 时批量落盘（B5）：每 N 次入库持久化一次，
      flush() 强制落盘（批次结束/退出时调用）；文件为紧凑 JSON（无缩进）。
    """

    def __init__(self, path: Optional[str] = None, save_interval: int = 1):
        self.path = path
        self.save_interval = max(1, int(save_interval))
        self._records: dict[str, dict] = {}
        self._evaluations: dict[str, dict] = {}
        self._strategy_stats: dict[str, dict] = {}   # 增量统计（strategy_id 粒度）
        self._next_seq = 1
        self._pending_saves = 0
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()   # 串行化文件写（tmp 路径并发写保护）
        if path and os.path.exists(path):
            self._load()

    # ---- 加载/保存 ----
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("records"), dict):
                self._records = data["records"]
                self._next_seq = int(data.get("next_seq", len(self._records) + 1))
                evals = data.get("evaluations")
                if isinstance(evals, dict):
                    self._evaluations = evals
                stats = data.get("strategy_stats")
                if isinstance(stats, dict):
                    self._strategy_stats = stats
            # 回填：旧版本存储文件没有派生表 → 从存量记录重建（派生数据，可重算）
            if self._records and (not self._evaluations or not self._strategy_stats):
                self._backfill_derived()
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            # 损坏的存储文件 → 从空库开始（不覆盖原文件，直到下一次保存）
            self._records = {}
            self._evaluations = {}
            self._strategy_stats = {}
            self._next_seq = 1

    def _backfill_derived(self) -> None:
        """从全部存量记录重建评价表与增量统计（内存聚合 + 一次持久化）。"""
        for rec in self._records.values():
            self._record_evaluation_inplace(rec)
            self._record_strategy_stats_inplace(rec)
        self._save()

    def _snapshot_payload(self) -> dict:
        """在锁内构建落盘快照：records 浅拷贝（记录不可变，安全），
        派生表深拷贝（条目数量小）。"""
        import copy as _copy
        return {
            "version": 1,
            "next_seq": self._next_seq,
            "records": dict(self._records),
            "evaluations": _copy.deepcopy(self._evaluations),
            "strategy_stats": _copy.deepcopy(self._strategy_stats),
        }

    def _write_payload(self, payload: dict) -> None:
        """数据锁外 dump + 原子写（B5）。

        tmp 文件名唯一（pid+线程），多线程/多实例并发写互不干扰；
        原子 replace 保证任何时刻文件完整。
        """
        tmp_path = f"{self.path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            # 紧凑序列化（无缩进）：大库落盘开销显著降低（B5）
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, self.path)

    def _save(self) -> None:
        if not self.path:
            return
        payload = self._snapshot_payload()
        self._write_payload(payload)
        self._pending_saves = 0

    def _maybe_save_locked(self) -> Optional[dict]:
        """锁内调用：按 save_interval 决定是否产出落盘快照（dump/写盘在锁外）。

        仅内存模式（path=None）不落盘，返回 None。
        """
        if not self.path:
            return None
        self._pending_saves += 1
        if self._pending_saves >= self.save_interval:
            payload = self._snapshot_payload()
            self._pending_saves = 0
            return payload
        return None

    def flush(self) -> None:
        """强制落盘（批次结束/进程退出前调用，避免批量模式丢失尾部更新）。"""
        if not self.path:
            return
        payload = None
        with self._lock:
            if self._pending_saves > 0:
                payload = self._snapshot_payload()
                self._pending_saves = 0
        if payload is not None:
            self._write_payload(payload)

    # ---- 追加（唯一写操作）----
    def add(self, record: dict) -> str:
        """追加一条记录，返回分配的 record_id。不修改任何已有记录。

        落盘采用"锁内快照 + 锁外 dump/写盘"：大库时避免长时间持锁
        阻塞其他 worker（快照依赖记录不可变性与派生表深拷贝）。
        """
        payload = None
        with self._lock:
            record_id = f"rec{self._next_seq:08d}"
            self._next_seq += 1
            stored = dict(record)
            stored["record_id"] = record_id
            self._records[record_id] = stored
            self._record_strategy_stats_inplace(stored)   # 增量统计（O(1) 查询）
            payload = self._maybe_save_locked()
        if payload is not None:
            self._write_payload(payload)
        return record_id

    # ---- 策略评价表（派生聚合，可重写；原始记录不可变）----
    @staticmethod
    def _evaluation_key(query_type: str, dim: str, strategy_name: str) -> str:
        return f"{query_type or 'unknown'}|{dim}|{strategy_name}"

    def record_evaluation(self, record: dict) -> dict:
        """把一条已入库记录聚合进评价表，返回本次更新的条目（键→条目）。"""
        payload = None
        with self._lock:
            updated = self._record_evaluation_inplace(record)
            if updated:
                payload = self._maybe_save_locked()
        if payload is not None:
            self._write_payload(payload)
        return updated

    def _record_evaluation_inplace(self, record: dict) -> dict:
        """内存内聚合一条记录进评价表（不落盘）。返回更新条目。"""
        updated: dict = {}
        qf = (record.get("query_record") or {}).get("query_features") or {}
        query_type = qf.get("query_type") or "unknown"
        by_dim = (record.get("strategy") or {}).get("by_dimension") or {}
        effect = record.get("execution_result") or ""
        lat = ((record.get("execution") or {}).get("metrics") or {}).get("response_time_ms")
        lat_ok = isinstance(lat, (int, float)) and not isinstance(lat, bool)
        for dim, strategy_name in by_dim.items():
            if not strategy_name or strategy_name == "not_applicable":
                continue
            key = self._evaluation_key(query_type, dim, strategy_name)
            e = self._evaluations.setdefault(
                key, {"count": 0, "success": 0, "latencies": []})
            e["count"] += 1
            if effect == "improved":
                e["success"] += 1
            if lat_ok:
                e["latencies"].append(float(lat))
                if len(e["latencies"]) > 1000:      # 只保留最近样本（内存有界）
                    e["latencies"] = e["latencies"][-1000:]
            updated[key] = {
                "count": e["count"],
                "success": e["success"],
                "success_ratio": round(e["success"] / e["count"], 4),
                "avg_latency_ms": round(sum(e["latencies"]) / len(e["latencies"]), 2)
                                  if e["latencies"] else None,
            }
        return updated

    def evaluations(self) -> dict:
        """评价表摘要：键 → {count, success, success_ratio, avg_latency_ms}。"""
        with self._lock:
            out: dict = {}
            for key, e in self._evaluations.items():
                out[key] = {
                    "count": e["count"],
                    "success": e["success"],
                    "success_ratio": round(e["success"] / e["count"], 4) if e["count"] else 0.0,
                    "avg_latency_ms": round(sum(e["latencies"]) / len(e["latencies"]), 2)
                                      if e["latencies"] else None,
                }
        return out

    # ---- 只读操作 ----
    def get(self, record_id: str) -> Optional[dict]:
        with self._lock:
            return self._records.get(record_id)

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._records.values())

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def _record_strategy_stats_inplace(self, record: dict) -> None:
        """内存内增量更新 strategy_id 粒度统计（statistics 的 O(1) 来源）。"""
        sid = (record.get("strategy") or {}).get("strategy_id") or "unknown"
        e = self._strategy_stats.setdefault(
            sid, {"total": 0, "success": 0, "failure": 0, "latencies": []})
        e["total"] += 1
        effect = record.get("execution_result") or ""
        if effect == "improved":
            e["success"] += 1
        elif effect in ("degraded", "failed"):
            e["failure"] += 1
        lat = ((record.get("execution") or {}).get("metrics") or {}).get("response_time_ms")
        if isinstance(lat, (int, float)) and not isinstance(lat, bool):
            e["latencies"].append(float(lat))
            if len(e["latencies"]) > 1000:      # 只保留最近样本（内存有界）
                e["latencies"] = e["latencies"][-1000:]

    def statistics(self) -> dict:
        """全库累计统计（按 strategy_id 聚合；增量维护，O(策略数) 而非 O(记录数)）。"""
        with self._lock:
            out = {}
            for sid, e in self._strategy_stats.items():
                lats = e["latencies"]
                out[sid] = {
                    "total": e["total"],
                    "success": e["success"],
                    "failure": e["failure"],
                    "success_ratio": round(e["success"] / e["total"], 4) if e["total"] else 0.0,
                    "avg_latency_ms": round(sum(lats) / len(lats), 2) if lats else None,
                }
        return out
