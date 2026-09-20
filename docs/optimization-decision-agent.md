# Optimization Decision Agent（优化决策智能体）规格说明

版本：v1.0
实现：`mataof/agents/optimization_decision/`

## 1. 职责与边界

**核心职责**：根据 Query Analysis Agent 提供的查询特征、当前数据库状态、系统状态以及
Knowledge Memory Agent 提供的历史执行经验，在预定义的候选策略空间中选择当前查询
更合适的优化策略。负责"策略选择"，不负责查询解析和实际执行。

**严格界限**：
- **有界策略空间**：候选 = 输入候选策略 ∩ 策略目录（`catalog.py`）；禁止自行创造候选
  策略；目录之外的策略名一律拒绝（记入 `notes`）；
- **混合输出模型**：输出 = 策略标识 + 等价 SQL + 执行级参数。等价 SQL 由候选提供方
  随候选携带，**决策 Agent 自身不生成/改写 SQL**（只选择）；不可 SQL 表达的维度
  （分区/chunk 裁剪、聚合位置）输出执行级参数，由执行层按 strategy_id 应用；
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
  "candidate_strategies": {               // 可选；缺失 → 使用目录全集
    "time_pruning": ["full_scan", "partition_pruning"],
    "filter_order": [
      // 富条目：候选提供方给出等价 SQL（决策 Agent 只选择、不生成 SQL）
      {"strategy": "time_first",
       "equivalent_sql": "SELECT ... WHERE time >= ... AND s1 > 10 ...",
       "parameters": {"hint": "..."}},
      "device_tag_first"
    ],
    "aggregation_placement": []
  },
  "baseline_strategy": {"time_pruning": "full_scan"},   // 可选；缺失 → 内置安全默认
  "reference_time": 1700000000000         // 可选；历史时效衰减参考点（epoch 毫秒）
}
```

- 候选策略也接受扁平名称列表（如 `["full_scan", "partition_pruning"]`，按目录反查维度）；
- `load_level` 由 system_state 的 CPU/内存/IO 利用率推导：任一 >0.7 → high；
  全部 <0.3 → low；否则 normal；未提供 → unknown（不调整评分）。

## 3. 有界策略目录（`catalog.py`）

| 维度 | 策略 | 风险 | 需求（不满足 → 排除） | SQL 可表达 |
|---|---|---|---|---|
| time_pruning | full_scan | low | 无 | 否（执行级） |
| time_pruning | partition_pruning | medium | 时间过滤边界已知 | 否（执行级） |
| time_pruning | chunk_level_filtering | high | 边界已知 + 数据库提供 chunk 级物理组织信息 | 否（执行级） |
| filter_order | time_first | low | 非时间条件 ≥ 2 | **是**（WHERE 重排） |
| filter_order | device_tag_first | medium | 非时间条件 ≥ 2 | **是**（WHERE 重排） |
| aggregation_placement | final_level_aggregation | low | 含聚合 | 否（执行级） |
| aggregation_placement | intermediate_level_aggregation | medium | 含聚合 | 否（执行级） |
| aggregation_placement | scan_level_aggregation | medium | 含聚合（执行层支持 = 外部系统将其列入候选） | 否（执行级） |

内置安全 baseline：`full_scan` / `time_first` / `final_level_aggregation`（输入可覆盖）。

## 4. 评分模型（`scoring.py`，全部确定性、可追踪）

```
score(c) = base(c) + context_bonus(c) + history_bonus(c) + system_bonus(c)
```

**基础分（结构性先验）**：full_scan 0.30 / partition_pruning 0.55 / chunk 0.70；
time_first 0.60 / device_tag_first 0.35；final 0.50 / intermediate 0.55 / scan 0.60。

**上下文加分（只使用已提供的数据）**：
- partition_pruning：范围 narrow +0.10；分区覆盖 ≤25% +0.10
- chunk_level_filtering：范围 narrow +0.10；chunk 覆盖 ≤10% +0.10
- time_first：范围 narrow +0.10；时间条件平均选择率 ≤0.1 → +0.10，≥0.5 → −0.10
- device_tag_first：非时间条件平均选择率 ≤0.1 且优于时间条件 → +0.40（无选择率数据
  则不加分——不得在缺乏数据时假设其选择性更高）；设备过滤覆盖 ≤10%（device_scale 已知）→ +0.15
- scan_level_aggregation：范围 narrow 或扫描估计小 → +0.15；窗口聚合 → +0.10
- intermediate_level_aggregation：存在 GROUP BY → +0.10；可能较大中间结果 → +0.10

**历史加分**：该策略有相似记录时 `+0.25 × 证据权重 × (最优延迟/该策略延迟，封顶 1)`；
其他策略有记录而该策略无记录时 −0.10 × 维度证据总权重。

**系统调整**（load_level=high）：full_scan −0.05、final_level −0.05；
partition/chunk/time_first/scan_level +0.05。

**风险门控**（通过后才可选）：
- low：需求满足即可；
- medium：需求满足 且（上下文加分 > 0 或 历史证据 ≥ 1 条）；
- high：需求满足 且 历史证据 ≥ 2 条。

**选择**：门控通过者按（score 降序 → 风险升序 → 目录顺序）取第一；
全部被排除 → 回退该维度 baseline（安全机制），维度置信度封顶 0.3。

## 5. 历史记录相似度（`similarity.py`）

- 相似度 = 0.6×查询特征 + 0.25×数据库状态 + 0.15×系统状态；
- 查询特征逐项加权：query_type 0.15、时间过滤/范围等级/跨度、设备数/多设备、
  非时间条件数、聚合/分组/窗口、聚合函数集合（Jaccard）；类别相等 1/0、
  数值 min/max 比例、双方缺失 0.5、单方缺失 0.3；
- 阈值 0.5：低于阈值不计入证据（记入 evidence 摘要）；
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

顶层：`query_id`、`decision`（三维度各 {strategy, reason, confidence,
equivalent_sql}）、`selected_strategy`（strategy_id + strategy_parameters +
equivalent_sql）、`evidence`（实际用于决策的条目）、`overall_confidence`、
`fallback_strategy`、`decision_status`、`notes`（扩展）。

- 维度不适用（如查询无聚合）→ `strategy = "not_applicable"` + 原因，置信度 0；
- `decision_status`：success / partial_fallback / fallback / invalid_input；
- `strategy_parameters` 只包含事实性取值（如裁剪时间边界）+ 候选提供的执行级参数；
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
  "semantic_reason": "窄时间范围且分区覆盖低，partition_pruning 有明确依据；当前信息条件下的选择。",
  "preferences": {"time_pruning": ["partition_pruning", "full_scan"], ...},
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

## 9. 与上下游的接口约定

- 上游（Query Analysis Agent）：消费其完整输出；`analysis_confidence` 参与整体置信度；
- 上游（Knowledge Memory Agent）：消费 `historical_records`（契约见
  `schemas.HISTORICAL_RECORD_CONTRACT`），历史只是证据，相似度不足时不引用；
- 下游（执行层）：`selected_strategy.strategy_id` 与 `strategy_parameters` 为可传递的
  结构化策略；`fallback_strategy` 供执行层在策略执行失败时回退。

## 9. 验证方式

- 单元测试：`pytest tests/test_optimization_decision_agent.py`（25 例，覆盖输入契约、
  门控/回退、数据驱动决策、历史相似度、置信度、确定性、schema）；
- 全量回归：`pytest tests/`（63 例）。
