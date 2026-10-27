# MATAOF 任务摘要（唯一权威的当前状态）

> 后续执行任务以此为准；其余文档为规格参考（docs/ 下各 Agent 规格与
> optimization-mechanism.md）或论文素材（design-gap-analysis、comparison-mapo-mataof）。
> 最后更新：2026-10-26

## 1. 当前目标

以 Apache IoTDB 为实验平台，**产出开题报告三组对照实验的正式数据**，支撑论文撰写：
- Baseline 1：native（原生 IoTDB，客户端计时同口径）
- Baseline 2：fixed（固定策略）
- Ours：agent（确定性链路）+ llm（LLM 参与策略选择，确定性兜底）

## 2. 核心约束（不可违反）

1. 一切决策以查询信息/数据库状态/历史记录/运行时反馈为依据；无法确定 → `unknown`，
   **绝不编造**统计/指标/结果
2. 有界策略空间：候选 = 输入候选 ∩ 固定目录（**alignment + fetch_size 两维度
   4 策略**，见 optimization-mechanism.md）；禁止自造；每个决策可追踪
3. 不改数据库内核/源码；系统需**数据库无关**（策略为概念级，SQL 由候选提供方
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
| 决策输出 | 混合模型：strategy_id + 候选提供方的 equivalent_sql + 执行级参数（决策只选择、不生成 SQL） |
| baseline 机制 | **不逐条配对双执行**（measure_baseline=false，每条只执行优化后语句）；baseline 由 native 轮承担，跨轮对比 |
| LLM 运行模式 | llm 变体：`decision_mode="llm_primary"` 同步决策——特征化量化 prompt（16500 条抽样仅 69 个唯一 prompt）+ 输出缓存（全量可行）；每条查询恰好 1 次 LLM 调用（QA/监控解读关闭）；agent 变体：无 LLM |
| 策略空间 | alignment（time_aligned / align_by_device）+ fetch_size（fetch_default / fetch_large）——全部有实测杠杆背书（scripts/bench_sql_levers.py）；无杠杆或语义不等价的改写一律不做 |
| 知识库策略 | 统一知识库：**跨规模共享**（dbs/dbm/dbl 同库，经验跨数据集迁移）+ **跨模式隔离**（fixed/agent/llm 各库，对照证据不污染）；SQLite 签名聚合后端 |
| 效果评估口径 | 无逐条 baseline → 效果分布如实为 insufficient_evidence；策略效果证据来自跨轮对比与知识库累计统计 |
| 配置管理 | `scripts/gen_configs.py` 是唯一事实来源（改默认参数后重新生成全部配置） |
| 汇报口径 | 批次=文件；本轮总体=调用；轮次=run_id 过滤；native 汇总与 agent/llm 同格式（客户端墙钟同尺子） |

## 4. 当前架构状态（已完成）

**四 Agent 闭环**：分析（特征提取）→ 知识检索（签名聚合证据，按执行数加权）
→ 决策（确定性评分 + LLM 选择，合法性校验，确定性兜底）→ 执行（equivalent_sql
/ 执行参数真实送达）→ 监控 → 知识入库（评价表 + 签名聚合增量）→ 落盘。

- **策略真正改变执行**：候选提供方（provider.py）按查询生成 ALIGN BY DEVICE
  改写（仅一级通配路径——显式设备列表被引擎拒绝）与 fetch_size=10000 参数
  （执行层多会话消费）；实测方向：time_aligned 护栏（选错 8~25× 降级）、
  fetch 中性偏正
- **LLM 决策**：特征化量化 prompt + 经验摘要（评价表分桶）→ LLM 的决策消费
  已学知识；输出缓存高命中（69 个唯一 prompt）；失败/非法兜底确定性
- **知识库**：SQLite 签名聚合（118k 执行 → 24.6k 签名，文件 140→57.8MB，
  冷加载 0.9s）；检索 O(唯一签名) ~4.5ms@48k；决策路径检索与展示聚合解耦
  （summary=False）；增量重建只处理新增签名
- **指标**：决策开销分阶段（分析/检索/决策/监控/入库 + 端到端 + 净开销）与
  执行耗时分开记录；native 客户端计时同口径、汇总同格式
- **语料**：q1~q12 重查询改写（全数据域/全设备，执行 3~890ms，dbl 平均 ~30ms），
  q12 为通配宽扫描；生成器 scripts/gen_heavy_queries.py / gen_wildcard_queries.py
- **进程健壮性**：补写线程池 daemon（退出不卡）、trace 原子回写
- **测试**：267 个自动化测试通过（含真实 IoTDB 集成、向量化等价性、签名聚合
  等价性、LLM 决策合法性校验、进程退出回归）

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

## 6. 未解决问题

1. **三规模完整对照未跑**（当前仅 dbl 1200 条小样本；dbs/dbm 及全量 66k 未跑）
2. **D5 模块消融开关**未实施（开题报告 3.4.5）
3. **D6 动态工作负载、D7 策略追踪（oracle）**未实施（设计见 design-gap-analysis）
4. git：工作区有大量未提交改动（代码+配置+文档；results/data 不入 git，
   建议 .gitignore）；旧备份 data/knowledge_llm.db.v1.bak 可归档删除
5. 论文撰写：实验报告文档（三组对照数据 + 口径 + 图表）未产出

## 7. 下一步计划（按序执行）

1. **git 整理提交**（代码 + 配置 + 文档；results/data 加 .gitignore）
2. **三规模正式对照**（缓存协议 + 大样本/全量，native/fixed/agent/llm 四模式）
3. **D5 消融开关**（QA/EM/KM 剥离配置化）
4. D6/D7 按论文进度
5. **论文撰写支撑**：实验报告（对照表、口径说明、break-even 曲线、决策开销
   分解图）
