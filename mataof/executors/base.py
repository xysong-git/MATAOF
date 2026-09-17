"""执行层协议与内置 Null 执行器。

Executor 协议：执行层负责两件事——
1. 执行决策 Agent 选出的策略（把 strategy_id / strategy_parameters 翻译成
   实际执行动作，这由对接方实现）；
2. 采集真实执行指标并按 ExecutionResult 返回。

严格限制：所有指标必须来自真实执行环境；执行器绝不编造指标。
采不到的指标传 null，Execution Monitoring Agent 会将其标记为 unknown。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass
class ExecutionResult:
    """一次执行的原始事实结果（由真实执行环境提供）。"""

    execution_status: str = "unknown"       # success/timeout/failed/cancelled/unknown
    failure_reason: Optional[str] = None    # failed 时的原因（真实错误信息）
    metrics: dict = field(default_factory=dict)        # 实测指标（采不到就不填）
    baseline_metrics: Optional[dict] = None            # baseline 执行结果（可选）
    timestamp: Optional[int] = None                    # 执行时间戳（epoch 毫秒）


class Executor(Protocol):
    """执行层协议。对接真实环境时实现此协议。"""

    name: str

    def execute(self, query: str, query_id: str, strategy: dict) -> ExecutionResult:
        """执行查询并返回事实性执行结果。

        strategy = {"strategy_id": str, "strategy_parameters": dict,
                    "equivalent_sql": str | None}

        执行约定（混合模型）：
        - strategy["equivalent_sql"] 非空时，优先执行该等价 SQL
          （由候选提供方生成，决策 Agent 仅选择）；
        - 为 null 时，执行原查询并按 strategy_id / strategy_parameters 应用
          执行级策略（如分区裁剪、聚合下推等执行层动作）。

        不得编造 metrics / baseline_metrics / timestamp。
        """
        ...


class NullExecutor:
    """不执行任何查询的执行器（安全默认）。

    用于只跑分析/决策链路、或尚未对接真实执行环境的场合。
    返回 execution_status=unknown、无任何指标——绝不编造执行结果，
    监控 Agent 会如实输出 insufficient_evidence。
    """

    name = "null"

    def execute(self, query: str, query_id: str, strategy: dict) -> ExecutionResult:
        return ExecutionResult(execution_status="unknown")
