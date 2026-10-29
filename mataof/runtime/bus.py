"""进程内同步消息总线（可追踪）。

当前用途：
- 为 Agent 间的显式请求/响应提供统一入口（如决策 Agent 请求监控 Agent
  补一次 baseline 判定）；
- 把每次消息往返写入 trace，实验时可直接观察 Agent 间交互；
- 暂不引入线程/队列：单查询闭环内同步派发，语义简单、可复现。

未注册接收方 → KeyError（显式失败，不静默丢弃请求）。
"""

from __future__ import annotations

from typing import Callable, Optional

from mataof.runtime.message import AgentMessage, new_correlation_id, reply_to

Handler = Callable[[AgentMessage], Optional[dict]]


class AgentBus:
    """同步消息总线：register(recipient, handler) / send(message)。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._trace: list[dict] = []

    # ------------------------------------------------------------------
    # 注册与查询
    # ------------------------------------------------------------------
    def register(self, recipient: str, handler: Handler) -> None:
        if not isinstance(recipient, str) or not recipient.strip():
            raise ValueError("接收方名称必须是非空字符串")
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        self._handlers[recipient] = handler

    def unregister(self, recipient: str) -> None:
        self._handlers.pop(recipient, None)

    def has(self, recipient: str) -> bool:
        return recipient in self._handlers

    @property
    def trace(self) -> list[dict]:
        return list(self._trace)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def send(self, message: AgentMessage) -> AgentMessage:
        """同步派发请求并返回响应消息（handler 返回 None → 空载荷响应）。"""
        if not isinstance(message, AgentMessage):
            raise TypeError("send 需要 AgentMessage")
        if not message.correlation_id:
            message.correlation_id = new_correlation_id()
        handler = self._handlers.get(message.recipient)
        if handler is None:
            self._trace.append({
                "sent": message.to_dict(),
                "error": f"接收方未注册：{message.recipient}",
            })
            raise KeyError(f"接收方未注册：{message.recipient}")

        try:
            payload = handler(message)
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                payload = {"result": payload}
            response = reply_to(message, sender=message.recipient, payload=payload)
            error = None
        except Exception as exc:   # 处理器失败 → 响应中携带错误，消息不丢失
            response = reply_to(message, sender=message.recipient,
                                payload={}, intent="error")
            response.meta["error"] = f"{type(exc).__name__}: {exc}"
            error = response.meta["error"]

        entry = {"request": message.to_dict(), "response": response.to_dict()}
        if error:
            entry["error"] = error
        self._trace.append(entry)
        return response
