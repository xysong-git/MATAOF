# 执行说明：在真实 IoTDB 上运行一轮完整实验

本文档说明如何**手动**把 `query/` 下三个规模（dbs / dbm / dbl）的查询集在真实IoTDB 上各执行一轮，并解读产出。
所有命令都在项目根目录 `/opt/program-code/MATAOF`下执行。

## 0. 前置条件

1. IoTDB 已启动且数据已加载（一次性）：

```bash
IOTDB=/opt/program-code/iotdb/distribution/target/apache-iotdb-2.0.6-all-bin/apache-iotdb-2.0.6-all-bin
bash $IOTDB/sbin/start-standalone.sh          # 启动；停止用 stop-standalone.sh
```

2. 验证数据在位（应看到 `root.dbs.g_0` / `root.dbm.g_0` / `root.dbl.g_0`）：

```bash
mataof run --config config.dbs.json \
  "SELECT s_1200 FROM root.dbs.g_0.d_0 WHERE time >= 1640966400000 AND time <= 1640966650000"
```

3. 依赖：`pip install -r requirements.txt`（sqlglot / psutil / apache-iotdb / pytest）。

## 1. 配置（已就绪）

| 配置文件 | 规模 | 知识库 | 追踪输出目录 |
|---|---|---|---|
| `config.dbs.json` | small（root.dbs.g_0） | `data/knowledge.db`（三规模共享） | `results/round_dbs/` |
| `config.dbm.json` | medium（root.dbm.g_0） | `data/knowledge.db`（三规模共享） | `results/round_dbm/` |
| `config.dbl.json` | large（root.dbl.g_0） | `data/knowledge.db`（三规模共享） | `results/round_dbl/` |

三个配置均启用真实 IoTDB 执行与 **baseline 实测**（每条查询执行两次：原查询 +
策略查询）。`database_state.data_scale_level` 用于历史相似度区分规模。

**统一知识库（设计更新）**：跨规模共享——dbs/dbm/dbl 共用
`data/knowledge.db`，不同数据集的执行经验互相迁移（相似度模型按
data_scale_level 区分规模，跨规模记录仍参与证据）；跨模式隔离——
fixed 用 `data/knowledge_fixed.db`、llm 用 `data/knowledge_llm.db`，
固定策略的执行记录不污染 agent/llm 的历史证据（对照实验公平）。

**知识库持久化层**（D8b/D10 实测选定）：`"knowledge_backend": "sqlite"`，
**签名聚合布局（v2）**——executions（append-only 逐条执行日志）+
signatures（静态特征/策略/上下文按内容哈希去重）+ meta。收益（118k 执行实测）：
同质经验归并（24,625 个签名，4.8×）、库文件 140→57.8MB、冷加载 0.9s、
检索扫描 O(唯一签名) ~4.5ms；证据与统计按执行数加权（与逐条口径精确一致）。
可选值：`json` / `jsonl`（逐条存储，旧行为）/ `sqlite`（v2 签名聚合）。
v1 格式库首次加载自动迁移。基准脚本：`scripts/bench_kb.py`。

## 1.1 四种运行模式（统一脚本切换）

`scripts/run_experiment.sh` 用 `--mode` 切换四种模式（同参数、同取样语义、同种子）：

```bash
# 模式一：纯原生 IoTDB（Baseline 1：数据库原生规则与代价优化机制）
bash scripts/run_experiment.sh --mode native --per-file-limit 100 --sample random --seed 42

# 模式二：固定策略对照（Baseline 2：跳过决策 Agent，固定策略组合执行）
bash scripts/run_experiment.sh --mode fixed --per-file-limit 100 --sample random --seed 42

# 模式三：Agent 确定性链路（Ours，无 LLM）
bash scripts/run_experiment.sh --mode agent --per-file-limit 100 --sample random --seed 42

# 模式四：Agent + 本地 vLLM 推理（Ours + LLM 增强；异步补写 + 输出缓存默认开启）
# 需要 vLLM 已启动；未启动会预检失败（不会静默退化成 agent 模式）
bash scripts/run_experiment.sh --mode llm --per-file-limit 100 --sample random --seed 42
```

三组对照（对应开题报告 3.4.1）：native = Baseline 1，fixed = Baseline 2，
agent（与 agent+llm） = Ours。fixed 模式固定策略在
`config.{scale}.fixed.json` 的 `fixed_strategy` 中配置（默认
full_scan / time_first / final_level_aggregation），跳过决策 Agent 与历史检索，
其余链路（执行/监控/入库/追踪）完全复用，指标口径一致。

公共参数：`--query-dir DIR`（**只跑指定查询目录**，如 `query/queries_dbs`；设置后忽略
`--scales`，规模从目录名推导用于选配置）、`--scales dbs,dbm,dbl`（规模）、
`--files q1,q4`（只跑指定文件，默认全部 q*.sql）、`--per-file-limit N`、
`--sample seq|random`、`--seed N`、`--workers N`（查询级并行，agent/llm/fixed
模式生效）。

**workers 选型（实测结论）**：
- 默认异步 LLM 模式与 agent/fixed 模式：主链路为 CPU 密集，workers>1 反而更慢
  （GIL 串行化：29k 库实测 workers=1 约 896ms/条 vs workers=4 约 2215ms/条）；
  异步模式下系统会**自动降级为 1 并提示**，保持默认即可；
- 同步 LLM 模式（`async_enhance: false`，需逐条 LLM 参与决策的实验）：
  workers=4 实测提速 1.75×（重叠 LLM 网络等待），此时并行才有效。

**知识库规模与每轮实验**：知识库 append-only 累积（统一库：跨规模共享，
持续学习），检索成本随库大小线性增长。已实现 **numpy 向量化相似度扫描** +
**融合单遍聚合**（D8b：51k 库单条检索 447~779ms → **86ms**，12.8×；1k 库 4ms）
+ **决策路径解耦**（D8c：runner 走 `summary=False`——跳过全匹配集展示聚合，
分层计数 numpy 直接算、只物化 top-K，48k 库检索 **7.6ms**，随库增长只余
O(N) 扫描 ~5ms/5 万条，近乎解耦）；写入已切换 SQLite 增量持久化
（整库 JSON 快照 3687ms/条 → 12.7ms/条）。CLI `mataof retrieve` 等展示
场景仍走完整聚合路径（historical_summary 完整）。

**CLI 的 --file 支持多文件与通配符**：`mataof run --file query/queries_dbs/q*.sql ...`
（shell 展开为多文件，全部按 --file 处理，每文件分别采样）；若位置参数是文件路径
会直接报错（防止把路径当 SQL 执行）。

各模式配置：`config.{scale}.json`（agent）/ `config.{scale}.llm.json`（llm，本地
Qwen3-8B + enable_thinking=false + **async_enhance 异步增强 + LLM 输出缓存**）。

LLM 模式说明（decision_mode="llm_primary"，D9 设计更新）：
- **LLM 参与策略选择**：每条查询同步调用 LLM 决策（特征化量化 prompt：连续值
  分桶 → 16500 条抽样仅 69 个唯一 prompt，`data/llm_cache_*.json` 输出缓存
  高命中 → 全量实验可行；每条查询恰好 1 次 LLM 调用，QA/监控解读关闭）；
- LLM 只做合法性校验（候选 ∩ 目录、维度适用、需求满足；equivalent_sql 取自
  候选提供方）；LLM 失败/非法 → 确定性决策兜底（reason 标注 llm_selected）；
- 决策输入含**经验摘要**（评价表按查询类型过滤 + 分桶量化）——已学到的知识
  喂给 LLM，学习闭环与 LLM 决策打通；
- 消融/对照实验时清空 `data/llm_cache_*.json` 保证公平（首轮冷调用有界：
  69 个唯一 prompt × 经验分桶状态数）；
- 唯一数验证脚本：`scripts/bench_decision_cache.py`。
每轮结束自动输出汇总报告（**与每批次汇总的指标/排版完全一致**，便于逐批次 vs 总体对比）：
- agent/fixed/llm：`scripts/aggregate_agent.py --run-id <轮次ID>`（合并各规模
  run_log.jsonl，**按轮次过滤**——每轮脚本自动生成统一 run_id，历史批次不混入；
  查询行提供延迟/baseline 延迟/CPU/内存（平均+峰值）/效果分布，
  batch 行提供各批次墙钟总用时 → 总用时与吞吐）；
- 手动指定轮次：`mataof run --run-id my-round ...`；不加 `--run-id` 的
  aggregate 汇总全部历史（如需）；
- native：`scripts/aggregate_native.py`（解析 `run_native_client.py` 输出的 .jsonl——
  客户端墙钟，与 agent **同一客户端/同一段计时代码**；旧 start-cli.sh 的 .out
  文件仍可解析用于回看历史数据，但口径不同、不直接可比）。

计时口径：四模式的执行延迟统一为**客户端墙钟**（发送 → 完整消费结果集）。
native 由 `run_native_client.py` 直发原始 SQL 测量（采样与 agent 共用
`sample_items`，同种子同抽样）；本机 loopback 部署下网络开销可忽略，客户端墙钟
≈ 服务器执行 + 结果传输。决策开销与端到端指标见 §1.2。

## 1.2 决策开销与端到端指标（D8）

每次运行除执行延迟外，还记录**优化机制各行为自身的成本**与**端到端延迟**
（trace 顶层 `overhead` 节；批次/轮次汇总输出口径完全一致）。

**决策开销 = 各行为成本归因**（衡量优化机制自身的成本，每条平均，不出分位数）：

| 阶段 | 字段 | 含义 |
|---|---|---|
| 查询分析 | `analysis_ms` | Query Analysis Agent 特征提取耗时 |
| 知识检索 | `retrieval_ms` | 历史证据检索耗时（随知识库规模增长） |
| 策略决策 | `decision_ms` | 决策 Agent 评分/门控耗时（同步 LLM 模式含推理等待） |
| 执行监控 | `monitoring_ms` | 执行后事实采集耗时 |
| 知识入库 | `knowledge_update_ms` | 经验写入知识库耗时 |
| 合计 | `pipeline_overhead_ms` | 上述之和（不含执行） |
| LLM 推理（异步） | `llm_backfill_ms` | 异步补写全部 llm_analysis 的耗时（**不计入端到端**） |

汇总中显示为：
`决策开销·各阶段（每条平均，样本 N）: 分析 Xms | 检索 Xms | ... | 合计 Xms`。

**端到端 = 请求进入优化系统 → 查询结果就绪**（`end_to_end_ms`，含决策开销与
执行；监控/入库是执行后的收尾，不在用户等待路径上）。端到端是延迟口径
（avg/min/max/P50/P95/P99），用于判断"实际使用系统是否值得"：
- 批次/轮次汇总附带 **净开销** = 端到端每条平均 − baseline 执行每条平均
  （正值 = 优化机制每查询引入的额外时间；净开销明显小于优化节省的执行时间
  即为值得）。

口径要点：
- `execution_stage_ms` 含 baseline 配对双执行，与 `response_time_ms`
  （单次策略执行）不是同一口径，各自展示；
- 失败/超时的查询同样计入各阶段成本（优化开销照常发生，如实记录）；
- 同步 LLM 模式下 LLM 推理时间含在对应阶段（分析/决策/监控）内；异步模式
  单独记录在 `llm_backfill_ms`；
- run_log 汇总行携带各阶段字段 + `pipeline_overhead_ms` + `end_to_end_ms`，
  轮次汇总（aggregate_agent）与批次汇总口径完全一致；旧轮次数据无这些字段时
  如实不计样本。

## 2. 执行一轮（完整流程）

查询集：`query/queries_{dbs,dbm,dbl}/q1.sql ~ q11.sql`，每个文件 2000 条。

**方式 A：逐文件执行（推荐，进度可见、失败不中断）**

```bash
# 小规模（root.dbs.g_0）：11 个文件（worker 保持默认 1，见 §1.1 选型说明）
for f in query/queries_dbs/q*.sql; do
  mataof run --file "$f" --config config.dbs.json
done

# 中规模（root.dbm.g_0）
for f in query/queries_dbm/q*.sql; do
  mataof run --file "$f" --config config.dbm.json
done

# 大规模（root.dbl.g_0）
for f in query/queries_dbl/q*.sql; do
  mataof run --file "$f" --config config.dbl.json
done
```

**方式 B：每文件取样（不用跑全量，新增参数）**

```bash
# 每个文件顺序取前 20 条
mataof run --file query/queries_dbs/q1.sql --config config.dbs.json --per-file-limit 20

# 每个文件随机取 20 条（seed=42 默认，同种子可复现；换 --seed 换抽样）
mataof run --file query/queries_dbs/q1.sql --config config.dbs.json \
           --per-file-limit 20 --sample random --seed 42

# 一轮全量=每个文件取 N 条：三规模 33 个文件 × N 条
for f in query/queries_dbs/q*.sql; do
  mataof run --file "$f" --config config.dbs.json --per-file-limit 100 --sample random
done
```

参数说明：
- `--workers N`：**查询级并行**（B2，默认 1；仅同步 LLM 模式有效，
  实测 1.75× 提速；异步/无 LLM 模式系统自动降级为 1，见 §1.1 选型说明）；
- `--per-file-limit N`：**每个 SQL 文件**最多取 N 条（默认全量 2000）；
- `--sample seq|random`：顺序取前 N 条 / 随机抽样（抽样保持文件内原有顺序，种子可复现；同一 `--seed` 两次运行取到完全相同的语句）；
- `--seed N`：随机抽样种子（默认 42）；
- `--limit N`：与 `--file` 联用时等价于 `--per-file-limit`（兼容旧用法）；
- 配置文件同样支持：`"per_file_limit": 100, "sample_mode": "random", "sample_seed": 42`；
- 采样信息写入每条追踪与 `run_log.jsonl` 的 `sampling` 字段（如 `{"mode": "random", "limit": 3, "total": 2000, "seed": 42}`），实验可追溯。

**知识库规模与证据截断**：知识库 append-only 累积，查询集同质时相似历史会很多——决策证据清单默认取 top-100（配置 `knowledge_max_records`），防止结果文件膨胀与每查询检索开销线性增长；截断不影响成功率统计（始终全量计算）。

耗时 = 每文件条数 × 每查询耗时（见 §2.1 吞吐表），按取样比例线性缩短。

**方式 C：后台整轮运行（统一脚本，见 §2.1 耗时）**

```bash
nohup bash scripts/run_experiment.sh --mode agent \
  > /tmp/mataof_round.log 2>&1 &
```

## 2.1 吞吐预期（B1/B2/B3/B4/B5 优化后的口径）

两个时间口径需要区分：
- **主链路完成**：确定性决策+执行+监控+入库全部落盘的时间（结果可用）；
- **LLM 产物补齐**：llm 模式下后台补写全部 llm_analysis 的时间（结果文件最终完整）。

| 配置 | 主链路每查询 | 全量 6.6 万条主链路 | LLM 产物补齐（llm 模式） |
|---|---|---|---|
| native（客户端直发，无 Agent） | ~10ms 量级（客户端墙钟，与 agent 同口径） | ≈25 分钟 | — |
| agent / fixed（无 LLM） | ≈43~200ms（随知识库增长） | ≈**2~4 小时** | — |
| llm（异步+缓存，默认配置） | ≈43~200ms（**与 agent 相同**） | ≈2~4 小时 | 后台 2 workers：采样 10% 时约 6 小时；**第二轮起缓存命中 → 趋零** |
| llm 同步（`"async_enhance": false`） | ≈6.3~8.4s（不推荐全量） | 数天 ❌ | — |

说明：
- llm 模式默认 `async_enhance: true` + `cache_file`：**主链路与 agent 模式同速**，
  LLM 解读后台补齐；重复跑同一查询集时缓存命中，后台也几乎零开销；
- 知识库随运行增长，检索开销线性上升（D8b 后：1k 库 4ms → 51k 库 86ms/条）；
  落盘已切换 SQLite 增量写入（O(新增)，每文件 flush 不再随库放大）；
- 需要"逐条同步 LLM 参与"的实验（如 preference_bonus）才关闭 async。

## 3. 运行中你会看到什么

每查询一行摘要：
```
[query-0001] type=range_query | strategy=time_pruning=partition_pruning|... | status=success | assessment=improved | anomalies=0 | knowledge_conf=0.05
```
- `status`：真实执行状态（success/timeout/failed/...）；
- `assessment`：策略相对 baseline 的效果判断（improved/unchanged/degraded/...）；
- `knowledge_conf`：检索到的历史知识量（越跑越高）；
- 个别失败不会中断整轮（逐条独立），失败原因会如实记录。

## 04. 产出与解读

每次运行 `mataof run` 产生以下产物（**每次运行都是新文件，绝不覆盖历史结果**）：

| 产出 | 位置 | 内容 |
|---|---|---|
| 完整追踪 | `results/<results_dir>/<query_id>.json` | 单条查询的完整决策链：analysis（含 llm_analysis）→ knowledge_retrieval → decision（含 llm_analysis）→ execution（原始实测）→ monitoring（含 llm_analysis）→ knowledge_update → **overhead（D8：决策开销/端到端计时与分项）** |
| 汇总日志 | `results/<results_dir>/run_log.jsonl` | **追加**的逐行 JSON：query_id、source（来源文件）、query_type、strategy_id、decision_status、execution_status、performance_assessment、anomaly_count、response_time_ms、**各阶段成本（analysis/retrieval/decision/monitoring/knowledge_update_ms）+ pipeline_overhead_ms + end_to_end_ms（D8）**、knowledge_confidence、stored——可直接 pandas 统计 |
| 知识库 | `data/<knowledge_store>`（默认 .db = SQLite） | append-only 历史经验（跨运行累积；含 LLM 经验总结 llm_note） |
| LLM 调用日志 | `data/llm_log.jsonl`（配置 log_file） | 每次 LLM 调用的时间/延迟/状态（含失败原因） |
| 终端摘要 | stdout | 每查询一行（query_id/类型/策略/状态/评估/异常/知识置信度） |

**命名规则**（防止同名覆盖）：
- 自动 query_id = `<run_id>-<序号>`，run_id 为每次运行的时戳（如 `20260921-143012-0001`）；
  `--file` 运行时 trace 与 run_log 附带`source` 字段记录来源文件名；
- 极端同名冲突时自动追加 `-2`、`-3` 后缀，**绝不覆盖既有文件**。

**查询知识库统计**：`mataof stats --config config.dbs.json`（逐策略total/success/failure/成功率/平均延迟）。

示例分析：
```bash
mataof stats --config config.dbs.json      # 各策略累计成功率与平均延迟
python3 - <<'EOF'
import json, collections
rows = [json.loads(l) for l in open("results/round_dbs/run_log.jsonl", encoding="utf-8")]
c = collections.Counter(r["performance_assessment"] for r in rows)
print("dbs 规模效果分布:", dict(c))
EOF
```

## 5. 重要注意事项

1. **毫秒级查询的噪声**：阈值 ±5% 下，10ms 量级查询的实测抖动可能让同一条 SQL两次执行判为 degraded——这是真实测量波动，系统如实记录；
   评估策略应看`mataof stats` 的**累计成功率**，而非单条结果。
2. **baseline 双执行**：`measure_baseline: true` 使每次运行执行两条 SQL（原查询 + 策略），并带来缓存预热效应；对延迟敏感的对比实验可关闭。
3. **`SELECT last` 查询（q8）**：不属于五类查询类型，分析如实标记 unknown、各维度 not_applicable，按原 SQL 执行——预期行为，不是错误。
4. **BOOLEAN 传感器**：部分传感器（如某些 `s_0`）为 BOOLEAN 类型，数值谓词
   （`s_0 > -5`）会被数据库真实拒绝并记录为失败经验；这是真实数据特性，系统如实记录（不掩盖、不编造）。
5. **换轮次**：query_id 自动带 run_id 时戳前缀，不同运行的文件互不覆盖（极端冲突
   自动加后缀）；想彻底分开多轮实验时换 `results_dir`/`knowledge_store` 即可。
   知识库是 append-only 累积的——换知识库路径即从零开始。
6. **每文件 2000 条**的 query_id 为自动编号 `query-NNNN`（同文件内唯一）；
   跨文件/跨规模分析时以 `run_log.jsonl` 所在目录区分。

## 6. 一键自检

```bash
python3 -m pytest tests/ -v      # 246 例，含真实 IoTDB 集成测试（服务器不可用自动跳过）
```
