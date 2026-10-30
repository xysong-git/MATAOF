# MATAOF 会话总结与后续计划（2026-10-30）

> 覆盖本轮会话：P0/P1/A0/A1 的 2×2 控制流改造、四模式冒烟、A1 知识库污染定位、
> 经验库语义讨论与决策。
> 相关文档：`docs/run-experiment.md`（运行与缓存协议）、`docs/active-plan.md`
> （路线图）、`docs/agent-runtime.md`（Agent 运行时）。

## 0. 结论速览

- 已完成 2×2 控制流改造：`--mode pipeline|pipeline-llm|agent|llm`，另保留
  `native`、`fixed` 基线；旧“固定链路”现在叫 `pipeline`，不再占用 `agent` 名字。
- 四模式各跑 1,200 条（仅 dbs，12 文件 × 100）冒烟通过：pipeline 无
  replan/recall/扩 K；agent 有 6.2% replan、0.1% recall、0.4% 扩 K；A1 的
  21.8% replan 是旧知识库导致的假级联，**不作为有效结果**。
- 已定位 A1 问题根因：`data/knowledge_llm.db` 有 100,700 条旧 dbs 记录，
  同签名新旧执行绝对延迟从 6.70ms 漂到 13.04ms；`reference_time=null` 使时效
  衰减失效，监控 5% 阈值把旧参照下的当前值大量判成 degraded。
- 已讨论“为什么要多个经验库”：五个库是按**实验组**隔离，不是按数据集/规模；
  同一组内 dbs/dbm/dbl 共用一个库。用户决定暂不改经验库设计，保留现状。
- 尚未提交任何 commit；工作区有 56 个文件改动（含 `.pyc`），提交前需清理。

---

## 1. 本次会话已完成

### 1.1 2×2 控制流架构落地（P0）

引入配置键 `agents.control`，取值 `"agent"`（默认）或 `"pipeline"`；非法值
回退 `agent` 并打印警告。运行时据此选择控制流，而不是靠“有没有 LLM”猜测。

| 模式 | 控制流 | LLM | 配置 | 知识库 | 结果目录 |
|---|---|---|---|---|---|
| `native` | 无 Agent | 无 | — | — | `results_baseline/`（`--result-mode df` 时为 `results_baseline_df/`） |
| `fixed` | 固定策略基线 | 无 | `config.{scale}.fixed.json` | `data/knowledge_fixed.db` | `results/round_fixed_{scale}/` |
| `pipeline`（P0） | 固定流水线 | 无 | `config.{scale}.pipeline.json` | `data/knowledge_pipeline.db` | `results/round_pipeline_{scale}/` |
| `pipeline-llm`（P1） | 固定流水线 | 共享 LLM | `config.{scale}.pipeline-llm.json` | `data/knowledge_pipeline_llm.db` | `results/round_pipeline_llm_{scale}/` |
| `agent`（A0） | 四 Agent 闭环 | 无 | `config.{scale}.json` | `data/knowledge_agent.db` | `results/round_agent_{scale}/` |
| `llm`（A1，主实验） | 四 Agent 闭环 | 共享 LLM | `config.{scale}.llm.json` | `data/knowledge_llm.db` | `results/round_{scale}_llm/` |

控制流差异（组件和 LLM 调用点相同，差异只在闭环逻辑）：

- **Pipeline**：单次决策 + 单次执行；KM 固定 top-K（`max_rounds=1`）；OD 不
  replan；OD 不向 KM 发起 recall；KM 不做自适应 top-K 扩展。
- **Agent**：OD 在 degraded/failed 时 replan；初始历史证据为空时 OD 向 KM
  recall；KM 在强证据不足且仍有截断时扩 top-K。
- 相关实现：`mataof/runner.py`（`_agents_control`、`_control_flow_label()`、
  `_retrieve_knowledge(..., fixed_topk=...)`、决策分支）；OD 元数据补充
  `llm_used_for_decision`，避免确定性 replan 覆盖后漏报 LLM 参与。

run_log 行为剖面字段：

- `control_flow`、`llm_enabled`、`llm_shared`、`decision_mode`；
- `agent_action_hash`、`agent_action_count`、`agent_action_counts`；
- `agent_steps_by_agent`、`agent_terminations`、`agent_triggers`、`bus_intents`；
- `agent_triggers` 键：`replan`、`recall`、`qa_llm`、`em_llm`、`km_llm`、
  `km_note_reused`、`km_adaptive_expand`、`od_llm_decision`、`llm_engaged_count`。

### 1.2 配置、脚本与分析工具

- `scripts/gen_configs.py`：按 `MODE_SPECS` 生成五种模式 × 三规模的配置；
  统一知识库路径、`results_dir`、`agents.control`、`llm_shared`、LLM 参数。
- `scripts/run_experiment.sh`：支持六种模式；`--mode` 映射到对应配置；
  `pipeline-llm`/`llm` 启动前检查 vLLM；所有非 native 模式汇总后调用
  `aggregate_agent.py` 和 `agent_profile.py`。
- `scripts/agent_profile.py`：读取 run_log（可按 `--run-id` 过滤），汇总
  control_flow、LLM flags、decision_mode、各 Agent 步数/终止原因、动作序列/
  hash、触发器组合、LLM 参与数、总线 intent、开销、结果分布，以及 replan/
  recall/KM 新经验子集。
- `tests/test_pipeline_control.py`：5 个控制流对照测试（pipeline 不 replan、
  agent 会 replan、pipeline-llm 同预算但无闭环、pipeline KM 固定 top-K、
  `agent_profile.py` 可消费 run_log）。

### 1.3 四模式冒烟结果（仅 dbs，1,200 条/模式）

> 注意：四个模式实际只跑了 `query/queries_dbs`（结果目录只有
> `results/round_*_dbs`），因此 A0 库 1,200 条全是 `small`；**跨规模对照尚未跑**。

| 模式 | replan | recall | KM 扩 K | LLM 参与 | 动作序列 | 备注 |
|---|---:|---:|---:|---|---:|---|
| `pipeline` | 0 | 0 | 0 | 无 | 1 | 6,000 个动作；固定链路符合预期 |
| `pipeline-llm` | 0 | 0 | 0 | OD 100%、QA 8.3%、EM 1.0%、KM 新经验 2.1%/复用 97.9% | 2 | 同预算、无闭环；LLM 调用点与 A1 对齐 |
| `agent` | 6.2% | 0.1% | 0.4% | 无 | 5 | 7,280 个动作；74 次 replan 中 33 improved / 23 unchanged / 18 degraded |
| `llm` | 21.8% | — | — | OD 100%、QA 8.3%、EM 21.8%、KM 新经验 1.2%/复用 98.8% | 3 | **受旧知识库污染，结果无效** |

控制流机制本身验证通过：pipeline 系列没有 replan/recall/扩 K，agent 系列出现
了观察驱动的闭环动作；但 A1 的闭环触发率被旧延迟参照放大，不能用于效果结论。

### 1.4 A1 知识库污染定位

事实：

- `data/knowledge_llm.db`：15,755 个签名 / 100,700 次执行，**全部是 `small`
  （dbs）**；时间跨度 2026-10-29 20:20 ~ 2026-10-30 15:17，最早只比本次早约
  19 小时。
- 同时出现在旧跑与本次跑的 704 个签名（同查询形状 + 同策略）：
  - 旧执行 21,403 次，平均 **6.70ms**；
  - 本次执行 1,164 次，平均 **13.04ms**；
  - 同一签名绝对延迟漂移约 **2 倍**。
- A1 的 1,127 条 baseline 决策中 1,124 条被判 degraded，触发 21.8% replan 和
  21.8% EM LLM 调用。

根因不是“跨数据集”，而是**同一数据集、同一形状、同一策略的时间/环境漂移**：

1. 所有配置 `reference_time: null`，`_decay()` 在 `reference_time=None` 时
   直接返回 1.0——时效衰减实际未开启。
2. `_history_bonus()` 与 `build_historical_reference()` 直接用裸 `latency_ms`
   比较，没有跨轮/跨环境的归一化。
3. 监控默认 `change_threshold_percent=5%`，对绝对延迟过于敏感。
4. 当前链路没有逐查询 baseline（run_log 中 `baseline_response_time_ms: null`），
   监控只能依赖历史绝对延迟。

结论：按数据集分库救不了这个问题；需要的是“绝对延迟本地/近期校准 + 策略先验
跨数据集迁移”的分层，但本次已决定暂不实施（见 §1.5）。

### 1.5 经验库语义讨论与决策

- 五个 `knowledge_*.db` 是按**实验组**（pipeline/pipeline-llm/agent/llm/fixed）
  隔离，不是按规模/类型/查询/数据集隔离。
- 同一实验组内，dbs/dbm/dbl 共用同一个库：`config.dbs.json`、`config.dbm.json`、
  `config.dbl.json` 的 `knowledge_store` 都指向 `data/knowledge_agent.db` 这类
  同一路径。新数据集的记录追加进同一个库，检索按相似度取历史。
- 相似度仍是单一 `record_similarity`：0.6×query + 0.25×database + 0.15×system；
  记录里只有 `data_scale_level` 时，scale 不匹配会把 database 相似度打到 0，
  跨规模同形状综合相似度约 0.675，达不到 strong ≥0.8。
- 用 A0 的 1,200 条 small 记录做探针，把当前上下文改成 medium/large：
  每个查询形状仍有中位数约 300 条记录 ≥0.5（旧经验不是白学），但 0 条 ≥0.8
  （强证据和绝对阈值必须靠本地）。

**用户决策（2026-10-30）**：暂不改经验库设计，保留 per-mode 隔离与现有相似度/
绝对延迟口径，认为当前实现有其合理性。后续正式 2×2 仍建议从空库或同一知识
快照起跑（这是实验协议问题，不是设计改动）。

### 1.6 文档与测试

- 更新：`README.md`、`docs/run-experiment.md`、`docs/active-plan.md`、
  `docs/agent-runtime.md`、`docs/cli.md`、`docs/knowledge-memory-agent.md`、
  `docs/execution-monitoring-agent.md`、`docs/optimization-decision-agent.md`、
  `docs/query-analysis-agent.md`、`config.example.json`。
- 最近一次全量回归：`pytest tests/` **476 passed + 1 skipped**。
- 本轮**未做任何 commit**；工作区有 56 个文件改动，含大量 `.pyc`，提交前需
  决定是否清理/加入 `.gitignore`。

---

## 2. 待办事项

### 2.1 正式 2×2 实验（当前阻塞项）

- [ ] **确定知识库起跑状态**：每个模式从空库起跑，或四组从同一知识快照起跑；
      不能直接复用 A1 的 10 万条旧库。
- [ ] **处理 A1 旧库**：建议归档/换路径/新建库，**不删除**；需用户确认后再动。
- [ ] **隔离 LLM 缓存**：`config.{scale}.pipeline-llm.json` 与
      `config.{scale}.llm.json` 目前共用 `data/llm_cache_{scale}.json`；正式
      对照应按模式清空或使用独立 cache 文件，避免后跑模式继承前序模式的
      缓存预热。
- [ ] **执行缓存协议**：每个模式开跑前重启 IoTDB + 清 OS 页缓存，见
      `docs/run-experiment.md` §1.3；跨模式延迟对比必须在相同缓存状态下进行。
- [ ] **明确跨规模累积口径**：同一模式内 dbs→dbm→dbl 共用一个库时，后一个
      规模会继承前一个规模的经验。若这是有意设计的“迁移”，固定顺序并报告；
      若要做每个规模独立对照，则每规模重置知识库。两种口径不能混。
- [ ] **统一实验条件**：相同查询采样（`--sample random --seed 42`）、相同
      `--per-file-limit`、相同 `--workers`、相同 vLLM 模型/版本/温度；
      记录 LLM prompt/cache 版本。
- [ ] **跑完整三规模 × 六模式**：`native`、`fixed`、`pipeline`、`pipeline-llm`、
      `agent`、`llm`，按缓存协议逐模式执行。

### 2.2 结果汇总与分析

- [ ] 每个模式跑 `scripts/aggregate_agent.py` + `scripts/agent_profile.py`，
      按统一 `--run-id` 过滤，避免历史批次混入。
- [ ] 控制流指标：replan 率/成功率、recall 率、KM 扩 K 率、动作序列/hash
      分布、终止原因分布。
- [ ] LLM 指标：OD/QA/EM/KM 调用次数、LLM 参与率、LLM 耗时占比、缓存命中率、
      每条查询 LLM 预算（目标：稳态 1 次，最多 3 次）。
- [ ] 性能指标：p50/p95/p99、吞吐、检索/入库/LLM 开销拆分、知识库规模增长与
      检索耗时（1k 库约 4ms → 51k 库约 86ms/条的既有曲线需复核）。
- [ ] 按规模（dbs/dbm/dbl）、查询类型、计划族拆分；区分冷/热知识库。
- [ ] 2×2 因子分析：控制流主效应、LLM 主效应、交互效应；给出置信区间/
      显著性检验（至少多 seed 或重复轮次）。
- [ ] 异常审查：degraded/failed 比例、replan 后改善率、假级联迹象（如某模式
      degraded 率异常高）；先排除知识库/缓存污染再解释结果。

### 2.3 论文材料

- [ ] 主表：六模式 × 三规模的延迟/吞吐/开销/知识命中；2×2 对照表。
- [ ] 消融：pipeline vs pipeline-llm、agent vs llm、KM 扩 K、OD replan、
      recall、LLM 共享范围。
- [ ] 案例：至少 2~3 个 replan/recall/扩 K 的真实轨迹（run_log 可追溯）。
- [ ] 冷/热启动或 leave-one-scale-out 迁移实验（可选，但对“经验库不是查表”
      的质疑很有说服力；见 §3.1）。
- [ ] 局限性：绝对延迟校准、LLM 非确定性、单机环境、缓存敏感性。

### 2.4 提交与文档

- [ ] 清理工作区 `.pyc`（确认 `.gitignore` 是否覆盖；当前有已跟踪的 `.pyc`）。
- [ ] 审阅 56 个文件改动，按逻辑拆分 commit（控制流、配置/脚本、文档、测试）。
- [ ] 用正式实验的最终数字更新 `docs/run-experiment.md`、`docs/active-plan.md`
      中的测试数、吞吐/耗时表、未解决问题。
- [ ] 若用户确认，提交时按仓库约定添加
      `Co-Authored-By: Claude Code <noreply@anthropic.com>`。

---

## 3. 项目后续优化方向

### 3.1 经验库：策略先验与本地校准分层（暂不实施）

> 用户已决定本轮不改；以下仅作为后续方向保留，避免与当前决策冲突。

- **问题**：现在把“可迁移的策略偏好”和“不可迁移的绝对延迟”压在一个相似度/
  一个延迟数值里。旧库跨时间/跨环境会污染监控判断（A1 已实证）。
- **方向**：
  1. 记录补 provenance：`dataset_id`、`device_scale`、`run_id`、`hardware_id`、
     `baseline_latency_ms` / `latency_ratio`。
  2. 相似度拆两个分数：`strategy_similarity`（query+system 为主，scale 轻降权，
     用于策略证据）与 `calibration_similarity`（同数据集+同规模+近期，用于
     绝对延迟参照）。
  3. 决策评分用 `latency_ratio`，不用裸 ms；监控参照只用 calibration 子集，
     `reference_time` 默认设为运行开始/滚动窗口，旧记录按半衰期降权。
  4. 新数据集冷启动：策略先验直接可用；绝对基线用本地前 N 条自举，N 条内
     诚实返回 `insufficient_evidence`。
  5. 验证实验：cold vs warm / leave-one-scale-out（dbs 学，dbm/dbl 测），
     指标为前 N 条策略选择正确率、累计延迟、达到稳定所需样本数。

### 3.2 LLM 参与决策的深度（C 方案）

- 现状：`select_plan()` 仍是确定性的，计划执行参数会覆盖 LLM 的 alignment/
  fetch 选择；LLM 目前主要参与 OD 决策与经验解读，不直接决定执行计划。
- 需要明确论文口径：如果主张“LLM 参与优化决策”，当前证据支持的是
  decision-level 参与；如果要把 plan selection 也交给 LLM，需要实现 C 方案
  并单独做消融/正确性/安全性评估（计划等价性、回退、失败率）。
- 建议先核对 `mataof/agents/optimization_decision/agent.py` 与 `plans/` 的
  实际覆盖顺序，再决定是否扩大 LLM 权限。

### 3.3 监控与绝对延迟校准

- 5% 阈值 + 裸历史绝对延迟在非平稳环境下会产生大量假 degraded；A1 已实证。
- 后续可选：近期窗口/滚动基线、同轮 paired baseline、归一化 `latency_ratio`、
  按查询形状分层的阈值、degraded 连续触发才升级 replan。
- 本轮不改；正式实验必须用干净知识库，并在分析时把“旧参照导致的 degraded”
  与“真实退化”分开统计。

### 3.4 实验设计与统计

- 多 seed / 多轮重复，报告均值、方差、置信区间；当前冒烟只有单轮。
- 固定查询采样与文件顺序；区分“同规模独立对照”与“跨规模累积迁移”两种协议。
- LLM 缓存版本化：记录 cache 文件 hash/清空时间，避免 P1/A1 互相预热。
- 成本预算：全量 LLM 轮次约数小时（历史实测 A1 dbs 全量约 4 小时），
  正式三规模 × 六模式需要提前估算机时。
- 结果可复现：run_id、配置 hash、模型版本、IoTDB 版本、数据版本写入 manifest。

### 3.5 工程与性能

- 知识库检索随规模增长（历史曲线 1k→51k 约 4ms→86ms/条）；top-K 截断、
  SQLite 索引/签名聚合、向量化相似度仍需按正式库规模复核。
- LLM 调用预算：稳态 1 次/查询，新形状/异常/QA 各 +1，单条最多 3 次；
  正式实验需统计实际分布与超预算比例。
- 入库开销曾是全量跑的大头（D8b 后 SQLite 增量写入已降低）；正式实验继续
  拆分 `knowledge_update_ms` 与 LLM 耗时。
- 清理已跟踪的 `.pyc`，避免 diff 噪声；确认 `.gitignore` 覆盖 `__pycache__`。

---

## 4. 风险与注意事项

1. **A1 旧库污染**：`data/knowledge_llm.db` 的 10 万条旧 dbs 记录不能直接
   用于正式对照；换库/归档前需用户确认，禁止删除或覆盖。
2. **冒烟只覆盖 dbs**：四模式各 1,200 条均为 dbs；dbm/dbl 尚未跑，跨规模
   结论暂无实验支撑。
3. **P1/A1 共用 LLM cache**：`data/llm_cache_{scale}.json` 按 scale 命名，
   两个模式共享；正式对照前需清空或分模式隔离。
4. **`reference_time=null` + 5% 阈值**：旧库存在时极易产生假 degraded；
   正式实验从空库起跑可规避首轮污染，但同轮内累积记录仍无时效衰减。
5. **未提交**：工作区 56 个文件改动（含 `.pyc`），提交前需审阅、清理、
   拆分 commit。
6. **数据安全**：不要删除/移动 `data/knowledge_*.db`、`results/` 下的实验
   数据；如需重置，先归档并取得确认。

---

## 5. 关键文件与命令

| 用途 | 路径/命令 |
|---|---|
| 生成配置 | `python3 scripts/gen_configs.py` |
| 跑实验 | `bash scripts/run_experiment.sh --mode <mode> [--query-dir ...] [--per-file-limit 100] [--sample random --seed 42]` |
| 行为剖面 | `python3 scripts/agent_profile.py --run-id <id> results/round_*/run_log.jsonl` |
| 汇总 | `python3 scripts/aggregate_agent.py --run-id <id> ...` |
| 全量测试 | `python3 -m pytest tests/ -v`（最近 476 passed + 1 skipped） |
| 运行协议 | `docs/run-experiment.md`（§1.3 缓存协议、§2 执行流程） |
| 路线图 | `docs/active-plan.md` |
| 控制流实现 | `mataof/runner.py`、`mataof/agents/optimization_decision/agent.py` |
| 配置生成 | `scripts/gen_configs.py`、`scripts/run_experiment.sh` |
| 控制流测试 | `tests/test_pipeline_control.py` |

### 建议执行顺序

1. 确认 A1 旧库处理方式 + 确定知识库起跑状态（空库/同一快照）。
2. 隔离 P1/A1 的 LLM cache，固定模型版本与采样参数。
3. 按 §1.3 缓存协议跑三规模 × 六模式。
4. 用 `aggregate_agent.py` + `agent_profile.py` 汇总，先做污染审查，再做
   2×2 效应分析。
5. 根据结果决定是否启动 §3.1 的经验库分层改造（当前暂不实施）。
6. 清理 `.pyc`、审阅 diff、按逻辑拆分提交。
