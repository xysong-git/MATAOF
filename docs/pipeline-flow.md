# MATAOF 整体流程说明（当前代码设计）

版本：2026-09-22（含采样、唯一命名、证据截断、LLM 增强、IoTDB 执行）
本文档描述"一个含多条 SQL 的文件"从输入到最终输出的完整流程。

## 第 0 层：文件入口（CLI / PipelineRunner）

```bash
mataof run --file queries.sql --config config.dbs.json \
           --per-file-limit 100 --sample random --seed 42
```

1. **拆分**：按 `;` 拆分语句，去掉 `--` 行注释与空行（`split_sql_statements`）；
   `.jsonl` 文件则逐行读取 `{"query_id", "query"}`；
2. **每文件采样**（`sample_items`）：
   - `per_file_limit` 未设或 ≥ 语句数 → 全量；
   - `sample_mode=seq` → 取前 N 条；
   - `sample_mode=random` → 按种子随机抽 N 条（保持文件内原序，同种子可复现）；
   - 采样说明写入每条追踪的 `sampling` 字段与 run_log；
3. **唯一命名**：每条语句分配 query_id = `<run_id>-<序号>`（run_id 为本次运行的
   时戳），`source` 字段记录来源文件名——不同运行的产物互不覆盖（极端冲突自动
   加 `-2` 后缀）。

以下是对**每一条 SQL** 的七步闭环：

```
文件 → [① Query Analysis] → [② Knowledge Retrieve] → [③ Optimization Decision]
                                   ↑ 历史证据                │ strategy + equivalent_sql
                                   │                         ▼
        [⑦ 追踪落盘] ← [⑥ Knowledge Update] ← [⑤ Execution Monitoring] ← [④ 执行层 IoTDB]
```

---

## ① Query Analysis Agent —— 只分析，不决策

**做什么**：确定性语义解析 + 六类特征提取 + 优化维度相关性陈述。

| 步骤 | 说明 |
|---|---|
| SQL 解析 | sqlglot AST（IoTDB 特殊语法预处理：窗口 GROUP BY、ALIGN BY DEVICE、通配符路径）；解析失败 → unknown 降级 |
| 任务1 查询类型 | 五类 + unknown（判定树，不强行分类） |
| 任务2 时间特征 | 起止/跨度/范围等级（narrow<1h≤medium<1d≤wide）/条件形式 |
| 任务3 设备维度 | FROM 路径/设备数/多设备（通配符 → unknown 不猜测） |
| 任务4 过滤条件 | 逐条件记录（time/value/tag_or_attribute/subquery）+ 组合树 + 选择率（仅拷贝数据库提供值） |
| 任务5 聚合特征 | 函数/GROUP BY 三种形式/窗口/聚合范围 |
| 任务6 扫描相关 | 扫描估计仅来自数据库统计；定性结构信号 |
| 相关性陈述 | 三个优化维度的"属于相关候选"陈述（不选策略） |
| 置信度 | 确定性公式（解析失败 0.1；按未知项扣减） |
| LLM 增强（可选） | 语义摘要 / 类型提示（不改写确定性类型）/ 标签属性判别建议 → `llm_analysis` 节；失败自动兜底 |

**输出**：完整分析 JSON（query_type + query_features + optimization_relevant_features
+ unknown_features + analysis_confidence + llm_analysis）。

## ② Knowledge Memory 检索 —— 只提供证据

**做什么**：在历史知识库中检索与当前查询相似的经验。

1. **全库扫描**：对库中每条记录计算多维相似度（0.6×查询特征 + 0.25×数据库状态 +
   0.15×系统状态——与决策 Agent 共用同一套口径，非 SQL 文本匹配）；
2. **三层匹配**：strong ≥0.8 / moderate ≥0.5 / weak ≥0.3；低于 0.3 不返回；
3. **top-K 截断**：按相似度取 top-K（runner 默认 100，`knowledge_max_records`），
   防止证据清单/结果文件随库膨胀；`truncated` 字段记录全貌；
4. **汇总**（始终基于全部匹配计算，不受截断影响）：
   - 成功策略模式：累计 ≥3 次且成功率 ≥0.6；
   - 失败经验：failed/timeout/degraded/critical 异常的汇总；
   - 逐策略统计（total/success/failure/success_ratio/avg_latency_ms）；
   - knowledge_confidence；
5. 输出 `matched_records_for_decision`（决策 Agent 的历史证据输入）。
   runner 链路跳过 LLM 解读（决策只消费结构化记录）。

**输出**：matched_records + historical_summary + knowledge_confidence +
matched_records_for_decision + match_counts + truncated。

## ③ Optimization Decision Agent —— 选策略，产出可执行物

**做什么**：在有界候选策略空间中为当前查询选择优化策略（三维度）。

1. **候选过滤**：输入候选 ∩ 策略目录；逐候选做需求评估（如 partition_pruning 要求
   时间边界已知），不满足 → 排除并记录原因；
2. **评分**：`score = base + context + history + system [+ llm_bonus]`
   - base：结构性先验分；
   - context：数据支撑加分（分区覆盖/选择率/扫描估计/窗口等，无数据不加分）；
   - history：相似历史证据加权（好表现加分、无证据略扣）；
   - system：高负载调整；
   - llm_bonus：实验性 LLM 偏好加分（+0.05，默认关，**不参与门控**）；
3. **风险门控**：low 需求满足即可；medium 需支撑信号或历史 ≥1；high 需历史 ≥2——
   LLM 不能解锁高风险策略；
4. **选择**：门控通过者按（score↓、风险↑、目录序）取第一；全部排除 → 回退该维度
   baseline；维度不适用 → not_applicable；
5. **混合输出**：`selected_strategy` = strategy_id + strategy_parameters +
   `equivalent_sql`（0 维携带 → 原查询；1 维携带 → 该 SQL；≥2 维 → null 不冒险组合）；
6. 证据清单 top-K 有界、reason 引用具体证据、不声称全局最优；
7. LLM 增强（可选）：决策解读 + 候选偏好（严格限于候选集合）。

**输出**：decision{三维度 strategy/reason/confidence/equivalent_sql} +
selected_strategy + evidence + overall_confidence + fallback_strategy +
decision_status + llm_analysis。

## ④ 执行层（IoTDBExecutor）—— 唯一接触数据库

**做什么**：执行选中策略并采集真实指标。

1. `equivalent_sql` 非空 → 执行该 SQL；否则执行原查询（执行级策略由引擎按其能力执行）；
2. `measure_baseline=true` 时先执行一次原查询作为真实 baseline；
3. 采集：墙钟延迟（含结果集完整消费）、执行期间 CPU/内存采样、状态与失败原因
   （真实异常分类：超时→timeout、连接→failed、SQL 错误→failed 原样记录）；
   单次执行不产生分位数（P50/P95/P99 置空，不编造）。

**输出**：ExecutionResult{execution_status, failure_reason, metrics,
baseline_metrics, timestamp}。

## ⑤ Execution Monitoring Agent —— 事实采集，不改策略

**做什么**：把执行事实整理为结构化反馈。

1. 指标校验归一化（无效 → null + 说明）；
2. baseline 对比：各指标变化百分比 + performance_change 方向标签；
   无 baseline 不生成比较结果；
3. 策略效果判断：improved/degraded/unchanged/failed/insufficient_evidence
   （基于实测；单次观察 scope=single_execution）；
4. 异常识别（9 类：超时/失败/取消/延迟超阈/CPU/内存/IO 偏离/相对 baseline 偏离/
   历史偏离），只记录；
5. LLM 增强（可选）：性能复述 + 异常关联解读（禁因果归因、禁建议；
   **数字级防虚构校验**——LLM 输出的数字必须 ⊆ 给定监控数据）。

**输出**：监控 JSON（execution_status/metrics/baseline_comparison/
performance_assessment/anomalies/feedback/llm_analysis）。

## ⑥ Knowledge Memory 更新 —— 记住这次执行

**做什么**：把三 Agent 输出关联为一条历史记录并入库。

1. 装配记录（8 要素：Query + Features + Context + Strategy + Execution +
   Result + Experience + Timestamp）；时间戳缺失 → null 不编造；
2. LLM 经验总结（可选）随记录存入 `llm_note` 元数据字段（与事实字段隔离）；
3. **append-only 入库**：只追加，不修改、不删除任何历史记录；
4. 成功/失败经验标记、逐策略累计统计更新、priority change
   （累计成功率跨 ±0.1 才报告方向）。

**输出**：update_type/record_id/stored/strategy_effect/knowledge_update/llm_analysis。

## ⑦ 追踪落盘 —— 最终产物

| 产物 | 说明 |
|---|---|
| `results/<目录>/<query_id>.json` | ①~⑥ 完整决策链快照（含决策时刻的证据快照——知识库会变，快照保证可审计） |
| `results/<目录>/run_log.jsonl` | 追加汇总行：query_id/source/sampling/query_type/strategy_id/decision_status/execution_status/performance_assessment/anomaly_count/response_time_ms/knowledge_confidence/stored |
| `data/<knowledge_store>.json` | append-only 知识库（跨运行累积） |
| `data/llm_log.jsonl` | LLM 调用日志（配置启用时） |

---

## 横切设计要点

1. **确定性底座 + LLM 增强**：四个 Agent 的核心产出全部确定性；LLM 只写各 Agent 的
   `llm_analysis` 附加节，失败/非法即兜底——LLM 影响可作为独立消融维度；
2. **三层采样**：语句级（每文件取样）→ LLM 增强级（sample_rate）→ 证据级（top-K）；
3. **安全路径**：无执行数据 → unknown 照常闭环；无上下文 → baseline/fallback；
   高风险策略需强证据；单次异常不否定历史；
4. **可追踪**：唯一命名不覆盖、证据快照、采样信息、LLM 调用日志、append-only 知识库。
