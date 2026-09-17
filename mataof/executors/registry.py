"""执行器工厂：按配置构造执行器。"""

from __future__ import annotations

from typing import Any

from mataof.executors.base import Executor, NullExecutor
from mataof.executors.file import FileExecutor
from mataof.executors.iotdb import IoTDBExecutor


def build_executor(executor_spec: Any) -> Executor:
    """按配置构造执行器。

    executor_spec 支持：
    - None / "null"                    → NullExecutor（安全默认）
    - "file" 或 {"type": "file", "results_file": ...} → FileExecutor
    - "iotdb" 或 {"type": "iotdb", "host":..., "port":..., "user":...,
      "password":..., "measure_baseline": true} → IoTDBExecutor（真实执行）
    - 已实现 Executor 协议的对象       → 原样返回（对接真实环境的入口）
    """
    if executor_spec is None or executor_spec == "null" or executor_spec == {"type": "null"}:
        return NullExecutor()
    if executor_spec == "file":
        return FileExecutor("execution_results.json")
    if isinstance(executor_spec, dict) and executor_spec.get("type") == "file":
        return FileExecutor(str(executor_spec.get("results_file") or "execution_results.json"))
    if executor_spec == "iotdb" or (isinstance(executor_spec, dict)
                                    and executor_spec.get("type") == "iotdb"):
        spec = executor_spec if isinstance(executor_spec, dict) else {}
        return IoTDBExecutor(
            host=str(spec.get("host") or "127.0.0.1"),
            port=int(spec.get("port") or 6667),
            user=str(spec.get("user") or "root"),
            password=str(spec.get("password") or "root"),
            fetch_size=int(spec.get("fetch_size") or 1024),
            measure_baseline=bool(spec.get("measure_baseline", True)),
        )
    if isinstance(executor_spec, dict) and executor_spec.get("type") == "null":
        return NullExecutor()
    if hasattr(executor_spec, "execute"):
        return executor_spec   # 自定义执行器（对接真实环境）
    raise ValueError(
        "无法识别的 executor 配置；支持 \"null\"、\"file\"（需 results_file）、"
        "\"iotdb\"（host/port/user/password/measure_baseline）或实现 Executor 协议的对象"
    )
