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
        self._next_seq = 1
        self._pending_saves = 0
        self._lock = threading.Lock()
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
            # 回填：旧版本存储文件没有评价表 → 从存量记录派生重建（派生数据，可重算）
            if self._records and not self._evaluations:
                self._backfill_evaluations()
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            # 损坏的存储文件 → 从空库开始（不覆盖原文件，直到下一次保存）
            self._records = {}
            self._evaluations = {}
            self._next_seq = 1

    def _backfill_evaluations(self) -> None:
        """从全部存量记录重建评价表（内存聚合 + 一次持久化）。"""
        for rec in self._records.values():
            self._record_evaluation_inplace(rec)
        self._save()

    def _save(self) -> None:
        if not self.path:
            return
        payload = {
            "version": 1,
            "next_seq": self._next_seq,
            "records": self._records,
            "evaluations": self._evaluations,   # 派生聚合，随入库同步持久化
        }
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            # 紧凑序列化（无缩进）：大库落盘开销显著降低（B5）
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, self.path)
        self._pending_saves = 0

    def _maybe_save(self) -> None:
        """按 save_interval 批量落盘（B5）：未达阈值仅累积，flush() 强制写入。"""
        self._pending_saves += 1
        if self._pending_saves >= self.save_interval:
            self._save()

    def flush(self) -> None:
        """强制落盘（批次结束/进程退出前调用，避免批量模式丢失尾部更新）。"""
        with self._lock:
            if self._pending_saves > 0:
                self._save()

    # ---- 追加（唯一写操作）----
    def add(self, record: dict) -> str:
        """追加一条记录，返回分配的 record_id。不修改任何已有记录。"""
        with self._lock:
            record_id = f"rec{self._next_seq:08d}"
            self._next_seq += 1
            stored = dict(record)
            stored["record_id"] = record_id
            self._records[record_id] = stored
            self._maybe_save()
        return record_id

    # ---- 策略评价表（派生聚合，可重写；原始记录不可变）----
    @staticmethod
    def _evaluation_key(query_type: str, dim: str, strategy_name: str) -> str:
        return f"{query_type or 'unknown'}|{dim}|{strategy_name}"

    def record_evaluation(self, record: dict) -> dict:
        """把一条已入库记录聚合进评价表，返回本次更新的条目（键→条目）。"""
        with self._lock:
            updated = self._record_evaluation_inplace(record)
            if updated:
                self._maybe_save()
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

    def statistics(self) -> dict:
        """全库累计统计（按 strategy_id 聚合的成功/失败与平均延迟）。"""
        stats: dict[str, dict] = {}
        with self._lock:
            records = list(self._records.values())
        for rec in records:
            sid = (rec.get("strategy") or {}).get("strategy_id") or "unknown"
            entry = stats.setdefault(sid, {"total": 0, "success": 0, "failure": 0,
                                           "latencies": []})
            entry["total"] += 1
            effect = rec.get("execution_result") or ""
            if effect == "improved":
                entry["success"] += 1
            elif effect in ("degraded", "failed"):
                entry["failure"] += 1
            lat = ((rec.get("execution") or {}).get("metrics") or {}).get("response_time_ms")
            if isinstance(lat, (int, float)) and not isinstance(lat, bool):
                entry["latencies"].append(float(lat))
        out = {}
        for sid, e in stats.items():
            lats = e["latencies"]
            out[sid] = {
                "total": e["total"],
                "success": e["success"],
                "failure": e["failure"],
                "success_ratio": round(e["success"] / e["total"], 4) if e["total"] else 0.0,
                "avg_latency_ms": round(sum(lats) / len(lats), 2) if lats else None,
            }
        return out
