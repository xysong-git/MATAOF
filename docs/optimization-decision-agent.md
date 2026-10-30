# Optimization Decision Agent（优化决策智能体）规格说明

版本：v1.1
实现：`mataof/agents/optimization_decision/`

## 1. 职责与边界

**核心职责**：根据 Query Analysis Agent 提供的查询特征、当前数据库状态、系统状态以及
Knowledge Memory Agent 提供的历史执行经验，在预定义的策略空间中选择当前查询更合适的
优化策略；提供原始 SQL 时，Agent 还会用内置生成器把查询编译为可执行计划（子查询 +
合并规格），再用确定性成本模型选择执行计划。Agent 负责"候选生成 + 策略选择"，
不负责查询解析和实际执行。

**严格界限**：
- **有界策略空间**：候选策略 = 输入候选 ∩ 策略目录（`catalog.py`）；计划候选来自
  固定计划族（`plans/generator.py`）；禁止自行创造目录外策略或计划；非法名称
  一律拒绝（记入 `notes`）；
- **生成与选择同属 Agent、阶段分离**：候选策略/计划由 Agent 内置 provider/generator
  生成，选择阶段只从候选中挑选；生成器只输出能编译出真实子查询与合并规格的计划，
  不做猜测式改写；不可 SQL 表达的维度（分区/chunk 裁剪、聚合位置）输出执行级参数，
  由执行层按 strategy_id 应用；
- **混合输出模型**：输出 = 策略标识 + 等价 SQL + 执行级参数。等价 SQL 由 Agent 内置
  候选生成器随候选提供；选择阶段不生成/改写 SQL；
- **上下文感知**：决策必须综合 Query Features + Database State + System State +
  Historical Knowledge；缺少 `query_analysis`（仅 SQL 文本）→ `invalid_input`；
- **历史知识是证据而非规则**：只有相似度达标（阈值 0.5）的记录参与决策，并按相似度/
  时效加权；上下文不同则不引用；
- **风险控制**：证据不足时优先安全 baseline/fallback，不因"希望提升性能"选高风险策略；
- **不声称全局最优**：输出的是"当前信息条件下选择的策略"；
- 确定性纯函数：相同输入 → 相同输出。

## 2. 输入契约

```json
{
  "query_id": "q1",
  "query": "SELECT ...",                  // 可选：原始 SQL；提供后 Agent 自行生成候选策略与计划
  "query_analysis": { ... },              // 必填：Query Analysis Agent 的完整输出
  "database_state": {                     // 可选；未提供 → 视为 unknown
    "statistics": {"selectivity": {"time": 0.4, "root.sg1.d1.s1": 0.02}},
    "partition_info": {"total_partitions": 128, "covered_partitions": 2},
    "physical_organization": {"chunk_level": {"total_chunks": 1000, "covered_chunks": 5}},
    "device_scale": 1000,
    "scan_estimate": {"time_range": [0, 60000], "estimated_points": 100}
  },
  "system_state": {"cpu_utilization": 0.9, "memory_utilization": 0.5,
                   "io_utilization": 0.85},   // 可选；0~1 或 0~100
  "historical_records": [ ... ],          // 可选；格式见 schemas.HISTORICAL_RECORD_CONTRACT
  "candidate_strategies": {               // 可选；缺失且有 query → Agent 生成；否则目录全集
    "alignment": ["time_aligned"],
    "fetch_size": [
      // 富条目：候选生成器给出执行参数/等价 SQL（决策只选择、不生成 SQL）
      {"strategy": "fetch_large", "equivalent_sql": null,
       "parameters": {"fetch_size": 10000}}
    ]
  },
  "candidate_plans": [ ... ],             // 可选：外部注入计划描述符（覆盖内置生成）
  "plan_generation_notes": ["..."],       // 可选：外部注入计划时的事实说明
  "device_inventory": [ ... ],            // 可选：通配设备展开清单
  "planning": {                            // 可选：计划生成配置（默认开启）
    "enabled": true,
    "max_parallelism": 8,
    "min_saving_ms": 1.0,
    "min_saving_ratio": 0.03
  },
  "plan_min_saving_ms": 1.0,              // 可选：顶层净收益门槛（优先级高于 planning）
  "plan_min_saving_ratio": 0.03,          // 可选：相对单条门槛
  "baseline_strategy": {"alignment": "time_aligned", "fetch_size": "fetch_default"},
  "reference_time": 1700000000000         // 可选：历史时效衰减参考点（epoch 毫秒）
}
```

提供 `query` 时，计划生成是决策 Agent 的固有步骤：Agent 把查询编译成带
子查询与合并规格的可执行计划，用成本模型选择，并把选中计划写入
`selected_strategy.execution_plan`。计划是权威执行动作，会显式对齐
`decision.alignment/fetch_size` 的取值（见 docs/plan-space.md）。外部
`candidate_plans` 仅用于覆盖/测试注入；`planning.enabled=false` 或
`candidate_plans=[]` 可关闭自动计划生成（消融/对照）。

- 候选策略也接受扁平名称列表（如 `["full_scan", "partition_pruning"]`，按目录反查维度）；
- `load_level` 由 system_state 的 CPU/内存/IO 利用率推导：任一 >0.7 → high；
  全部 <0.3 → low；否则 normal；未提供 → unknown（不调整评分）。

## 3. 有界策略目录（`catalog.py`）

| 维度 | 策略 | 风险 | 需求（不满足 → 排除） | 作用方式 |
|---|---|---|---|---|
| alignment | time_aligned | low | 无 | 数据库默认（SQL 不变） |
| alignment | align_by_device | low | 多设备/通配设备 | SQL 追加 `ALIGN BY DEVICE`（内容等价、组织形态不同） |
| fetch_size | fetch_default | low | 无 | 数据库默认 1024 |
| fetch_size | fetch_large | low | 无 | 执行参数 `fetch_size=10000` |

计划不扩展这两个维度目录：它是 Agent 生成的**执行动作候选**（分区/部分聚合/
SQL 改写，由 Agent 内置生成器编译、成本模型选择），计划族与门禁见
docs/plan-space.md。

内置安全 baseline：`time_aligned` / `fetch_default`（输入可覆盖）。

## 4. 评分模型（`scoring.py`，全部确定性、可追踪）

```
score(c) = base(c) + context_bonus(c) + history_bonus(c) + system_bonus(c)
           + llm_bonus(c) + eval_prior(c)
```

**基础分（结构性先验）**：time_aligned 0.55 / align_by_device 0.45；
fetch_default 0.50 / fetch_large 0.50。

**上下文加分（只使用已提供的数据）**：
- time_aligned：设备数 ≥10 +0.10（实测宽设备扫描默认对齐快 15~25×）
- align_by_device：设备数 ≤5 +0.05（实测量少设备下略优）
- fetch_large：+0.05（实测大批次 ~15-20% 收益）

**历史加分**：该策略有相似记录时 `+0.25 × 证据权重 × (最优延迟/该策略延迟，封顶 1)`；
其他策略有记录而该策略无记录时 −0.10 × 维度证据总权重。

**系统调整**：当前策略空间无实测的负载交互 → 不调整（不编造依据）。

**LLM 偏好加分**（实验性，默认关闭）：第一偏好 +0.05，不参与风险门控。

**评价表先验**（D1）：`strength × (success_ratio − 0.5) × 2`，方向明确样本数
（`decisive_count` = improved + degraded + failed；unchanged 是中性结果，
不计入分母也不惩罚）达标才生效，有界 ±strength，不参与风险门控。

**风险门控**（通过后才可选）：
- low：需求满足即可；
- medium：需求满足 且（上下文加分 > 0 或 历史证据 ≥ 1 条）；
- high：需求满足 且 历史证据 ≥ 2 条。

**选择**：门控通过者按（score 降序 → 风险升序 → 目录顺序）取第一；
全部被排除 → 回退该维度 baseline（安全机制），维度置信度封顶 0.3。

## 5. 历史记录相似度（`similarity.py`）

- 相似度 = 0.6×查询特征 + 0.25×数据库状态 + 0.15×系统状态；
- 查询特征逐项加权：query_type 0.15、时间过滤 0.10、范围等级 0.10、时间跨度 0.10、
  多设备 0.05、设备数 0.05、非时间条件数 0.10、聚合 0.10、分组 0.05、窗口 0.05、
  窗口间隔 0.08（可选数值）、聚合函数集合 0.10（Jaccard）、测点指纹 0.10（Jaccard）；
  类别相等 1/0；数值 min/max 比例（双方缺失 0.5、单方缺失 0.3）；
  可选属性（窗口间隔/聚合函数/测点）双方缺失 1.0（没有可比较的差异）、单方缺失 0.3
  （旧记录未提取，不可比）。窗口间隔与测点指纹用于区分结构相同但对象不同的
  近邻形状：q3/q10（测点与过滤值不同）、q7/q11（窗口间隔/聚合函数不同）；
- 阈值 0.5：低于阈值不计入证据（记入 evidence 摘要）；
- **同查询形状证据优先**：`same_query_shape` 要求除 `time.time_span`（允许 10%
  相对波动，时间边界随数据有细微差异）外所有紧凑特征完全一致，聚合函数集合与
  测点集合按集合相等判断；`same_shape_evidence` 进一步要求同数据规模
  （`data_scale_level` 相等，缺失时设备规模相差 ≤10%），且子集执行次数
  ≥ `MIN_BASELINE_SAMPLES`（3）才启用。计划的成本预测（`single_ms` 与各计划
  预测延迟）优先只在该子集内构造，样本不足才退回全部相似记录——避免近邻形状的
  绝对延迟混入当前形状（如 q3 的 3.539ms 稀释 q10 的 4.10ms）；
- 时效衰减：1/(1 + age_hours/72)（需 record_time 与 reference_time）；
- 结构不合法的记录（缺 record_id/query_features/strategy/数值延迟）计为 invalid，忽略。

## 6. 置信度（`confidence.py`）

维度置信度 = 0.35×data_factor + 0.35×evidence_factor + 0.30×margin_factor：
- data_factor：有数据库上下文支撑 1.0；仅需求满足 0.5；回退 0.2（且封顶 0.3）；
- evidence_factor：min(1.0, 该策略证据权重×2)；无证据 0；
- margin_factor：与第二名分差 ≥0.15 → 1.0；≥0.05 → 0.7；>0 → 0.5；唯一候选 0.6；平分 0.4；
- 高风险策略封顶 0.7。

整体置信度 = 适用维度置信度均值 × (0.7 + 0.3×分析置信度)；
无任何数据库状态且无历史证据 → 封顶 0.6；输入无效 → 0.0。
**证据不足时不得人为提高置信度。**

## 7. 输出契约（schema v1.0）

顶层：`query_id`、`decision`（各维度 {strategy, reason, confidence,
equivalent_sql}）、`selected_strategy`（strategy_id + strategy_parameters +
equivalent_sql）、`evidence`（实际用于决策的条目）、`overall_confidence`、
`fallback_strategy`、`decision_status`、`notes`（扩展）。

- 提供 `query` 且计划生成成功时额外输出：
  - `decision.plan`：计划决策条目（strategy=plan_id，family/risk/predicted_ms/
    single_ms/saving_ms/fallback 扩展字段）；
  - `selected_strategy.execution_plan`：选中计划的完整描述符（执行器直接消费）；
  - `plan_selection`：全部候选计划的评估明细、单条基线来源与选择说明
    （`single_ms/single_source/single_count`；baseline 历史样本不足时改用
    同查询形状计划实测典型值，见 docs/plan-space.md §3.1）；
  - `selected_strategy.strategy_id` 追加 `plan=<plan_id>`；
  - `decision.alignment/fetch_size` 被显式对齐到计划实际取值；
  - `decision_status` 保持 success（baseline 也是有效的安全动作）；
    `decision.plan.fallback=true` 表示没有候选超过收益/风险门槛。
- 未提供 `query`（如仅调用决策 Agent 做纯维度选择）或显式关闭计划生成时，
  不输出 `decision.plan` / `execution_plan` / `plan_selection`，维度路径保持原样。
- 维度不适用（如单设备查询的对齐维度）→ `strategy = "not_applicable"` + 原因，置信度 0；
- `decision_status`：success / partial_fallback / fallback / invalid_input；
- `strategy_parameters` 只包含事实性取值（如裁剪时间边界）+ 候选提供的执行级参数；
  计划生成成功时含 `plan` 摘要与计划实际使用的 `fetch_size`；
- **equivalent_sql（混合模型）**：
  - 维度级：选中候选携带的等价 SQL，未携带 → null；
  - 组合规则（`selected_strategy.equivalent_sql`，可直接执行的 SQL）：
    - 0 个维度携带 → 原查询文本（策略为执行级参数，SQL 不变）；
    - 恰好 1 个维度携带 → 该 SQL；
    - ≥2 个维度携带 → null（按维度独立生成的 SQL 无法可靠组合），
      执行层按 strategy_id 与各维度参数执行，绝不输出可能错误的 SQL；
- reason 引用具体证据（跨度/分区覆盖/历史记录数/相似度/系统负载），
  以"在当前信息条件下选择 X"结尾，不出现全局最优声明。

## 8. LLM 语义增强（混合增强模式）

确定性评分与风险门控是权威；LLM 只填充 `llm_analysis` 附加节：

```json
"llm_analysis": {
  "available": true,
  "semantic_reason": "设备数较少时按设备对齐有实测依据；当前信息条件下的选择。",
  "preferences": {"alignment": ["align_by_device", "time_aligned"], ...},
  "notes": []
}
```

- `semantic_reason`：对最终决策的自然语言解读（llm_generated，明确"当前信息条件下"）；
- `preferences`：候选偏好排序，**严格校验**（只允许输入候选集合中的策略名，非法条目丢弃）；
- **实验性偏好加分**（默认关闭）：`decision_input["llm_preference_bonus"]=true`
  时，LLM 第一偏好 +0.05 分。硬性边界：
  - 加分**不参与风险门控**（LLM 不能解锁高风险策略、不能改变证据不足回退 baseline
    的安全路径）；
  - 加分有界（0.05），强证据下的选择不会被 LLM 翻盘；
  - LLM 失败/输出非法 → 无加分，决策与不启用 LLM 完全一致（确定性兜底）；
- 非确定性说明：启用 LLM 后 `llm_analysis` 节受模型随机性影响；核心决策字段在
  `llm_preference_bonus=false` 时保持确定性。

接入方式：`decide(decision_input, llm=llm_client)`；客户端由共享层构造
（`mataof/llm.py`，配置见 docs/cli.md 的 llm 节）。

### 8.1 闭环模式（可选，默认关闭）

`decide()` 是纯函数；`run(decision_input, execute=...)` 在它外面加一个有界控制环：

1. 初始 `historical_records` 为空且注入 `recall` 工具时，先通过 AgentBus 向
   Knowledge Memory Agent 请求一次更宽 top-K 检索；结果按 `record_id` 去重合并，
   失败或仍为空则照常决策（不伪造证据）；
2. 决策 → 通过 `execute_decision` 工具执行 → 观察监控反馈；
3. `improved`/`unchanged` → 接受并终止；
4. `degraded`/`failed` 且仍有尝试预算 → 把该 `plan_id` 放入
   `excluded_plan_ids`，走确定性路径重规划 → 再次执行；
5. 预算耗尽 / 无替代计划 / baseline 失败 → 终止。

约束：不新增策略来源；baseline 不被排除也不重试；LLM 不参与重规划（避免放大调用
成本）；所有尝试、终止原因与逐步 trace 写入输出的 `agent` 节，补充证据结果写入
`agent.evidence_recall`（`attempted`/`ok`/`records_added`/`retrieval`/`agent`/
`error`；`agent` 为补充检索那次 KM 运行的摘要）。
配置键为 `agents.optimization_decision`（旧 `agent` 键仍兼容），补充检索由
`recall_on_empty_evidence`（默认 true）与 `recall_max_records`（默认 null =
`max(200, 首轮×2)`）控制；完整字段、trace 结构与消融见
[docs/agent-runtime.md](agent-runtime.md)。

## 9. 与上下游的接口约定

- 上游（Query Analysis Agent）：消费其完整输出；`analysis_confidence` 参与整体置信度；
- 上游（Knowledge Memory Agent）：消费 `historical_records`（契约见
  `schemas.HISTORICAL_RECORD_CONTRACT`），历史只是证据，相似度不足时不引用；
  闭环模式下证据为空时经 AgentBus 向其发送 `recall` 请求（更宽 top-K），
  只合并返回的决策可用记录；
- 下游（执行层）：`selected_strategy.strategy_id` 与 `strategy_parameters` 为可传递的
  结构化策略；计划生成成功时 `selected_strategy.execution_plan` 是执行层优先消费的
  可执行动作（子查询 + 合并规格），执行失败时执行器回退原查询并记录
  `plan_fallback=true`；`fallback_strategy` 供维度级回退说明。

## 10. 验证方式

- 单元测试：`pytest tests/test_optimization_decision_agent.py`（输入契约、门控/回退、
  数据驱动决策、历史相似度、置信度、确定性、schema）；
- 计划能力：`pytest tests/test_plan_space.py tests/test_plan_merge_executor.py`
  （Agent 自动生成/显式覆盖、语法硬门禁、生成器可执行性、成本模型、合并语义、
  执行器回退）；
- 全量回归：`pytest tests/`。
