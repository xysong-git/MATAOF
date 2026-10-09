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
| `config.dbs.json` | small（root.dbs.g_0） | `data/knowledge_dbs.json` | `results/round_dbs/` |
| `config.dbm.json` | medium（root.dbm.g_0） | `data/knowledge_dbm.json` | `results/round_dbm/` |
| `config.dbl.json` | large（root.dbl.g_0） | `data/knowledge_dbl.json` | `results/round_dbl/` |

三个配置均启用真实 IoTDB 执行与 **baseline 实测**（每条查询执行两次：原查询 +
策略查询）。`database_state.data_scale_level` 用于历史相似度区分规模。

## 1.1 四种运行模式（统一脚本切换）

`scripts/run_experiment.sh` 用 `--mode` 切换四种模式（同参数、同取样语义、同种子）：

```bash
# 模式一：纯原生 IoTDB（Baseline 1：数据库原生规则与代价优化机制）
bash scripts/run_experiment.sh --mode native --per-file-limit 100 --sample random --seed 42

# 模式二：固定策略对照（Baseline 2：跳过决策 Agent，固定策略组合执行）
bash scripts/run_experiment.sh --mode fixed --per-file-limit 100 --sample random --seed 42 --workers 4

# 模式三：Agent 确定性链路（Ours，无 LLM）
bash scripts/run_experiment.sh --mode agent --per-file-limit 100 --sample random --seed 42 --workers 4

# 模式四：Agent + 本地 vLLM 推理（Ours + LLM 增强；异步补写 + 输出缓存默认开启）
# 需要 vLLM 已启动；未启动会预检失败（不会静默退化成 agent 模式）
bash scripts/run_experiment.sh --mode llm --per-file-limit 100 --sample random --seed 42 --workers 4
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
模式生效；LLM 模式建议 4）。

**CLI 的 --file 支持多文件与通配符**：`mataof run --file query/queries_dbs/q*.sql ...`
（shell 展开为多文件，全部按 --file 处理，每文件分别采样）；若位置参数是文件路径
会直接报错（防止把路径当 SQL 执行）。

各模式配置：`config.{scale}.json`（agent）/ `config.{scale}.llm.json`（llm，本地
Qwen3-8B + enable_thinking=false + **async_enhance 异步增强 + LLM 输出缓存**）。

LLM 模式说明（B4/B3 已默认启用）：
- 主链路为确定性快路径（LLM 解读由后台线程补写进 trace，批次结束自动排空）；
- 第二轮起同查询集命中 LLM 缓存（`data/llm_cache_*.json`），LLM 开销趋零；
- 需要 LLM 同步参与决策（实验性偏好加分）时：设 `"preference_bonus": true`，
  系统自动回退同步模式（async 失效并提示）；
- 消融实验对比 LLM 影响时，清空 `data/llm_cache_*.json` 保证公平。
每轮结束自动输出汇总报告（**与每批次汇总的指标/排版完全一致**，便于逐批次 vs 总体对比）：
- agent/fixed/llm：`scripts/aggregate_agent.py --run-id <轮次ID>`（合并各规模
  run_log.jsonl，**按轮次过滤**——每轮脚本自动生成统一 run_id，历史批次不混入；
  查询行提供延迟/baseline 延迟/CPU/内存（平均+峰值）/效果分布，
  batch 行提供各批次墙钟总用时 → 总用时与吞吐）；
- 手动指定轮次：`mataof run --run-id my-round ...`；不加 `--run-id` 的
  aggregate 汇总全部历史（如需）；
- native：`scripts/aggregate_native.py`（解析 CLI 原生 `It costs` 计时，服务端执行口径）。

计时口径注意：native 的延迟为服务端执行耗时（不含结果集传输）；agent/llm 为含结果集消费的墙钟时间——对比时注明口径。

## 2. 执行一轮（完整流程）

查询集：`query/queries_{dbs,dbm,dbl}/q1.sql ~ q11.sql`，每个文件 2000 条。

**方式 A：逐文件执行（推荐，进度可见、失败不中断）**

```bash
# 小规模（root.dbs.g_0）：11 个文件，4 并发
for f in query/queries_dbs/q*.sql; do
  mataof run --file "$f" --config config.dbs.json --workers 4
done

# 中规模（root.dbm.g_0）
for f in query/queries_dbm/q*.sql; do
  mataof run --file "$f" --config config.dbm.json --workers 4
done

# 大规模（root.dbl.g_0）
for f in query/queries_dbl/q*.sql; do
  mataof run --file "$f" --config config.dbl.json --workers 4
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
- `--workers N`：**查询级并行**（B2，默认 1 串行；LLM 模式下与 vLLM 连续批处理
  配合可获 3~4× 吞吐，结果按输入顺序输出；`mataof run` 同样支持）；
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
nohup bash scripts/run_experiment.sh --mode agent --workers 4 \
  > /tmp/mataof_round.log 2>&1 &
```

## 2.1 吞吐预期（B1/B2/B3/B4/B5 优化后的口径）

两个时间口径需要区分：
- **主链路完成**：确定性决策+执行+监控+入库全部落盘的时间（结果可用）；
- **LLM 产物补齐**：llm 模式下后台补写全部 llm_analysis 的时间（结果文件最终完整）。

| 配置 | 主链路每查询 | 全量 6.6 万条主链路 | LLM 产物补齐（llm 模式） |
|---|---|---|---|
| native（原生 CLI） | ~15ms（服务端口径） | ≈25 分钟 | — |
| agent / fixed（无 LLM） | ≈43~200ms（随知识库增长） | ≈**2~4 小时** | — |
| llm（异步+缓存，默认配置） | ≈43~200ms（**与 agent 相同**） | ≈2~4 小时 | 后台 2 workers：采样 10% 时约 6 小时；**第二轮起缓存命中 → 趋零** |
| llm 同步（`"async_enhance": false`） | ≈6.3~8.4s（不推荐全量） | 数天 ❌ | — |

说明：
- llm 模式默认 `async_enhance: true` + `cache_file`：**主链路与 agent 模式同速**，
  LLM 解读后台补齐；重复跑同一查询集时缓存命中，后台也几乎零开销；
- 知识库随运行增长，检索/落盘开销线性上升——全量运行后期每查询约 0.2~0.4s
  （`knowledge_save_interval=20` 已缓解落盘，检索扫描是主要增长项）；
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

## 4. 产出与解读

每次运行 `mataof run` 产生以下产物（**每次运行都是新文件，绝不覆盖历史结果**）：

| 产出 | 位置 | 内容 |
|---|---|---|
| 完整追踪 | `results/<results_dir>/<query_id>.json` | 单条查询的完整决策链：analysis（含 llm_analysis）→ knowledge_retrieval → decision（含 llm_analysis）→ execution（原始实测）→ monitoring（含 llm_analysis）→ knowledge_update |
| 汇总日志 | `results/<results_dir>/run_log.jsonl` | **追加**的逐行 JSON：query_id、source（来源文件）、query_type、strategy_id、decision_status、execution_status、performance_assessment、anomaly_count、response_time_ms、knowledge_confidence、stored——可直接 pandas 统计 |
| 知识库 | `data/<knowledge_store>.json` | append-only 历史经验（跨运行累积；含 LLM 经验总结 llm_note） |
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
python3 -m pytest tests/ -v      # 135+ 例，含真实 IoTDB 集成测试（服务器不可用自动跳过）
```
