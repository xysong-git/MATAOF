# MATAOF 任务摘要（唯一权威的当前状态）

> 后续执行任务以此为准；其余文档为规格参考（docs/ 下各 Agent 规格与
> optimization-mechanism.md）或论文素材（design-gap-analysis、comparison-mapo-mataof）。
> 最后更新：2026-10-29

## 1. 当前目标

以 Apache IoTDB 为实验平台，**产出开题报告三组对照实验的正式数据**，支撑论文撰写：
- Baseline 1：native（原生 IoTDB，客户端计时同口径）
- Baseline 2：fixed（固定策略）
- Ours：agent（确定性链路）+ llm（LLM 参与策略选择，确定性兜底）

## 2. 核心约束（不可违反）

1. 一切决策以查询信息/数据库状态/历史记录/运行时反馈为依据；无法确定 → `unknown`，
   **绝不编造**统计/指标/结果
2. 有界策略空间：候选 = 输入候选 ∩ 固定目录（**alignment + fetch_size 两维度
   4 策略**，见 optimization-mechanism.md）；禁止自造；每个决策可追踪。
   **执行计划在决策 Agent 内生成并选择**：`decide()` 按 query/QA/设备清单把
   查询编译为可执行计划（子查询 + 合并规格），按确定性成本模型选择，
   计划族/门禁/成本公式见 plan-space.md；计划候选同样来自固定计划族，
   不生成目录外计划；`planning.enabled=false`/空候选仅用于消融
3. 不改数据库内核/源码；系统需**数据库无关**（策略为概念级，SQL 由 Agent 内置候选生成器
   按库方言生成，换库只换适配器）
4. 知识库 = **append-only 执行日志 + 派生聚合签名索引**（SQLite v2）：执行事实
   只追加不修改（可审计）；同质经验按签名归并（聚合统计为派生数据，可重算）；
   单次结果不当规律
5. **LLM 参与决策（decision_mode="llm_primary"）**：LLM 从候选选择策略，只做
   合法性校验（候选 ∩ 目录、维度适用、需求满足；equivalent_sql 一律取自候选
   提供方——决策只选择、不生成 SQL）；LLM 失败/非法 → 确定性决策兜底。
   证据与置信度始终来自确定性链路（LLM 不给证据，防编造）
6. 实验可复现：种子/run_id/口径统一；分位数只在批次/轮次汇总生成；
   **三组对照必须执行缓存协议**（每模式前重启 IoTDB + 清 OS 页缓存，
   见 run-experiment.md §1.3）

## 3. 已确认决策（不再反复讨论）

| 决策 | 结论 |
|---|---|
| 决策输出 | 混合模型：strategy_id + Agent 内置候选生成器的 equivalent_sql + 执行级参数（决策只选择、不生成 SQL） |
| baseline 机制 | **不逐条配对双执行**（measure_baseline=false，每条只执行优化后语句）；baseline 由 native 轮承担，跨轮对比 |
| LLM 运行模式 | llm 变体：`decision_mode="llm_primary"` 同步决策——特征化量化 prompt（16500 条抽样仅 69 个唯一 prompt）+ 输出缓存（全量可行）；每条查询恰好 1 次 LLM 调用（QA/监控解读关闭）；agent 变体：无 LLM |
| 策略空间 | alignment（time_aligned / align_by_device）+ fetch_size（fetch_default / fetch_large）——全部有实测杠杆背书（scripts/bench_sql_levers.py）；无杠杆或语义不等价的改写一律不做 |
| 执行计划（Agent 内置） | 不扩展决策维度目录；决策 Agent 在 `decide()` 内生成并选择执行计划：baseline/exec_mode/sql_rewrite/时间·设备·混合分区/部分聚合；硬门禁保证可执行，等价性分 exact / content_equivalent，执行失败回退原查询；默认随 Agent 生效，见 plan-space.md |
| 知识库策略 | 统一知识库：**跨规模共享**（dbs/dbm/dbl 同库，经验跨数据集迁移）+ **跨模式隔离**（fixed/agent/llm 各库，对照证据不污染）；SQLite 签名聚合后端 |
| 效果评估口径 | 不逐条双执行；监控以**历史实测参照**判断效果（参照首先限定在"同查询形状 + 同数据规模"且样本 ≥3 的实测子集——除时间跨度允许 10% 波动外含测点指纹/窗口间隔/聚合函数完全一致；不足才退回全部相似记录；子集内 baseline 计划历史样本充分时优先——≥3 次且 ≥ 同形状全部计划证据的 10%；样本稀少/冷启动时用同计划历史；再次为相似历史）→ 已见查询输出 improved/unchanged/degraded，仅无任何实测历史的全新查询为 insufficient_evidence；参照只取实测值，不用成本先验；参照来源、样本量与 scope 写入 assessment_basis/monitoring_reference；评价表成功率分母只含方向明确的执行（improved/degraded/failed），unchanged 为中性不计入 |
| 决策成本口径 | 单条基线 `single_ms` 与监控同口径，且优先只使用"同查询形状 + 同数据规模"的实测子集（≥3 次；近邻形状 q3/q10、q7/q11 不计入）：baseline 历史样本充分才采用；否则用同形状其他计划的实测典型值（≥3 次，`history_plan_typical`）；稀疏 cold baseline 只记录证据、不修正预测；仍无实测 → 数据库状态/结构化先验（标注 prior） |
| 配置管理 | `scripts/gen_configs.py` 是唯一事实来源（改默认参数后重新生成全部配置） |
| 汇报口径 | 批次=文件；本轮总体=调用；轮次=run_id 过滤；native 汇总与 agent/llm 同格式（客户端墙钟同尺子） |

## 4. 当前架构状态（已完成）

**四 Agent 闭环**：分析（特征提取）→ 知识检索（签名聚合证据，按执行数加权）
→ 决策（确定性评分 + LLM 选择，合法性校验，确定性兜底）→ 执行（equivalent_sql
/ 执行参数真实送达）→ 监控 → 知识入库（评价表 + 签名聚合增量）→ 落盘。

- **策略真正改变执行**：Agent 内置候选生成器（provider.py）按查询生成 ALIGN BY DEVICE
  改写（显式多设备列表与一级通配路径均可；完整测点路径谓词会被引擎拒绝，
  已作为门禁）与 fetch_size=10000 参数（执行层多会话消费）；实测方向：
  time_aligned 护栏（选错 8~25× 降级）、fetch 中性偏正
- **Agent 内置执行计划**：决策 Agent 的 `decide()` 调用内置 generator，把查询
  编译为 baseline/exec/sql_rewrite/时间·设备·混合分区/部分聚合计划（q1~q12 在
  2~18 个候选），语法硬校验 + 结构门禁保证可执行；cost.py 用历史基线/数据库
  状态/结构先验预测耗时并按收益门槛选择，baseline 恒为安全动作；执行器并行
  子查询、按合并规格组装、失败回退原查询并记录成本；真实 IoTDB 2.0.6 对
  q1~q12 全部 exact 计划逐一验证等价（详见 plan-space.md）
- **LLM 决策**：特征化量化 prompt + 经验摘要（评价表分桶）→ LLM 的决策消费
  已学知识；输出缓存高命中（69 个唯一 prompt）；失败/非法兜底确定性
- **知识库**：SQLite 签名聚合（118k 执行 → 24.6k 签名，文件 140→57.8MB，
  冷加载 0.9s）；检索 O(唯一签名) ~4.5ms@48k；决策路径检索与展示聚合解耦
  （summary=False）；增量重建处理新增签名，同签名追加执行时按脏签名增量
  刷新检索缓存（count/平均延迟不会停留在首次执行）
- **指标**：决策开销分阶段（分析/检索/决策/监控/入库 + 端到端 + 净开销）与
  执行耗时分开记录；native 客户端计时同口径、汇总同格式
- **语料**：q1~q12 重查询改写（全数据域/全设备，执行 3~890ms，dbl 平均 ~30ms），
  q12 为通配宽扫描；生成器 scripts/gen_heavy_queries.py / gen_wildcard_queries.py
- **进程健壮性**：补写线程池 daemon（退出不卡）、trace 原子回写
- **Agent 运行时 + 决策闭环（2026-10-29）**：新增零依赖运行时 `mataof/runtime/`
  （ToolRegistry 参数校验/异常包装、Goal 步数与时间预算、Act/Finish/Abort、
  Observation、AgentContext、有界控制环、AgentBus 消息请求-响应）。Optimization
  Decision Agent 新增 `run()`：决策 → 通过 `execute_decision` 工具执行 → 观察
  监控反馈 → degraded/failed 时排除该 plan 后确定性重规划 → 达标/预算耗尽/
  baseline 失败/无替代时终止；所有尝试、终止原因与逐步 trace 写入
  `trace["agent"]`，run_log 增加 `agent_steps/agent_attempts/agent_terminated_by`。
  **默认关闭**（`agent.enabled=false`），关闭时与旧路径完全一致；LLM 只参与首次
  决策，重规划不额外调用。运行语义、配置与消融见 agent-runtime.md
- **测试**：386 个自动化测试通过 + 1 跳过（含真实 IoTDB 集成、向量化等价性、
  签名聚合等价性、LLM 决策合法性校验、进程退出回归、近邻查询形状区分与
  同形状证据选择；计划能力新增 36 个语法/生成/成本/Agent 生成测试 +
  11 个合并/执行器测试 + 9 个同形状参照测试；原生双口径 execute_raw
  与设备清单完整性校验新增 14 个测试；运行时与 OD 闭环新增 29 个测试）
- **公平对比口径**：native 支持 `--result-mode row|df` 双口径（df 与 agent
  计划同为 DataFrame 批量消费，输出 `results_baseline_df/`）；设备清单由
  `scripts/gen_device_inventory.py` 生成（SHOW+COUNT 双校验），运行期
  `validate_device_inventory` 再按模式核对设备数，防止设备分区静默丢数据

## 5. 实验数据现状

**dbl 小样本对照（1200 条 × 3 模式，冷启动协议，2026-10-26）**：

| 模式 | 执行延迟 avg/P50 | 端到端 avg | 总耗时 |
|---|---|---|---|
| native | 30.56 / 25.27ms | 30.56ms | 37.75s |
| agent | 31.39 / 24.86ms | 38.41ms | 49.97s |
| llm | 30.87 / 25.70ms | 53.84ms（P50 36.06） | 74.03s |

结论（诚实口径）：
- 执行延迟三模式统计同分布（±1-3%）——策略语义 = 护栏 + 中性，**无执行加速**
- agent 智能税 +21%（6.6ms 决策开销）；执行 ≥120ms 的重查询（q12 级）上税
  占比降到 ~3-5%——break-even 曲线方向正确
- 论文主线建议：自适应决策机制 + 学习闭环 + 护栏 + 开销可控性（不主张执行加速）

**口径修正（2026-10-29，双口径实测）**：上表 native 走逐行消费（`has_next()/
next()`，历史口径），而 agent 的所有计划（含 `baseline_single_df`）走
DataFrame 批量消费（`has_next_df()/next_df()`）——同一 SQL、同一客户端交替
重复实测，仅消费方式差异即可造成数倍差距（dbl：q12 120.4→41.4ms、q2
40.0→4.5ms、q9 33.7→3.9ms；q1/q3~q6 差异 <7%，结果集形状一致）。因此上表
"执行延迟同分布"是**跨消费口径比较**，不能作为 agent 无加速的证据，跨模式
对比必须统一用 `--result-mode df`（输出 `results_baseline_df/`）。该公平口径
下 dbl 12 个形状 native 中位平均 ~8.5ms/条 vs agent 端到端 ~25.3ms/条
（~3.0×，固定决策开销主导）；q12 级宽结果查询上计划收益可覆盖开销
（2 个以上宽测点时设备分区 1.5~2.5×），是当前 break-even 的切入点。

## 6. 未解决问题

1. **三规模完整对照未跑**（当前仅 dbl 1200 条小样本；dbs/dbm 及全量 66k 未跑）
2. **D5 模块消融开关**未实施（开题报告 3.4.5）
3. **D6 动态工作负载、D7 策略追踪（oracle）**未实施（设计见 design-gap-analysis）
4. git：工作区有大量未提交改动（代码+配置+文档；results/data 不入 git，
   建议 .gitignore）；旧备份 data/knowledge_llm.db.v1.bak 可归档删除
5. 论文撰写：实验报告文档（三组对照数据 + 口径 + 图表）未产出
6. **其余三个 Agent 尚未接入运行时**：Query Analysis / Execution Monitoring /
   Knowledge Memory 仍是纯函数模块；下一步可接入工具化与 AgentBus（检索/监控
   反馈消息驱动），并按需要增加"主动探测"等动作；当前闭环能力仅 OD 具备

## 7. 下一步计划（按序执行）

1. **git 整理提交**（代码 + 配置 + 文档；results/data 加 .gitignore）
2. **三规模正式对照**（缓存协议 + 大样本/全量，native/fixed/agent/llm 四模式）
3. **D5 消融开关**（QA/EM/KM 剥离配置化）
4. **其余 Agent 运行时化**：QA/EM/KM 接入运行时与 AgentBus；OD 增加 probe 动作
   或 LLM 策略（保持有界与确定性兜底）
5. D6/D7 按论文进度
6. **论文撰写支撑**：实验报告（对照表、口径说明、break-even 曲线、决策开销
   分解图）
