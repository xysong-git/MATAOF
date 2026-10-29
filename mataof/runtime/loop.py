"""Agent 有界控制环：目标 + 工具 + 策略 + 终止条件。

一个 Agent 的最小定义（与本仓库的"确定性优先"原则兼容）：

    Agent = 目标（Goal） + 工具（ToolRegistry） + 策略（policy） + 预算
    循环：观察当前上下文 → 策略选择动作（Act / Finish / Abort）
          → 调用工具 → 记录观察 → 进入下一轮

与流水线的区别：下一步做什么由策略函数根据**观察结果**决定，而不是由外部
写死调用顺序；终止权在 Agent 手里（Finish / Abort）。

安全默认：
- 步数上限（goal.max_steps）与时间上限（goal.deadline_ms）双保险；
- 工具异常不穿透：记录为 ok=False 的 Observation，交给策略处理；
- 未注册的工具不执行；
- 每一步的 action / reason / observation / 耗时全部写入 trace。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from mataof.runtime.tools import ToolNotFound, ToolRegistry


# ---------------------------------------------------------------------------
# 动作与目标
# ---------------------------------------------------------------------------

@dataclass
class Goal:
    """Agent 本轮要达成的目标与预算。"""

    name: str
    max_steps: int = 4
    deadline_ms: Optional[float] = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        try:
            self.max_steps = max(1, int(self.max_steps))
        except (TypeError, ValueError):
            self.max_steps = 1
        if self.deadline_ms is not None:
            try:
                self.deadline_ms = max(0.0, float(self.deadline_ms))
            except (TypeError, ValueError):
                self.deadline_ms = None


@dataclass
class Act:
    """请求调用一个工具。"""

    tool: str
    args: dict = field(default_factory=dict)
    reason: str = ""


@dataclass
class Finish:
    """正常终止：Agent 认为目标已达成/已无法进一步改进。"""

    result: Any = None
    reason: str = "finish"


@dataclass
class Abort:
    """异常终止：预算耗尽、缺少必要能力或输入无效。"""

    result: Any = None
    reason: str = "abort"


# ---------------------------------------------------------------------------
# 观察与上下文
# ---------------------------------------------------------------------------

def _brief(value: Any, depth: int = 0) -> Any:
    """trace 用的轻量摘要：限制嵌套层数/键数/字符串长度。"""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        items = list(value.items())[:20]
        return {str(k): _brief(v, depth + 1) for k, v in items}
    if isinstance(value, (list, tuple)):
        return [_brief(v, depth + 1) for v in list(value)[:10]]
    if isinstance(value, str) and len(value) > 400:
        return value[:400] + "..."
    return value


@dataclass
class Observation:
    """一次工具调用的结果（成功或失败都算观察）。"""

    tool: str
    step: int
    ok: bool
    value: Any = None
    error: Optional[str] = None
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        out = {
            "tool": self.tool,
            "step": self.step,
            "ok": self.ok,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }
        if self.error is not None:
            out["error"] = self.error
        out["value"] = _brief(self.value)
        return out


@dataclass
class AgentContext:
    """Agent 的短期工作记忆（跨步骤共享；由策略自行约定 data 的键）。"""

    data: dict = field(default_factory=dict)
    steps: int = 0
    observations: list = field(default_factory=list)   # [Observation]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def update(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    @property
    def last_observation(self) -> Optional[Observation]:
        return self.observations[-1] if self.observations else None

    def record(self, obs: Observation) -> None:
        self.observations.append(obs)
        self.steps = obs.step


@dataclass
class AgentRunResult:
    """一次 Agent 循环的完整结果（含可追溯的逐步轨迹）。"""

    result: Any
    trace: list
    steps: int
    terminated_by: str
    elapsed_ms: float
    context: AgentContext

    def to_dict(self) -> dict:
        return {
            "result": self.result,
            "trace": self.trace,
            "steps": self.steps,
            "terminated_by": self.terminated_by,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


# ---------------------------------------------------------------------------
# 控制环
# ---------------------------------------------------------------------------

Policy = Callable[[AgentContext, ToolRegistry], Any]


class AgentRuntime:
    """执行一个 Agent 的有界控制环。

    用法：
        runtime = AgentRuntime("optimizer", tools, policy)
        result = runtime.run(Goal("optimize_query", max_steps=5, deadline_ms=30000))
    """

    def __init__(self, name: str, tools: ToolRegistry, policy: Policy, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not callable(policy):
            raise TypeError("policy 必须可调用")
        if not isinstance(tools, ToolRegistry):
            raise TypeError("tools 必须是 ToolRegistry")
        self.name = name
        self.tools = tools
        self.policy = policy
        self._clock = clock

    def run(self, goal: Goal, context: Optional[AgentContext] = None) -> AgentRunResult:
        ctx = context if context is not None else AgentContext()
        trace: list = []
        start = self._clock()

        for step in range(1, goal.max_steps + 1):
            if goal.deadline_ms is not None:
                elapsed_ms = (self._clock() - start) * 1000.0
                if elapsed_ms >= goal.deadline_ms:
                    return self._result(None, trace, ctx, "deadline", start)

            action = self.policy(ctx, self.tools)

            if isinstance(action, Finish):
                return self._result(action.result, trace, ctx,
                                    action.reason or "finish", start)
            if isinstance(action, Abort):
                return self._result(action.result, trace, ctx,
                                    action.reason or "abort", start)
            if not isinstance(action, Act):
                raise TypeError(
                    f"policy 必须返回 Act/Finish/Abort，得到 {type(action).__name__}")

            # ---- 调用工具：所有失败都变成观察，不穿透控制环 ----
            t0 = self._clock()
            if action.tool not in self.tools:
                obs = Observation(tool=action.tool, step=step, ok=False,
                                  error=f"未注册的工具：{action.tool}",
                                  elapsed_ms=0.0)
            else:
                try:
                    value = self.tools.call(action.tool, **dict(action.args or {}))
                    obs = Observation(tool=action.tool, step=step, ok=True,
                                      value=value,
                                      elapsed_ms=(self._clock() - t0) * 1000.0)
                except Exception as exc:   # ToolCallError / 工具内部异常
                    obs = Observation(tool=action.tool, step=step, ok=False,
                                      error=f"{type(exc).__name__}: {exc}",
                                      elapsed_ms=(self._clock() - t0) * 1000.0)
            ctx.record(obs)
            trace.append({
                "step": step,
                "tool": action.tool,
                "reason": action.reason,
                "args": _brief(action.args),
                "observation": obs.to_dict(),
            })

        return self._result(None, trace, ctx, "max_steps", start)

    def _result(self, result: Any, trace: list, ctx: AgentContext,
                terminated_by: str, start: float) -> AgentRunResult:
        return AgentRunResult(
            result=result, trace=trace, steps=ctx.steps,
            terminated_by=terminated_by,
            elapsed_ms=(self._clock() - start) * 1000.0,
            context=ctx,
        )
