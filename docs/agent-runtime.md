# Agent 运行时与四 Agent 闭环

> 本文说明 MATAOF 中"智能体"的运行机制：一个最小但完整的控制环（工具 → 行动 →
> 观察 → 再决策 → 终止），以及四个 Agent 如何各自用观察驱动下一步行动。
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
| `AgentMessage` / `AgentBus` | Agent 间请求-响应消息与 trace；主链路按查询构建总线，四 Agent 端点经总线调用（见 §8） |

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

## 3. 四个 Agent 的运行语义

四个 Agent 共用同一个运行时；差别在于**各自观察什么、下一步能做什么**。确定性
函数（`analyze` / `decide` / `monitor` / `retrieve`）全部保留为工具或快路径，
Agent 模式不复制、不篡改其事实输出。

| Agent | 目标 | 工具 | 观察驱动的下一步 | 终止原因示例 |
|---|---|---|---|---|
| Query Analysis | 产出可信的查询分析 | `analyze_deterministic`、`llm_annotate` | 确定性结果中存在证据缺口才调用 LLM | `analyzed` / `llm_annotated` / `analysis_failed` |
| Optimization Decision | 选一个可接受执行的计划 | `request_evidence`（OD→KM）、`decide()`（确定性步骤）、`execute_decision` | 初始证据为空 → 向 KM 请求补充；实测改善 → 接受；degraded/failed → 排除计划重规划 | `accepted:*` / `max_attempts` / `no_alternative` |
| Execution Monitoring | 把执行事实变成结构化反馈 | `normalize_facts`、`compare_baseline`、`assess_effect`、`detect_anomalies`、`llm_annotate` | 无 baseline/无指标 → 跳过比较；真实异常或 failed/timeout → LLM 解读（degraded 本身不触发） | `monitored` / `normalize_failed` |
| Knowledge Memory | 检索足够且可信的历史证据 | `retrieve_round`、`llm_summarize` | 已列出的强匹配不足且仍有未展开记录 → 扩大 top-K | `sufficient` / `no_match` / `exhausted` / `max_rounds` / `cap` |

## 4. Query Analysis Agent：缺口驱动的 LLM 标注

```
Act(analyze_deterministic)          # 确定性解析 + 特征 + 置信度 + 向量
   │
   ├─ Observation: {analysis, signals}
   │      signals.gaps ∈ {parse_error, query_type_unknown,
   │                      low_confidence, ambiguous_filter_types}
   │
   ├─ llm_policy=on_gap 且无缺口 ────────────→ Finish("analyzed")
   ├─ llm 不可用 / policy=never ─────────────→ Finish("analyzed")
   └─ 需要解读 → Act(llm_annotate)
            ├─ 成功 → Finish("llm_annotated")
            └─ 失败 → 保留确定性结果 + note → Finish("llm_failed")
```

- **确定性字段是权威**：`query_type`、`time_features`、`feature_vector`、
  `analysis_confidence` 等不受 LLM 影响；LLM 只填 `llm_analysis` 附加节。
- **缺口信号来自事实**：解析失败、类型 unknown、置信度低于阈值、tag/attribute
  歧义条件；不是"每查都调 LLM"。
- **失败可观察**：分析工具异常 → `analysis_failed`，输出保守的 unknown 模板，
  不伪造特征。

配置：

```json
"query_analysis": {
  "llm_policy": "on_gap",
  "confidence_threshold": 0.5,
  "deadline_ms": 10000
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `llm_policy` | `on_gap` | `on_gap`：仅有缺口时调用；`always`：总是；`never`：从不 |
| `confidence_threshold` | `0.5` | 低于该置信度视为缺口（钳制在 0~1） |
| `deadline_ms` | `10000` | 控制环墙钟预算 |

## 5. Execution Monitoring Agent：事实流水线

```
Act(normalize_facts)                # 状态/指标/阈值校验，无法获得 → unknown/null
   │
   ├─ 有实测指标且有 baseline → Act(compare_baseline)
   ├─ 无 baseline → 跳过比较（no_baseline），仍继续评估
   └─ 有 baseline 但无有效指标 → 跳过比较（no_metrics）
   │
Act(assess_effect)                  # improved / degraded / unchanged /
   │                                # failed / insufficient_evidence
Act(detect_anomalies)               # 只记录，不改策略
   │
   └─ 异常数 > 0 或状态 ∈ {failed, timeout}
          ├─ 是 → Act(llm_annotate)（默认 policy=on_anomaly）
          └─ 否 → Finish("monitored")，记录 skip reason
                 （degraded 本身不触发；on_anomaly_or_degraded 消融时才触发）
```

- **跳过比较也是决策**：没有 baseline 或没有实测指标时，比较没有事实基础；
  Agent 记录 `comparison_skip_reason` 后继续，而不是伪造 `available=true`。
- **LLM 只在值得解读时调用**；`external_llm=true` 时由 Runner 统一管理解读，
  Agent 不重复调用（`runner_manages_llm`）。
- **事实采集失败**：状态 `unknown`、指标全 `null`、note 说明原因，不猜测。

配置：

```json
"execution_monitoring": {
  "llm_policy": "on_anomaly",
  "deadline_ms": 5000
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `llm_policy` | `on_anomaly` | 或 `on_anomaly_or_degraded` / `always` / `never`；`on_anomaly` 仅真实异常或 failed/timeout |
| `deadline_ms` | `5000` | 控制环墙钟预算 |

## 6. Knowledge Memory Agent：自适应 top-K 检索

```
Act(retrieve_round, max_records=K)  # 三层匹配 + 匹配计数 + 截断信息
   │
   ├─ Observation: listed / total_matched / truncated / match_level
   │
   ├─ 已列出强匹配 ≥ min_strong ─────────────→ Finish("sufficient")
   ├─ total_matched = 0 ─────────────────────→ Finish("no_match")
   ├─ 无截断（全部匹配已列出）───────────────→ Finish("exhausted")
   ├─ 轮数 ≥ max_rounds ─────────────────────→ Finish("max_rounds")
   ├─ K 已达 cap ────────────────────────────→ Finish("cap")
   └─ 强证据不足且仍有未展开记录 → K = min(cap, K×expand_factor)
                                        └─ Act(retrieve_round, 新 K)
```

- **扩 K 的条件是"观察到的证据不足"**：只统计**实际返回给决策 Agent** 的
  `strong_match` 条数——全局有很多强匹配但 top-K 截断时，不能宣称证据充分。
- **有界**：轮数 `1~4`、K 上限 `max_records_cap`（缺省 = 首轮 K × 倍数或 200）、
  墙钟 `deadline_ms`；任一触发即终止。
- **LLM 解读只做一次**：每轮检索 `llm_summary=False`，终止后如需解读才调用
  `llm_summarize`；无匹配/未启用/被禁用时记录 skip reason。
- **只读**：检索不修改 append-only 知识库；入库仍由 `update()` 独立完成。
- **检索失败诚实兜底**：`retrieval_ok=false`、匹配计数为 `null`、证据列表为空，
  不把失败写成"0 条匹配"。

配置：

```json
"knowledge_memory": {
  "min_strong": 1,
  "max_rounds": 2,
  "expand_factor": 2,
  "deadline_ms": 2000
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `min_strong` | `1` | 判定证据充分所需的已列出强匹配条数 |
| `max_rounds` | `2` | 最多检索轮数（含首轮），钳制在 1~4 |
| `expand_factor` | `2` | 每轮 top-K 扩大倍数，钳制在 2~8 |
| `max_records_cap` | 首轮 K×倍数或 200 | top-K 上限 |
| `deadline_ms` | `2000` | 控制环墙钟预算 |

## 7. Optimization Decision Agent：补充证据 → 决策 → 执行 → 观察 → 退化重规划

```
初始历史证据为空？（recall_on_empty_evidence=true）
   │
   ├─ 是 → Act("request_evidence")          # OD → KM 消息
   │        └─ Observation：补充记录 / 错误
   │             └─ 按 record_id 去重合并 → 继续决策（失败只记录，不伪造证据）
   └─ 否 → 直接决策
   │
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
- **补充证据也只请求一次**：仅当初始 `historical_records` 为空时向 KM 发
  `recall`（top-K 更宽），失败或仍为空则照常决策；合并时按 `record_id` 去重，
  不重复计入同一记录。是否发生、增加了几条写入 `agent.evidence_recall`。
- **最终产物是最后一次尝试的事实**：`decision`/`execution`/`monitoring` 三者一致，
  不会出现"决策是 A、执行是 B"。

配置（旧键 `agent` 仍兼容，等价于 `agents.optimization_decision`）：

```json
"optimization_decision": {
  "max_attempts": 2,
  "deadline_ms": 30000,
  "replan_on": ["degraded", "failed"],
  "recall_on_empty_evidence": true,
  "recall_max_records": null
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `max_attempts` | `2` | 最多执行次数（含首次），钳制在 1~4 |
| `deadline_ms` | `30000` | 闭环墙钟预算；超时终止 |
| `replan_on` | `["degraded","failed"]` | 触发重规划的监控结论 |
| `recall_on_empty_evidence` | `true` | 初始证据为空时是否向 KM 请求补充检索 |
| `recall_max_records` | `null` | 补充检索 top-K；`null` = `max(200, 首轮×2)`，且至少比首轮大 1，钳制 1~5000 |

## 8. 主链路消息总线（AgentBus）

任一 Agent 启用时，Runner 为**每条查询**构建一个进程内同步总线；四个 Agent 以
端点（recipient）注册，主流程通过 `AgentMessage(sender, recipient, intent, payload)`
请求并拿到响应。全部关闭时不创建总线，走旧的直接调用路径。

### 8.1 端点与意图

| 接收方 | intent | 处理实现 | 返回 |
|---|---|---|---|
| `query_analysis` | `analyze` | QA Agent（启用）或 `analyze()` | `analysis`、`agent` |
| `knowledge_memory` | `retrieve` | KM Agent（启用）或 `retrieve()` | `retrieval`、`agent` |
| `knowledge_memory` | `recall` | 同上，top-K 由消息 `max_records` 指定 | 同上 |
| `knowledge_memory` | `remember` | `km.update(..., use_llm=False)` | `update` |
| `execution_monitoring` | `monitor` | EM Agent（启用）或 `monitor()` | `monitoring`、`agent` |

端点内部按配置选择 Agent 或确定性实现，因此单 Agent 消融不改变消息拓扑。
查询上下文（query/analysis/feedback_input 等）由 Runner 闭包持有，消息 payload
只带小型参数，避免大对象在 trace 中重复。

### 8.2 一次冷启动查询的消息流

```
runner → query_analysis    analyze
runner → knowledge_memory  retrieve
od     → knowledge_memory  recall      # 初始证据为空
od     → execution_monitoring monitor  # 执行工具的观察步骤
runner → knowledge_memory  remember
```

第二次相同查询命中历史后不再发 `recall`。OD 以 `optimization_decision` 身份
发送消息，执行/监控请求的 sender 因此区分于 Runner 自己的调用。

### 8.3 失败语义

- 处理器异常不会丢失请求：总线返回 `intent="error"` 的响应，错误文本写入
  `reply.meta["error"]` 与 `trace["bus"][i]["error"]`；
- 调用方（Runner）收到错误响应后**回退到直接实现**，查询继续，真实异常保留在
  trace；
- 未注册接收方 → `KeyError`（显式失败，不静默丢弃）；主链路总会注册三个端点，
  该分支只影响未来的自定义拓扑。

### 8.4 追踪

- `trace["bus"]` 按顺序记录每次请求/响应；`_brief` 对写入 trace 的副本做摘要
  （嵌套深度 >6 截断、列表最多 10 项、字符串最多 400 字符），并保留
  `correlation_id` / `in_reply_to` 以便关联；
- `run_log.jsonl` 增加 `bus_messages`（消息数）与 `bus_errors`（错误响应数）；
- 总线只在单查询内存在，不跨查询共享状态；知识库仍由 `KnowledgeMemory` 持久化。

## 9. 配置：一个 `agents` 节

```json
"agents": {
  "enabled": false,
  "llm_shared": false,
  "control": "agent",
  "query_analysis": {"llm_policy": "on_gap", "confidence_threshold": 0.5,
                     "deadline_ms": 10000},
  "optimization_decision": {"max_attempts": 2, "deadline_ms": 30000,
                            "replan_on": ["degraded", "failed"],
                            "recall_on_empty_evidence": true,
                            "recall_max_records": null},
  "execution_monitoring": {"llm_policy": "on_anomaly",
                           "deadline_ms": 5000},
  "knowledge_memory": {"min_strong": 1, "max_rounds": 2,
                       "expand_factor": 2, "deadline_ms": 2000}
}
```

解析规则（`resolve_agents_config`）：

1. `agents.enabled` 是全局默认值；**某个 Agent 自己声明 `enabled` 时优先生效**
   （便于单 Agent 消融）；
2. 旧配置 `agent` 等价于 `agents.optimization_decision`，两者合并、新键优先；
3. 未给出的字段由各 Agent 的 `_normalize_agent_config` 补默认值并钳制，
   非法值回退默认，不抛异常；
4. 默认全部关闭：关闭时 Runner 走原有确定性路径，输出与旧版本一致；
5. `agents.llm_shared`（默认 `false`）控制 LLM 是否常驻共享：
   - `false`：旧语义——`decision_mode="llm_primary"` 下 LLM 只给 OD，
     QA/EM/KM 不接收 LLM；
   - `true`：同一 LLM 客户端按各 Agent 的 `llm_policy` 共享给 QA/EM/KM。
     QA 仅在 QA Agent 启用且策略触发（`on_gap`/`always`）时调用；EM 监控解读
     仅在真实异常或 failed/timeout 时调用（默认 `llm_policy=on_anomaly`，
     degraded 由确定性链路记录，不逐条触发）；KM 经验总结按查询形状收口
     （`experience_key`：同形状 + 策略 + 数据规模，不含精确 time_span），
     同一形状首次执行调用 1 次、后续执行复用。未触发时写入
     `available=false` + 原因（诚实占位）。主实验配置（OD Agent +
     `llm_primary`）的调用预算：OD 决策 1 次/查询，QA 可选 +1，监控解读
     可选 +1，新形状首条 KM 经验 +1——**稳态正常查询 1 次、单条最多 3 次**。
     `on_anomaly_or_degraded` 保留为消融策略。
6. `agents.control`（默认 `agent`）切换控制流，用于 2×2 对照隔离“控制流”变量：
   - `agent`：观察驱动闭环——OD 在 degraded/failed 后 replan、空证据时 recall，
     KM 可按观察扩 top-K；
   - `pipeline`：固定流水线——单次决策/执行、固定 top-K 检索，不做
     replan/recall/自适应扩 K；QA/EM/KM 组件与 LLM 调用点保持不变。

Python API：

```python
from mataof.agents.query_analysis import run_agent as run_qa
from mataof.agents.execution_monitoring import run_agent as run_em
from mataof.agents.knowledge_memory import KnowledgeMemoryAgent

qa = run_qa(query, query_id="q1", llm=llm, agent_config={"llm_policy": "on_gap"})
em = run_em(monitoring_input, llm=llm, agent_config={"enabled": True})
km = KnowledgeMemoryAgent(store_path=..., llm=llm)
km_out = km.run_retrieve(analysis, max_records=100,
                         agent_config={"min_strong": 2, "max_rounds": 3})
```

## 10. 追踪与观测

启用 Agent 后，Runner 在 trace 中写入：

- `trace["agents"]`：按名称索引的 Agent 元数据，只包含**本次实际启用**的 Agent；
  每个条目至少含 `enabled`、`runtime`、`policy`、`steps`、`terminated_by`、
  `elapsed_ms`、`trace`（逐步工具/参数/观察/耗时）。
- `trace["agent"]`：**保留为 Optimization Decision Agent 的元数据**，向后兼容旧
  的 trace 消费方（与 `trace["agents"]["optimization_decision"]` 相同对象）。
- 各 Agent 的专有字段：
  - QA：`llm_policy`、`confidence_threshold`、`llm_used`、`llm_skip_reason`、
    `gap_signals`、`gap_details`、`analysis_failed`；
  - OD：`max_attempts`、`replan_on`、`attempts`、`timings`、
    `final_monitoring_ms`、`evidence_recall`（`attempted`/`ok`/`records_added`/
    `retrieval`/`agent`/`error`；`agent` 是补充检索那次 KM 运行的摘要）；
  - EM：`llm_policy`、`external_llm`、`llm_used`、`llm_skip_reason`、
    `comparison_performed`、`comparison_skip_reason`、`assessment`、
    `anomaly_count`；
  - KM：`min_strong`、`max_rounds`、`expand_factor`、`max_records_cap`、
    `initial_max_records`、`final_max_records`、`rounds`、`topk_rounds`、
    `retrieval_termination`、`retrieval_ms`、`llm_summary_used`。
- `trace["bus"]`：本次查询的 Agent 间消息（请求/响应摘要、关联键、错误）；
  未启用任何 Agent 时为 `[]`（见 §8.4）。
- `trace["overhead"]`：OD Agent 模式下分项来自闭环内累计计时；其余路径仍为
  Runner 墙钟分项。
- `run_log.jsonl`：
  - 旧字段 `agent_steps`、`agent_attempts`、`agent_terminated_by` 继续表示 OD；
  - 新字段 `agents_enabled`（本次启用的 Agent 名列表）与
    `agent_steps_total`（四 Agent 步数之和，含 OD 补充检索那次 KM 运行的步数）；
    未启用时分别为 `[]` 与 `0`；
  - `bus_messages`（本次查询的消息数）与 `bus_errors`（错误响应数）；
    无总线时均为 `0`；
  - `control_flow`（`pipeline` / `agent` / `legacy` / `fixed_strategy`）、
    `llm_enabled`、`llm_shared`、`decision_mode`：每条查询自描述实验口径；
  - 行为剖面：`agent_action_hash`（动作序列哈希，用于统计不同控制流数量）、
    `agent_action_count` / `agent_action_counts`、`agent_steps_by_agent`、
    `agent_terminations`、`agent_triggers`（replan/recall/QA LLM/EM LLM/
    KM LLM/自适应扩 K/OD LLM 决策）、`bus_intents`（请求 intent 分布）。
    这些字段由 `scripts/agent_profile.py` 汇总为 Agent Activation Profile。

一次四 Agent 全启用的记录（节选）：

```json
{
  "agents": {
    "query_analysis": {"policy": "gap_aware_llm", "steps": 1,
                       "terminated_by": "analyzed", "gap_signals": []},
    "knowledge_memory": {"policy": "adaptive_topk", "steps": 1,
                          "retrieval_termination": "no_match"},
    "optimization_decision": {"policy": "deterministic_replan", "steps": 2,
                               "terminated_by": "accepted:insufficient_evidence",
                               "evidence_recall": {"attempted": true, "ok": true,
                                                   "records_added": 0,
                                                   "agent": {"steps": 1,
                                                             "terminated_by": "no_match"},
                                                   "error": null}},
    "execution_monitoring": {"policy": "facts_pipeline", "steps": 3,
                              "terminated_by": "monitored",
                              "comparison_performed": false,
                              "comparison_skip_reason": "no_baseline"}
  }
}
```

## 11. 确定性、安全与消融

- **默认关闭**：`agents.enabled=false` 时四个确定性入口与旧路径完全一致。
  代码与模板的默认值保持关闭；`scripts/gen_configs.py` 生成 2×2 对照矩阵：
  - `config.*.pipeline.json`（P0）：`enabled=true`、`control="pipeline"`、无 LLM；
  - `config.*.pipeline-llm.json`（P1）：`enabled=true`、`control="pipeline"`、
    `llm_shared=true`；
  - `config.*.json`（A0）：`enabled=true`、`control="agent"`、无 LLM；
  - `config.*.llm.json`（A1，主实验）：`enabled=true`、`control="agent"`、
    `llm_shared=true`；
  - `config.*.fixed.json`：`enabled=false`（固定策略对照，跳过决策/检索）。
  四种启用配置都复用 QA/EM/KM/OD 组件与同一 LLM 调用点，差异只在控制流
  （pipeline vs agent）与 LLM（off/on）。
- **确定性优先**：所有 Agent 的第一步都是确定性工具；LLM 只做附加解读或
  在既有候选中选择，失败自动回退。
- **有界**：每个 Agent 有 `max_steps` 与 `deadline_ms`；KM 额外有轮数与
  top-K 上限；OD 有尝试次数上限。
- **失败可解释**：工具异常被转为失败观察；真实异常类型与文本保留在
  `error` / `observation.error` 中。
- **可消融**：
  - `agents.control="pipeline"`：固定流水线（单次决策/执行、固定 top-K，
    无 replan/recall），与 `control="agent"` 构成控制流 2×2 对照；
  - `agents.enabled=false`：纯确定性路径（旧行为）；
  - `agents.llm_shared=false`：LLM 只给 OD（`llm_primary` 下 1 次/查询），
    保留“仅 OD 用 LLM”消融路径；
  - 全局开启、单 Agent `enabled=false`：只保留其余 Agent；
  - QA：`llm_policy=never` → 只做确定性分析；
  - EM：`llm_policy=never` → 只采集事实、不做 LLM 解读（执行后调用不发起）；
    `llm_policy=on_anomaly_or_degraded` → degraded 也触发解读（消融）；
  - KM：`max_rounds=1` → 固定 top-K（等价旧检索）；`min_strong=0` →
    首轮即判定充分；
  - OD：`max_attempts=1` → 只执行一次但保留 Agent trace；
  - OD：`recall_on_empty_evidence=false` → 不发补充证据消息（冷启动步数 2→1）；
  - 旧 `agent.enabled=true` → 仅启用 OD，其余三个保持确定性路径。

## 12. 当前不包含（下一步）

- **主动探测（probe）**：Agent 目前只能在"执行当前决策"上行动，不能先做低成本
  探测再决定。`Act` 已支持任意注册工具，接入探测工具即可。
- **消息驱动的边界**：总线是同步请求-响应，由 Runner 或 OD 发起；QA/EM/KM
  不会主动向其他 Agent 发消息，也没有并发协商、广播或消息队列。当前目标是
  "协作路径显式可追踪"，不是自由多 Agent 对话。
- **LLM 策略**：LLM 只参与 OD 的首次策略选择与 QA/EM/KM 的附加解读；
  各 Agent 的策略函数（何时扩 K、何时重规划）仍是确定性的，`policy` 可注入，
  后续可替换。
- **尝试级知识沉淀**：OD 当前只把最终一次执行写入知识库；失败/退化尝试保留在
  trace，尚未作为负证据入库。
- **EM 主动补采**：监控目前只消费调用方提供的指标，不会主动向监控接口追问
  缺失字段。

## 13. 测试

```bash
pytest tests/test_runtime.py tests/test_qa_agent_loop.py \
       tests/test_od_agent_loop.py tests/test_em_agent_loop.py \
       tests/test_km_agent_loop.py tests/test_runner_agents.py \
       tests/test_agent_bus_pipeline.py tests/test_pipeline_control.py -v
```

全量回归：`pytest tests/`（476 例通过 + 1 跳过）。新增
`tests/test_pipeline_control.py` 覆盖 pipeline 单次执行/不重规划、agent 对照
重规划、P1/A1 相同 LLM 调用点、pipeline 固定 top-K、run_log 行为剖面字段与
`scripts/agent_profile.py` 冒烟。

覆盖：工具参数校验与异常包装、Act→观察→Finish、步数/时间预算、未注册工具、
消息总线请求-响应与 trace 摘要；QA 确定性等价/缺口触发/策略开关/工具失败；
EM 事实等价/跳过比较/LLM 触发条件/外部 LLM/采集失败兜底；KM 扩 K 条件/四类
终止/LLM 单次调用/检索失败兜底/只读性；OD 退化重规划/尝试预算/baseline 不重试/
补充证据；Runner 四 Agent 配置合并、trace 与 run_log 字段、默认关闭与单 Agent
消融；主链路消息流转、OD→KM recall、recall 开关、端点异常回退与总线计数。
