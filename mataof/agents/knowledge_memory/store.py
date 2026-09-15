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
import tempfile
from typing import Optional


class MemoryStore:
    """append-only 知识存储。

    path=None 时仅内存保存（实验/测试用）；给定 path 时持久化到 JSON 文件。
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._records: dict[str, dict] = {}
        self._next_seq = 1
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
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            # 损坏的存储文件 → 从空库开始（不覆盖原文件，直到下一次保存）
            self._records = {}
            self._next_seq = 1

    def _save(self) -> None:
        if not self.path:
            return
        payload = {
            "version": 1,
            "next_seq": self._next_seq,
            "records": self._records,
        }
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)

    # ---- 追加（唯一写操作）----
    def add(self, record: dict) -> str:
        """追加一条记录，返回分配的 record_id。不修改任何已有记录。"""
        record_id = f"rec{self._next_seq:08d}"
        self._next_seq += 1
        stored = dict(record)
        stored["record_id"] = record_id
        self._records[record_id] = stored
        self._save()
        return record_id

    # ---- 只读操作 ----
    def get(self, record_id: str) -> Optional[dict]:
        return self._records.get(record_id)

    def all(self) -> list[dict]:
        return list(self._records.values())

    def count(self) -> int:
        return len(self._records)

    def statistics(self) -> dict:
        """全库累计统计（按 strategy_id 聚合的成功/失败与平均延迟）。"""
        stats: dict[str, dict] = {}
        for rec in self._records.values():
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
