# MATAOF 任务摘要（唯一权威的当前状态）

> 后续执行任务以此为准；其余文档为规格参考（docs/ 下各 Agent 规格与
> optimization-mechanism.md）或论文素材（design-gap-analysis、comparison-mapo-mataof）。
> 最后更新：2026-10-30

## 1. 当前目标

以 Apache IoTDB 为实验平台，**产出开题报告正式对照数据**，支撑论文撰写：
- Baseline 1：native（原生 IoTDB，客户端计时同口径）
- Baseline 2：fixed（固定策略）
- 2×2 主对照（控制流 × LLM）：
  - **P0** `pipeline`：固定流水线，无 LLM
  - **P1** `pipeline-llm`：固定流水线 + 共享 LLM
  - **A0** `agent`：四 Agent 观察驱动闭环，无 LLM
  - **A1** `llm`：四 Agent 观察驱动闭环 + 共享 LLM（Ours 主实验）

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
   **多模式对照必须执行缓存协议**（每模式前重启 IoTDB + 清 OS 页缓存，
   见 run-experiment.md §1.3）

## 3. 已确认决策（不再反复讨论）

| 决策 | 结论 |
|---|---|
| 决策输出 | 混合模型：strategy_id + Agent 内置候选生成器的 equivalent_sql + 执行级参数（决策只选择、不生成 SQL） |
| baseline 机制 | **不逐条配对双执行**（measure_baseline=false，每条只执行优化后语句）；baseline 由 native 轮承担，跨轮对比 |
| LLM 运行模式 | llm 变体：`decision_mode="llm_primary"` 同步决策——特征化量化 prompt（16500 条抽样仅 69 个唯一 prompt）+ 输出缓存（全量可行）；`agents.llm_shared=true`（LLM 常驻）：OD 决策 1 次 + QA 按 `llm_policy` 可选 +1 + 执行后监控解读按 EM `llm_policy=on_anomaly` 可选 +1 + KM 经验总结按查询形状收口（同一形状只调用 1 次，首条 +1）；稳态正常查询 1 次、单条最多 3 次；`llm_shared=false` 为“仅 OD 用 LLM”消融；agent 变体：无 LLM |
| 策略空间 | alignment（time_aligned / align_by_device）+ fetch_size（fetch_default / fetch_large）——全部有实测杠杆背书（scripts/bench_sql_levers.py）；无杠杆或语义不等价的改写一律不做 |
| 执行计划（Agent 内置） | 不扩展决策维度目录；决策 Agent 在 `decide()` 内生成并选择执行计划：baseline/exec_mode/sql_rewrite/时间·设备·混合分区/部分聚合；硬门禁保证可执行，等价性分 exact / content_equivalent，执行失败回退原查询；默认随 Agent 生效，见 plan-space.md |
| 知识库策略 | 每个实验模式独立库（pipeline / pipeline-llm / agent / llm / fixed），**同模式内跨规模共享**（dbs/dbm/dbl 同库，经验跨数据集迁移）+ **跨模式隔离**（对照证据不污染）；SQLite 签名聚合后端 |
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
- **指标**：决策开销分阶段（分析/检索/决策/监控/执行后 LLM/入库 + 端到端 +
  净开销）与执行耗时分开记录；执行后 LLM 与知识入库分开计时（合并 LLM
  调用不再计入 `knowledge_update_ms`）；native 客户端计时同口径、汇总同格式
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
  **运行时默认关闭**（`agent.enabled=false`），关闭时与旧路径完全一致；
  配置矩阵中 A0/A1 启用，pipeline 模式以 `agents.control="pipeline"` 关闭
  闭环（组件与 LLM 调用点保留）；LLM 只参与首次决策，重规划不额外调用。
  运行语义、配置与消融见 agent-runtime.md
- **四 Agent 全部运行时化（2026-10-29）**：Query Analysis / Execution Monitoring /
  Knowledge Memory 与 OD 共用 `mataof/runtime`，各自形成观察驱动的有界闭环：
  - QA：确定性分析为固定第一步，仅在解析失败/类型 unknown/低置信度/tag-attribute
    歧义等**证据缺口**出现时调用 LLM 标注（`llm_policy=on_gap` 默认）；
  - EM：事实流水线（归一化 → 按需比较 → 评估 → 异常 → 按需解读），无 baseline/
    无有效指标时**跳过比较并记录原因**，LLM 仅在真实异常或 failed/timeout 时调用
    （degraded 由确定性链路记录，`on_anomaly_or_degraded` 保留为消融）；
  - KM：自适应 top-K 检索，仅在"已列出的强匹配不足且仍有未展开记录"时扩 K，
    受轮数/上限/截止时间约束，LLM 解读只做一次；检索失败输出 null 证据而非 0 匹配；
  - Runner：新增 `agents` 配置节（全局 enabled + 单 Agent 覆盖，旧 `agent` 键仍
    兼容且仅启用 OD）；trace 新增按名称索引的 `trace["agents"]`（`trace["agent"]`
    保留为 OD 元数据），run_log 新增 `agents_enabled` / `agent_steps_total`。
  四个 Agent 运行时本身默认关闭，可整体或单独消融；`scripts/gen_configs.py`
  生成的 P0/P1/A0/A1 四套配置均启用（`agents.enabled=true`，P1/A1 另加
  `agents.llm_shared=true`），仅 fixed 配置关闭；语义、配置、trace
  与消融见 agent-runtime.md
- **LLM 常驻（B，2026-10-29；方案一 2026-10-30）**：`agents.llm_shared=true`
  时同一 LLM 客户端按各 Agent 的 `llm_policy` 共享。OD 决策仍每次 1 次；QA
  在证据缺口/`always` 时 +1；执行后监控解读默认 `llm_policy=on_anomaly`，
  仅真实异常或 failed/timeout 时 +1（degraded 由确定性链路记录）；
  KM 经验总结按查询形状收口（`experience_key`：同形状 + 策略 + 数据规模，
  不含精确 time_span），同一形状首次执行调用 1 次、后续复用，不再逐执行
  生成。稳态正常查询 1 次、单条最多 3 次；未触发时写诚实占位
  （`available=false` + 原因），不伪造解读。`llm_shared=false` 保留
  “仅 OD 用 LLM”消融路径；`on_anomaly_or_degraded` 保留为消融策略
- **2×2 对照与行为剖面（P0，2026-10-30）**：`agents.control` 新增
  （`"agent"` 默认 / `"pipeline"`），Runner 以 `_pipeline_mode` 分流——
  pipeline 单次决策/执行、固定 top-K（KM `max_rounds=1`）、不 replan/recall；
  agent 保留观察驱动闭环。`--mode` 提供
  `native | fixed | pipeline | pipeline-llm | agent | llm` 六模式，
  `scripts/gen_configs.py` 生成五套独立知识库/结果目录。run_log 新增
  `control_flow`、`llm_enabled`、`llm_shared`、`decision_mode`、
  `agent_action_hash/count/counts`、`agent_steps_by_agent`、`agent_terminations`、
  `agent_triggers`（replan/recall/qa_em_km_llm/km_note_reused/km_adaptive_expand/
  od_llm_decision）、`bus_intents`；`scripts/agent_profile.py` 汇总控制流、
  动作序列、触发率、LLM 参与率与决策开销，用于回答“Agent 是否真的在行动”。
  `tests/test_pipeline_control.py` 5 例验证：pipeline 不 replan、agent 会
  replan、P1/A1 调用点相同但控制流不同、KM 固定 top-K、剖面脚本可消费 run_log
- **主链路 AgentBus 接线（2026-10-29）**：任一 Agent 启用时，Runner 为每条查询
  构建进程内同步总线，QA/KM/EM 以端点注册，主流程经 `AgentMessage` 请求；
  OD 在初始证据为空时以 `recall` 意图向 KM 请求一次更宽检索（按 record_id 去重
  合并，失败不伪造证据）；执行/监控请求的 sender 为 `optimization_decision`。
  端点按配置选择 Agent 或确定性实现，单 Agent 消融不改变消息拓扑；处理器异常
  返回 `intent="error"` 响应，调用方回退直接实现，查询继续。trace 新增
  `trace["bus"]`（请求/响应摘要 + 关联键 + 错误），run_log 新增 `bus_messages` /
  `bus_errors`。全部关闭时不创建总线，行为与旧路径一致
- **测试**：476 个自动化测试通过 + 1 跳过（含真实 IoTDB 集成、向量化等价性、
  签名聚合等价性、LLM 决策合法性校验、进程退出回归、近邻查询形状区分与
  同形状证据选择；计划能力新增 36 个语法/生成/成本/Agent 生成测试 +
  11 个合并/执行器测试 + 9 个同形状参照测试；原生双口径 execute_raw
  与设备清单完整性校验新增 14 个测试；运行时、四 Agent 闭环与 LLM 共享路由
  新增 111 个测试，含主链路消息流转、OD→KM 补充证据、recall 开关、端点异常
  回退，以及 `llm_shared` 的调用预算、`on_anomaly` 触发矩阵、形状级经验复用
  与诚实跳过语义；另含 5 个控制流对照测试：pipeline 不 replan/agent replan、
  P1/A1 同调用点、KM 固定 top-K、行为剖面脚本）
- **公平对比口径**：native 支持 `--result-mode row|df` 双口径（df 与 agent
  计划同为 DataFrame 批量消费，输出 `results_baseline_df/`）；设备清单由
  `scripts/gen_device_inventory.py` 生成（SHOW+COUNT 双校验），运行期
  `validate_device_inventory` 再按模式核对设备数，防止设备分区静默丢数据

## 5. 实验数据现状

> 以下历史数据早于 2×2 控制流矩阵（当时的 agent 配置未区分 pipeline/agent），
> 仅用于口径与量级参考；正式对照需按新六模式重跑。

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

**dbs 全量 LLM 模式（24,000 条，2026-10-30，run `round-20261030-112023`）**：

- 墙钟 1,117.61s（21.47 qps），较上一轮 14,679.58s（1.63 qps）提升 13.1×；
  真实 LLM 调用 136 次、输出缓存命中 25,957 次；`post_llm_ms` 合计 174.62s
  （15.6% 墙钟）、`knowledge_update_ms` 合计 32.23s（2.9%）——LLM 推理已不再
  计入入库成本。
- 效果：improved 15,411 / degraded 4,988 / unchanged 3,601，24,000 条全部
  success；四 Agent 全部启用，每查询 4 条总线消息（1,642 条重规划查询为 5 条），
  `bus_errors=0`，agent 步数 6~9。
- 协作触发面：QA 仅在 q8 的 2,000 条证据缺口调用 LLM；EM 仅 74 条真实异常
  触发解读（其余按 `on_anomaly` 跳过）；KM 19 个形状键、19 次生成 + 23,981 次
  复用；OD 22,358 条最终决策含 `llm_selected`，1,642 条输出重规划（确定性，
  按设计不再调 LLM），OD→KM recall 2 次（均成功补 200 条证据）。
- 该轮跑在 2026-10-30 的两处修复之前：EM 的 74 条异常解读因数字校验漏掉
  anomalies 字段被全部误拒（已修复，样例现可通过）；OD 的 `llm_analysis`
  在 `llm_primary` 下误写"LLM 未启用"（已改为显式标注 `decision_mode` /
  `llm_selected_dimensions`）。这两项只影响附加解读与 trace 标注，不影响
  决策、评估与知识入库，故本轮对照数据仍有效。

## 6. 未解决问题

1. **三规模完整 2×2 对照未跑**（P0/P1/A0/A1 + native/fixed；当前仅 dbl
   1200 条旧三模式小样本，需按新矩阵重跑；dbs/dbm 及全量 66k 未跑）
2. **D5 模块消融开关**未实施（开题报告 3.4.5）
3. **D6 动态工作负载、D7 策略追踪（oracle）**未实施（设计见 design-gap-analysis）
4. git：工作区有大量未提交改动（代码+配置+文档；results/data 不入 git，
   建议 .gitignore）；旧备份 data/knowledge_llm.db.v1.bak 可归档删除
5. 论文撰写：实验报告文档（2×2 + 基线对照数据 + 口径 + 图表）未产出
6. **多 Agent 协作的边界**：主链路已接入 AgentBus（检索/补充证据/监控/入库均
   经消息路由），但仍是单查询内的**同步请求-响应**，由 Runner/OD 发起；
   QA/EM/KM 不会主动发消息，没有并发协商或广播。OD 的 probe 动作、尝试级
   负证据入库也尚未实现

## 7. 下一步计划（按序执行）

1. **git 整理提交**（代码 + 配置 + 文档；results/data 加 .gitignore）
2. **三规模正式对照**（缓存协议 + 大样本/全量，六模式
   `native/fixed/pipeline/pipeline-llm/agent/llm`，每轮输出行为剖面）
3. **D5 消融开关**（QA/EM/KM 剥离配置化）
4. **Agent 主动能力**：OD 增加 probe 动作或可注入的 LLM 策略（保持有界与确定性
   兜底）；尝试级失败证据入库；按需评估消息驱动的并发/广播（当前同步总线已
   满足单查询闭环）
5. D6/D7 按论文进度
6. **论文撰写支撑**：实验报告（对照表、口径说明、break-even 曲线、决策开销
   分解图）
