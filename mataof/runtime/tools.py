"""工具注册表：Agent 与外部世界交互的唯一行动接口。

设计约束（与 MATAOF 的证据原则一致）：
- Agent 只能调用显式注册的工具；未注册的工具名不会被执行；
- 每个工具声明副作用等级与成本等级，便于白名单/审计/消融；
- 工具异常统一包装为 ToolCallError，由控制环记录为"失败的观察"，
  而不是让异常穿透整个 Agent 链路。

工具函数的返回值可以是任意对象；控制环只负责记录与回传，不解释语义。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# 副作用等级：none（纯计算）/ read（只读查询）/ write（写入状态）/ execute（执行查询）
SIDE_EFFECTS = ("none", "read", "write", "execute")
# 成本等级：free / low / high（供策略做预算取舍，不参与强制限制）
COST_LEVELS = ("free", "low", "high")


class ToolNotFound(KeyError):
    """工具未注册。"""


class ToolCallError(RuntimeError):
    """工具调用失败（参数缺失、执行异常等）。原始异常保存在 __cause__。"""


@dataclass(frozen=True)
class ToolSpec:
    """一个可调用工具的声明。

    parameters 采用精简的 JSON-Schema 子集：{"required": ["decision"], ...}；
    只做必填参数检查，不做类型强制（类型由工具自身校验，失败即 ToolCallError）。
    """

    name: str
    fn: Callable[..., Any]
    description: str = ""
    parameters: dict = field(default_factory=dict)
    side_effect: str = "none"
    cost: str = "low"

    def __post_init__(self):
        if self.side_effect not in SIDE_EFFECTS:
            raise ValueError(f"未知副作用等级：{self.side_effect}")
        if self.cost not in COST_LEVELS:
            raise ValueError(f"未知成本等级：{self.cost}")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters or {}),
            "side_effect": self.side_effect,
            "cost": self.cost,
        }


class ToolRegistry:
    """工具集合：注册、查询与调用。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(self, name: str, fn: Callable[..., Any], *,
                 description: str = "", parameters: Optional[dict] = None,
                 side_effect: str = "none", cost: str = "low") -> ToolSpec:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("工具名必须是非空字符串")
        if not callable(fn):
            raise ValueError(f"工具 {name} 的实现不可调用")
        spec = ToolSpec(name=name, fn=fn, description=description,
                        parameters=dict(parameters or {}),
                        side_effect=side_effect, cost=cost)
        self._tools[name] = spec
        return spec

    def register_spec(self, spec: ToolSpec) -> ToolSpec:
        if not isinstance(spec, ToolSpec):
            raise TypeError("register_spec 需要 ToolSpec")
        self._tools[spec.name] = spec
        return spec

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFound(f"工具未注册：{name}") from None

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict]:
        return [self._tools[n].to_dict() for n in sorted(self._tools)]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    def call(self, name: str, /, **kwargs: Any) -> Any:
        spec = self.get(name)
        self._validate_args(spec, kwargs)
        try:
            return spec.fn(**kwargs)
        except ToolCallError:
            raise
        except Exception as exc:   # 工具失败 → 带工具名的统一异常
            raise ToolCallError(
                f"工具 {name} 执行失败：{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _validate_args(spec: ToolSpec, kwargs: dict) -> None:
        required = (spec.parameters or {}).get("required") or []
        missing = [k for k in required if k not in kwargs]
        if missing:
            raise ToolCallError(
                f"工具 {spec.name} 缺少必填参数：{', '.join(map(str, missing))}")
        # 显式声明了 properties 时拒绝未知参数，避免拼写错误静默通过
        props = (spec.parameters or {}).get("properties")
        if isinstance(props, dict) and props:
            unknown = [k for k in kwargs if k not in props]
            if unknown:
                raise ToolCallError(
                    f"工具 {spec.name} 不接受参数：{', '.join(map(str, unknown))}")


def callable_arity(fn: Callable[..., Any]) -> Optional[int]:
    """返回函数的必填参数个数（内省失败 → None；仅用于测试/调试）。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    return sum(1 for p in sig.parameters.values()
               if p.default is inspect.Parameter.empty
               and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD,
                              p.KEYWORD_ONLY))
