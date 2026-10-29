"""Agent 间消息：显式的请求/响应信封。

进程内同步通信即可满足当前系统（单进程、单查询内闭环）；消息结构保持
可序列化，便于落盘追踪，也便于未来替换为进程外总线。

字段语义：
    sender / recipient   发送方与接收方 Agent 名
    intent               意图（如 "recall" / "redecide" / "judge"）
    payload              结构化载荷
    correlation_id       一次请求-响应的关联键（响应回填同一 id）
    in_reply_to          响应消息指向的请求 id
    step                 发起方当前步数（用于追踪）
    meta                 附加元数据（不参与业务语义）
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentMessage:
    sender: str
    recipient: str
    intent: str
    payload: dict = field(default_factory=dict)
    correlation_id: str = ""
    in_reply_to: Optional[str] = None
    step: int = 0
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "sender": self.sender,
            "recipient": self.recipient,
            "intent": self.intent,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
            "in_reply_to": self.in_reply_to,
            "step": self.step,
            "meta": self.meta,
        }


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def reply_to(request: AgentMessage, sender: str, payload: dict,
             intent: str = "reply") -> AgentMessage:
    """构造对请求的响应消息（关联键保持可追踪）。"""
    return AgentMessage(
        sender=sender,
        recipient=request.sender,
        intent=intent,
        payload=payload,
        correlation_id=request.correlation_id or new_correlation_id(),
        in_reply_to=request.correlation_id or None,
        step=request.step,
    )
