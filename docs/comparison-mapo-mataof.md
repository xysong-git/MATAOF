# MAPO（ADEPT）与 MATAOF 对比报告

**对象**：MAPO/ADEPT（前序工作，`/opt/program-code/MAPO`）vs MATAOF（多智能体自适应查询优化系统）
**日期**：2026-09-16

## 一句话总结

MATAOF 沿用了 MAPO 的**三大优化维度**（Time Pruning / Filter Order / Aggregation
Placement）、**候选路径空间**、**回退链与失败规避**、**执行反馈闭环**与**真实 IoTDB
实验语料**；但把"基于 SQL 改写的学习系统"重构为"基于结构化策略选择的确定性多智能体
系统"——核心差异是**优化对象（改写 SQL → 选择策略）**与**决策依据（学习代价模型 →
可验证证据链）**。

---

## 一、总体对比

| 维度 | MAPO / ADEPT | MATAOF |
|---|---|---|
| 架构形态 | 单体框架 + Bao Server 服务（模块化但单进程） | 四个职责独立的 Agent + 正式运行入口（PipelineRunner/CLI） |
| 优化对象 | **改写 SQL 文本**（候选 = 改写后的 SQL 字符串，自产自选） | **混合模型**：选择结构化策略（strategy_id + 参数），候选提供方给出等价 SQL 时直接输出可执行 SQL；决策 Agent 自身不生成/改写 SQL |
| 决策依据 | 学习代价模型（Bao + RandomForest + TreeCNN）+ 启发式打分（ω 权重、candidate_score） | 确定性规则 + **显式证据链**（特征/数据库状态/系统状态/历史记录，逐条可追踪） |
| 知识形态 | Bao 训练数据（bao.db 全局模型）+ Arm 黑名单 | 结构化历史记录库（append-only，可解释、可审计） |
| 对未知的态度 | 启发式估计（ω 权重、EXPLAIN ANALYZE 间接指标） | **unknown 标记**：无法确定一律 null/unknown，绝不编造 |
| 安全机制 | 运行时容错：Arm 黑名单 + 回退链 + 错误分类 | 事前风险门控 + 事后异常记录 + baseline/fallback |
| 冷启动 | 需 EXPLAIN ANALYZE 收集训练数据（已知不足②） | 零训练零预热：证据不足直接走安全 baseline |
| 机器学习 | Bao 学习模型 + Qwen2.5-3B LLM（SQL 改写） | v1 无学习模型、无 LLM（预留扩展位） |
| 运行方式 | 脚本 + proxy（run_queries.py / proxy_run.py） | `mataof run/retrieve/stats` CLI + 可编程 Runner + 执行器抽象 |
| 可追踪性 | 查询日志 + results/ 实验图 | 每次运行完整决策链落盘（`<query_id>.json` + `run_log.jsonl`） |

---

## 二、沿用 MAPO 的设计

| MAPO 元素（位置） | MATAOF 对应物 | 延续方式 |
|---|---|---|
| 三大优化维度：时间裁剪 / 过滤顺序 / 聚合位置（MQD，conclusion.md） | `optimization_decision/catalog.py` 三个维度的策略目录 | **直接沿用**；MQD 的第四维"算子实现"未进入 MATAOF v1 目录 |
| 候选路径空间（iotdb_candidate_planner 生成候选） | 有界策略目录 + 候选集合输入（禁止自造策略） | 思想沿用；实现从"生成改写 SQL"变为"从目录中选择策略" |
| 回退链 `_execute_with_fallback`（adept_exec.py） | 决策 Agent 的 baseline/fallback + `fallback_strategy` 输出 | 沿用：证据不足/全部候选被排除 → 回退安全策略 |
| Arm 黑名单（adept_infra.py：连续失败阈值 3 + 冷却 20） | 失败经验保留（KM failed_strategies）+ 风险门控（high 风险需 ≥2 条历史证据） | 意图沿用（避免反复选失败的策略）；机制从"运行时黑名单"变为"历史证据累积" |
| 错误分类 CONNECTION/TIMEOUT/SQL_ERROR/…（adept_infra.py） | 监控 Agent 的 execution_status（success/timeout/failed/cancelled/unknown）+ failure_reason + 异常类型 | 沿用并结构化：分类进入标准输出契约 |
| 执行反馈闭环：Bao reward 上报 + 重训练（adept_bao.py） | 监控反馈 → KM 入库 → 相似检索 → 决策证据 | 沿用闭环思想；去掉重训练，改为证据累积 |
| 真实 IoTDB 查询语料（sample_queries_iotdb*，q1~q12 生成工作负载） | `tests/test_sample_corpus.py` 鲁棒性验证语料 | **直接复用同一批语料**做回归测试 |
| 实验/评估导向（results/、CDF 图、analyze_by_type.py） | results_dir 追踪落盘 + run_log.jsonl（实验统计用） | 沿用目标，形式结构化 |

---

## 三、MATAOF 新增的设计

1. **多智能体分工与边界**：Query Analysis / Optimization Decision / Execution
   Monitoring / Knowledge Memory 四个 Agent，职责单一、边界明确（"分析的不决策、
   决策的不执行、监控的不改策略、记忆的不做决策"），JSON 结构化通信 + schema_version。
2. **确定性 SQL 语义解析**（sqlglot AST + IoTDB 语法预处理），取代 MAPO 的 regex
   文本匹配；六类特征提取，全部可验证。
3. **unknown 标记原则贯穿全链路**：选择率/扫描估计/设备数量/时间戳等缺失即 null +
   原因记录，无任何启发式填补。
4. **独立的事实采集 Agent**：baseline 对比、效果判断（improved/degraded/unchanged/
   failed/insufficient_evidence）、异常识别——与执行层解耦。
5. **显式历史知识库**：append-only 存储（禁止修改/删除历史）、Strong/Moderate/Weak
   三层相似匹配、成功策略模式（≥3 次且成功率 ≥0.6）与失败经验汇总。
6. **历史证据相似度门控**：历史记录是"证据"而非"规则"，只有上下文（查询特征 +
   数据库状态 + 系统状态）相似才引用，并按相似度/时效加权。
7. **策略风险分级与需求声明**：目录中每个策略声明风险等级（low/medium/high）与
   输入需求，不满足需求或证据不足即门控排除——把"数据库不支持的能力"显式化为可
   检查的约束。
8. **执行层抽象（Executor 协议）**：策略到执行动作的翻译交给执行层，Agent 侧不碰
   数据库；NullExecutor（不编造）/ FileExecutor（回放真实数据）/ 自定义对接。
9. **正式运行入口**：`mataof run/retrieve/stats` CLI、JSON 配置、可编程 Runner、
   每次运行的完整追踪落盘。

---

## 四、核心设计差异（三点）

### 1. 优化对象：改写 SQL → 选择策略（混合模型）

- MAPO 的核心是**自己生成改写后的 SQL 候选**（谓词重排、时间范围缩减、测点裁剪、
  LLM 改写……），候选本身就是可执行 SQL，选出来直接执行；
- MATAOF 采用**混合输出**：候选策略可携带候选提供方给出的等价 SQL（如 Filter Order
  的 WHERE 重排），决策输出 = strategy_id + 等价 SQL（可直接执行）+ 执行级参数
  （分区/chunk 裁剪、聚合位置等无法用 SQL 语法表达的维度）；
- 差异的本质：**"生成 SQL"与"选择 SQL"解耦**——MATAOF 决策 Agent 自身不生成/改写
  SQL（保持可验证、不越权），SQL 由候选提供方（外部系统，可以是 MAPO 式改写器或
  LLM）生成并随候选携带；MAPO 的两步（生成+选择）在 MATAOF 中分属候选提供方与
  决策 Agent，职责边界更清晰。

### 2. 决策依据：学习代价模型 → 可验证证据链

- MAPO 用 Bao 学习模型（TreeCNN 代价预测 + RandomForest 路由）与启发式打分
  （ω 权重依赖 EXPLAIN ANALYZE 间接指标——即其已知不足①）；
- MATAOF 的每个决策都可还原到具体依据：`evidence` 字段列出实际使用的特征条目、
  数据库状态、系统状态、历史记录（含相似度）；`reason` 引用具体数值；置信度是
  确定性公式。
- 差异的本质：MAPO 追求**预测精度**（黑盒学习）；MATAOF 优先**可验证性与安全性**
  （白盒规则），以牺牲学习能力的提升潜力为代价。

### 3. 知识形态：全局统计模型 → 显式结构化历史

- MAPO 的经验沉淀在 Bao 模型的参数与 Arm 黑名单里，不可直接解释，也无法按查询
  相似性定向检索；
- MATAOF 的历史是逐条保留的结构化记录（Query + Features + Context + Strategy +
  Execution + Result + Timestamp），append-only 不删除，支持三层相似匹配与成功/
  失败模式提取，单次异常不会否定历史（累计评价）。
- 差异的本质：MAPO 的"记忆"是隐式的；MATAOF 的"记忆"是显式、可审计、可消融的。

---

## 五、对 MAPO 三大已知不足（conclusion.md）的回应

| MAPO 已知不足 | MATAOF 的处理 |
|---|---|
| ① ω 代价权重依赖 EXPLAIN ANALYZE 间接指标启发式估算，换硬件需重标定 | **不再估算代价权重**：任何选择率/扫描量数据必须由数据库显式提供，缺失即 unknown；决策只使用可验证数据，不存在需要标定的参数 |
| ② 冷启动需额外 EXPLAIN ANALYZE 开销收集训练数据 | **零训练、零预热**：无学习模型；冷启动直接走安全 baseline/fallback，随执行反馈累积证据后逐步放开（这是 MATAOF 的安全路径设计） |
| ③ 时间裁剪受限于 IoTDB 内核接口，分区/chunk 级过滤无法通过 SQL 显式控制 | **能力边界显式化**：不再通过改写 SQL 去触发内核能力；执行层通过"候选策略声明 + 需求声明"表达支持范围（如 chunk 级过滤要求数据库提供物理组织信息），能力不存在时门控排除，不产生无法执行的策略 |

---

## 六、适用边界

- **MAPO/ADEPT**：训练数据充足、负载稳定、追求最大性能收益的在线场景；其学习
  模型的精度潜力高于 MATAOF v1 的规则系统。
- **MATAOF**：强调安全、可解释、可审计的场景；冷启动即用；天然适配多智能体协作、
  消融实验与错误分析；v1 的确定性规则是底座，后续可将学习组件（如 Bao 类代价
  模型、LLM 特征增强）以"提供数据而非做决策"的方式接入各 Agent 的输入契约。
