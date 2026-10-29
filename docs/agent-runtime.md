# Agent 运行时与决策闭环

> 本文说明 MATAOF 中"智能体"的运行机制：一个最小但完整的控制环（工具 → 行动 →观察 → 再决策 → 终止），以及 Optimization Decision Agent 如何用真实执行反馈闭合决策环。
> 设计目标是**可运行、可观测、可消融、可复现**，而不是"看起来像 Agent"。

## 1. 为什么自建运行时

使用 LangGraph / AutoGen / CrewAI 等框架并不自动使模块成为智能体；反之，没有框架
也可以有真正的智能体。判定标准是运行语义，而不是依赖列表：

1. **目标**：一次运行有明确的成功/终止条件；
2. **工具**：Agent 通过受控接口对外部世界产生影响，而不是调用一个硬编码函数；
3. **观察**：每次行动的结果（包括异常）回到上下文，影响下一步；
4. **有界自主**：它决定下一步做什么，但步数/时间/可行动作由运行时设限；
5. **终止权**：它能主动结束（达成、无替代、预算耗尽），而不是只能被动返回。

MATAOF 的运行时只有数百行、零新增依赖，换来的是：输入输出契约完全可控、
确定性路径可原样保留、每一步可写入 trace、实验可按配置消融。框架可以以后替换，
运行语义与 trace 契约不变。

## 2. 运行时组件（`mataof/runtime/`）

| 组件 | 职责 |
|---|---|
| `ToolRegistry` | 注册/查询/调用工具；校验必填参数与声明参数；工具异常包装为 `ToolCallError`（不吞掉原因） |
| `Goal` | 目标名、`max_steps`、`deadline_ms`（两者都会被钳制到安全范围） |
| `Act` / `Finish` / `Abort` | 策略函数的三种返回：调用工具 / 正常结束 / 主动放弃 |
| `Observation` | 一次行动的事实结果：`ok`、`value`/`error`、耗时；**失败也是观察** |
| `AgentContext` | 跨步共享数据、已执行步数、观察历史；提供 `last_observation` |
| `AgentRuntime` | 有界控制环：检查预算 → 调用策略 → 执行工具 → 记录观察 → 重复，直到 Finish/Abort/预算耗尽 |
| `AgentMessage` / `AgentBus` | Agent 间请求-响应消息与 trace；用于后续多 Agent 协作，当前主链路尚未接线 |

控制环的终止原因（`terminated_by`）只有四类：策略给出的 reason、`max_steps`、
`deadline`、`no_decision`。任何工具异常都不会让循环崩溃，而是作为失败观察交给策略。

### 最小示例

```python
from mataof.runtime import Act, AgentRuntime, Finish, Goal, ToolRegistry

tools = ToolRegistry()
tools.register("fetch", lambda url: f"body:{url}", side_effect="read")

def policy(ctx, registry):
    if ctx.last_observation is None:
        return Act("fetch", {"url": "example"})
    if not ctx.last_observation.ok:
        return Finish("failed", reason="fetch_failed")
    return Finish(ctx.last_observation.value, reason="done")

result = AgentRuntime("demo", tools, policy).run(Goal("fetch_one", max_steps=3))
result.result        # "body:example"
result.terminated_by # "done"
result.trace         # 每一步的工具、参数、观察
```

## 3. Optimization Decision Agent 的闭环

`OptimizationDecisionAgent.run(...)` 在保留确定性 `decide(...)` 的前提下，把
"决策"扩展为一次有界闭环：

```
决策（确定性 / LLM 选择）
   │
   ├─ Act(tool="execute_decision", decision)
   │        │
   │        └─ Runner：执行器执行 + Execution Monitoring Agent 观察
   │
   ├─ Observation：execution_status / response_time_ms / performance_assessment
   │
   ├─ improved / unchanged ──────────────→ Finish("accepted:<assessment>")
   │
   └─ degraded / failed（且有预算）
            │
            ├─ 无计划（planning 关闭）/ baseline 失败
            │     → Finish("no_plan" / "baseline_not_retried")
            │
            └─ 排除刚测过的 plan_id → 重规划（确定性路径）
                     ├─ 无替代 → Finish("no_alternative")
                     └─ 有替代 → 再次 Act(execute_decision, 新决策)
```

关键约束：

- **不新增策略来源**：重规划仍然走同一个 `decide()`，只是在输入里加入
  `excluded_plan_ids`；候选仍来自 Agent 内置生成器。
- **baseline 永不被排除**，也不会被重试：baseline 失败说明问题不在策略选择。
- **LLM 只参与首次决策**；重规划不额外调用 LLM（避免闭环放大调用成本）。
- **所有尝试都留在 trace 里**：包括失败尝试的 `plan_id`、`performance_assessment`、
  `error`、`replan` 标记与耗时。
- **最终产物是最后一次尝试的事实**：`decision`/`execution`/`monitoring` 三者一致，
  不会出现"决策是 A、执行是 B"。

### 配置

```json
{
  "agent": {
    "enabled": true,
    "max_attempts": 2,
    "deadline_ms": 30000,
    "replan_on": ["degraded", "failed"]
  }
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 关闭时主链路与旧版本完全一致（单次决策 + 单次执行） |
| `max_attempts` | `2` | 最多执行次数（含首次），钳制在 1~4 |
| `deadline_ms` | `30000` | 闭环墙钟预算；超时终止 |
| `replan_on` | `["degraded","failed"]` | 触发重规划的监控结论 |

也可以直接调用 Python API：

```python
from mataof.agents.optimization_decision import run_agent

out = run_agent(decision_input, execute=my_execute, agent_config=agent_cfg)
# out["agent"]["attempts"] / terminated_by / trace
```

`execute(decision) -> dict` 必须返回 `execution`、`monitoring`、
`monitoring_reference`、`monitoring_input`、`execution_ms`、`monitoring_ms`。

## 4. 追踪与观测

启用 Agent 模式后：

- `trace["agent"]`：`enabled`、`runtime`、`policy`、`max_attempts`、`replan_on`、
  `attempts`、`timings`、`final_monitoring_ms`、`steps`、`terminated_by`、
  `elapsed_ms`、`trace`（逐步工具/参数/观察）。
- `trace["overhead"]`：分项来自闭环内累计计时（多次尝试的决策/执行/监控之和），
  不是 runner 墙钟近似。
- `run_log.jsonl`：新增 `agent_steps`、`agent_attempts`、`agent_terminated_by`；
  未启用时分别为 `0`、`0`、`null`。

一次典型的两尝试记录：

```json
{
  "terminated_by": "accepted:improved",
  "attempts": [
    {"attempt": 1, "plan_id": "exec_fetch_10000_df", "response_time_ms": 50.0,
     "performance_assessment": "degraded", "replan": true},
    {"attempt": 2, "plan_id": "baseline_single_df", "response_time_ms": 5.0,
     "performance_assessment": "improved", "replan": false}
  ]
}
```

## 5. 确定性、安全与消融

- **默认关闭**：`agent.enabled=false` 时 `decide()` 的输入、输出与执行路径不变。
- **确定性优先**：重规划固定走确定性决策；相同输入 + 相同执行反馈 → 相同行动序列。
- **有界**：步数上限 `2*max_attempts+2`，时间上限 `deadline_ms`；不会无限重试。
- **失败可解释**：执行器异常被转为 `failed` 观察，`error` 保留真实异常类型与文本。
- **可消融**：
  - `enabled=false`：纯单次决策（旧行为）；
  - `enabled=true, max_attempts=1`：只执行一次，但保留 Agent trace；
  - `replan_on=["failed"]`：只对执行失败重规划，不对性能退化重规划；
  - `planning.enabled=false`：关闭计划生成，退回维度策略路径（此时无 `plan_id`，
    闭环不会重规划）。

## 6. 当前不包含（下一步）

- **主动探测（probe）**：Agent 目前只能在"执行当前决策"上行动，不能先做低成本
  探测再决定。`Act` 已支持任意注册工具，接入探测工具即可。
- **多 Agent 消息接线**：`AgentBus` 已实现并有测试，但主链路仍由 Runner 顺序调用
  四个 Agent；尚未把"检索请求/监控反馈"改为消息驱动。
- **LLM 策略**：LLM 只参与策略选择，决策策略函数（何时重规划、如何排除计划）
  仍是确定性的；`policy` 是可注入函数，后续可替换。
- **尝试级知识沉淀**：当前只把最终一次执行写入知识库；失败/退化尝试保留在 trace，
  尚未作为负证据入库。

## 7. 测试

```bash
pytest tests/test_runtime.py tests/test_od_agent_loop.py -v
```

覆盖：工具参数校验与异常包装、Act→观察→Finish、步数/时间预算、未注册工具、
消息总线请求-响应、退化重规划、首次达标不重试、尝试预算、`replan_on` 过滤、
baseline 失败不重试、执行异常下控制环存活、Runner 集成与默认关闭路径。
