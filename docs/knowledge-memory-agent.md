# Knowledge Memory Agent（知识记忆智能体）规格说明

版本：v1.0
实现：`mataof/agents/knowledge_memory/`

## 1. 职责与边界

**核心职责**：负责历史查询、查询特征、优化策略、数据库状态、系统状态以及实际执行
结果的结构化存储、检索、关联与更新，为 Optimization Decision Agent 提供历史决策依据。
**职责不是直接决定优化策略。**

**严格限制（严格遵守）**：
- 禁止编造历史查询、编造执行结果；
- 禁止修改真实历史数据、禁止删除与当前决策不一致的历史记录
  → 存储为 **append-only**（无修改/删除接口）；
- 不把单次实验结果当作绝对规律——历史样本永久保留，策略评价随累计结果更新；
- 不直接决定当前优化策略（检索结果仅作为决策 Agent 的证据输入）。

## 2. 知识对象（记录结构）

每条记录 = 一次完整查询执行的关联（Query + Features + DB State + System State +
Strategy + Execution Metrics + Execution Result + Timestamp）：

```json
{
  "record_id": "rec00000001",
  "query_record": {"query_id": "", "query": "", "query_features": {}},
  "context": {"database_state": {}, "system_state": {}},
  "strategy": {"strategy_id": "", "strategy_parameters": {}, "by_dimension": {}},
  "execution": {"status": "", "failure_reason": null, "metrics": {}},
  "execution_result": "improved",
  "anomalies": [],
  "experience": {"effect": "", "confidence": null},
  "timestamp": null
}
```

装配规则（`record.py`）：
- 一切内容只来自三个 Agent 的真实输出，绝不编造；
- 缺少 monitoring（执行反馈）→ 不存储（`rejected_missing_execution`）；
- 时间戳只接受输入提供的执行时间，缺失 → null + 说明（不编造）；
- `by_dimension` 优先取自 decision 输出，否则从 strategy_id 解析。

## 3. 存储（`store.py`，append-only）

- 只允许追加（`add`），不提供修改/删除接口；
- JSON 持久化采用原子写（临时文件 + os.replace），损坏文件从空库恢复且不覆盖原文件；
- record_id 由存储分配（`rec00000001` 递增），唯一可追踪；
- `store.statistics()` 提供全库累计统计（按 strategy_id：total/success/failure/
  success_ratio/avg_latency_ms）。

## 4. 检索（`retrieval.py`）

相似度口径与 Optimization Decision Agent 完全一致（共享模块 `mataof/similarity.py`，
0.6×查询特征 + 0.25×数据库状态 + 0.15×系统状态），多维特征匹配，**不做 SQL 文本
字符串匹配**。

匹配分层：
| 层级 | 综合相似度 |
|---|---|
| Strong Match | ≥ 0.8 |
| Moderate Match | 0.5 ~ 0.8 |
| Weak Match | 0.3 ~ 0.5（低于 0.3 不返回） |

输出（规格字段 + 扩展）：
- `matched_records`：record_id、similarity、query/database/system_similarity、
  match_level（扩展）、strategy_id、execution_result、performance；
- `historical_summary.successful_strategies`：成功经验模式——某策略在匹配记录中
  累计 ≥ 3 次且成功率 ≥ 0.6 时形成（策略、查询条件摘要、数据库状态摘要、性能表现、
  次数、成功比例）；
- `historical_summary.failed_strategies`：失败经验保留汇总（failed/timeout/degraded/
  critical 异常；含失败类型分布）；
- `historical_summary.strategy_statistics`：逐策略 total/success/failure/success_ratio/
  avg_latency_ms；
- `knowledge_confidence`：min(1.0, 0.25×strong + 0.15×moderate + 0.05×weak)；
- `matched_records_for_decision`（扩展）：决策 Agent 契约格式的记录列表，可直接作为
  `historical_records` 输入。

## 5. 反馈更新（`agent.update`）

- 装配记录 → append-only 入库（新记录，绝不动旧记录）；
- `strategy_effect` = monitoring 的 performance_assessment；
- `knowledge_update.success_record` = improved；
- `knowledge_update.failure_record` = degraded/failed 或执行 failed/timeout 或含
  critical 异常；
- `knowledge_update.strategy_priority_change`：该策略累计成功率跨过 ±0.1 阈值时
  报告 increased/decreased，否则 null（单次异常不立即否定历史，评价随累计更新）；
- 输出规格字段 + notes（扩展）。

## 6. LLM 语义增强（混合增强模式）

确定性检索与 append-only 事实记录是权威；LLM 只提供解读与总结：

- **retrieve 增强**（`llm_analysis` 节）：
  - `semantic_summary`：对匹配结果、成功模式、失败经验的自然语言解读
    （llm_generated；**prompt 明确禁止策略建议**——KM 不决策原则不变）；
- **update 增强**（`llm_analysis` 节）：
  - `experience_note`：本次执行经验的自然语言总结（单次观察不表述为规律），
    随记录存入知识库的 `llm_note` 字段——与事实字段隔离的 LLM 生成元数据；
- **确定性兜底**：LLM 未启用/失败/输出非法 → `available=false` + notes；
  检索结果与入库记录完全不变（失败时不写入任何 LLM 元数据）；
- **非确定性说明**：`llm_analysis`/`llm_note` 受模型随机性影响；匹配、
  统计、成功率等核心字段保持确定性。

接入方式：`KnowledgeMemoryAgent(store_path=..., llm=llm_client)`；
客户端由共享层构造（`mataof/llm.py`，配置见 docs/cli.md 的 llm 节）。

## 7. 与上下游的接口约定

- 上游（三个 Agent）：query_analysis + decision + monitoring + 上下文 + 时间戳 →
  `km.update(...)`；
- 下游（Optimization Decision Agent）：`km.retrieve(...)["matched_records_for_decision"]`
  → `decide({"historical_records": ...})`（端到端已由测试验证）；
- 历史只是证据，不是规则：决策 Agent 会再次按相似度/时效加权，低相似记录不引用。

## 7. 验证方式

- 单元测试：`pytest tests/test_knowledge_memory_agent.py`（17 例，含端到端闭环）；
- 全量回归：`pytest tests/`（106 例）。
