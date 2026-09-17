"""FileExecutor：从预先采集的执行结果文件回放真实执行数据（离线实验用）。

结果文件格式（JSON）：
{
  "results": [
    {
      "query_id": "q1",                    # 与运行时的 query_id 对应
      "execution_status": "success",       # success/timeout/failed/cancelled/unknown
      "failure_reason": null,
      "metrics": {"response_time_ms": 12.3, ...},      # 真实采集的指标
      "baseline_metrics": {...},                        # 可选
      "timestamp": 1700000000000                        # 可选：真实执行时间
    }
  ]
}

按 query_id 查找；找不到 → execution_status=unknown（不编造任何指标）。
"""

from __future__ import annotations

import json
import os
from typing import Optional

from mataof.executors.base import ExecutionResult, Executor


class FileExecutor:
    """从文件读取预先采集的执行结果（真实数据）。"""

    name = "file"

    def __init__(self, results_file: str):
        self.results_file = results_file
        self._results: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.results_file):
            self._load_error = f"执行结果文件不存在：{self.results_file}"
            return
        try:
            with open(self.results_file, encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("results") if isinstance(data, dict) else None
            if not isinstance(items, list):
                self._load_error = "执行结果文件格式错误（需要 {\"results\": [...]}）"
                return
            for item in items:
                if isinstance(item, dict) and item.get("query_id"):
                    self._results[str(item["query_id"])] = item
            self._load_error = None
        except (json.JSONDecodeError, OSError) as exc:
            self._load_error = f"执行结果文件读取失败：{exc}"

    def execute(self, query: str, query_id: str, strategy: dict) -> ExecutionResult:
        if getattr(self, "_load_error", None):
            return ExecutionResult(execution_status="unknown")
        item = self._results.get(str(query_id))
        if item is None:
            # 没有该查询的真实执行数据 → unknown（不编造）
            return ExecutionResult(execution_status="unknown")
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        baseline = item.get("baseline_metrics")
        ts = item.get("timestamp")
        return ExecutionResult(
            execution_status=str(item.get("execution_status") or "unknown"),
            failure_reason=item.get("failure_reason"),
            metrics=metrics,
            baseline_metrics=baseline if isinstance(baseline, dict) else None,
            timestamp=int(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None,
        )
