"""Agent 运行时骨架：工具注册、有界控制环与进程内消息总线。

与"四个确定性函数串联"的流水线不同，这里的 Agent 拥有：
    目标 + 工具 + 策略 + 预算 + 终止权
控制环的每一步都由策略根据上一轮观察决定，所有动作写入 trace。

对外入口：
    AgentRuntime / Goal / AgentContext / Act / Finish / Abort
    ToolRegistry / ToolSpec
    AgentMessage / AgentBus
"""

from mataof.runtime.bus import AgentBus
from mataof.runtime.loop import (
    Abort,
    Act,
    AgentContext,
    AgentRunResult,
    AgentRuntime,
    Finish,
    Goal,
    Observation,
)
from mataof.runtime.message import AgentMessage, new_correlation_id, reply_to
from mataof.runtime.tools import (
    ToolCallError,
    ToolNotFound,
    ToolRegistry,
    ToolSpec,
)

__all__ = [
    "Abort",
    "Act",
    "AgentBus",
    "AgentContext",
    "AgentMessage",
    "AgentRunResult",
    "AgentRuntime",
    "Finish",
    "Goal",
    "Observation",
    "ToolCallError",
    "ToolNotFound",
    "ToolRegistry",
    "ToolSpec",
    "new_correlation_id",
    "reply_to",
]
