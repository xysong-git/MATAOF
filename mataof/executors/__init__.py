"""执行层抽象：MATAOF 与真实执行环境之间的正式对接点。

Executor 协议定义执行层的唯一职责：执行决策 Agent 选择的策略并采集真实指标。
所有执行结果必须来自真实执行环境——执行器不得编造指标。

内置执行器：
- NullExecutor：不执行任何查询（返回 unknown 状态），用于只跑分析/决策的场合；
- FileExecutor：从预先采集的执行结果文件（真实数据）中按 query_id 取结果，
  用于离线实验回放。

对接真实环境：实现 Executor 协议（execute 方法），返回事实性执行结果即可。
"""

from mataof.executors.base import Executor, ExecutionResult, NullExecutor
from mataof.executors.file import FileExecutor
from mataof.executors.iotdb import IoTDBExecutor, classify_execution_error
from mataof.executors.registry import build_executor

__all__ = ["Executor", "ExecutionResult", "NullExecutor", "FileExecutor",
           "IoTDBExecutor", "classify_execution_error", "build_executor"]
