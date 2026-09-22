# 执行说明：在真实 IoTDB 上运行一轮完整实验

本文档说明如何**手动**把 `query/` 下三个规模（dbs / dbm / dbl）的查询集在真实
IoTDB 上各执行一轮，并解读产出。所有命令都在项目根目录 `/opt/program-code/MATAOF`
下执行。

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

## 2. 执行一轮（完整流程）

查询集：`query/queries_{dbs,dbm,dbl}/q1.sql ~ q11.sql`，每个文件 2000 条。

**方式 A：逐文件执行（推荐，进度可见、失败不中断）**

```bash
# 小规模（root.dbs.g_0）：11 个文件
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
- `--per-file-limit N`：**每个 SQL 文件**最多取 N 条（默认全量 2000）；
- `--sample seq|random`：顺序取前 N 条 / 随机抽样（抽样保持文件内原有顺序，
  种子可复现；同一 `--seed` 两次运行取到完全相同的语句）；
- `--seed N`：随机抽样种子（默认 42）；
- `--limit N`：与 `--file` 联用时等价于 `--per-file-limit`（兼容旧用法）；
- 配置文件同样支持：`"per_file_limit": 100, "sample_mode": "random", "sample_seed": 42`；
- 采样信息写入每条追踪与 `run_log.jsonl` 的 `sampling` 字段
  （如 `{"mode": "random", "limit": 3, "total": 2000, "seed": 42}`），实验可追溯。

**知识库规模与证据截断**：知识库 append-only 累积，查询集同质时相似历史会很多——
决策证据清单默认取 top-100（配置 `knowledge_max_records`），防止结果文件膨胀与
每查询检索开销线性增长；截断不影响成功率统计（始终全量计算）。

耗时 = 每文件条数 × 每查询耗时（见 §2.1 吞吐表），按取样比例线性缩短。

**方式 C：后台整轮运行（约 50 分钟，见耗时估算）**

```bash
nohup bash -c '
  for s in dbs dbm dbl; do
    for f in query/queries_$s/q*.sql; do
      mataof run --file "$f" --config config.$s.json
    done
  done' > /tmp/mataof_round.log 2>&1 &
```

**耗时估算**（实测：43ms/条，含 baseline 双执行与全链路开销）：
每文件 2000 条 ≈ 86 秒；每规模 11 文件 ≈ 16 分钟；三个规模整轮 ≈ 48 分钟。
关闭 baseline（`"measure_baseline": false`）约减半。

## 2.1 吞吐预期与 LLM 采样（重要）

实测（本机，IoTDB 真实执行 + baseline 双执行）：

| 配置 | 每查询耗时 | 一个文件（2000 条） | 三规模全量（6.6 万条） |
|---|---|---|---|
| 无 LLM（provider=none） | ≈43ms | ≈1.5 分钟 | ≈47 分钟 |
| LLM 全开（本地 Qwen3-8B，每条 4 次调用） | ≈6.6s | ≈3.7 小时 | ≈5 天（不推荐全量） |
| `"sample_rate": 10`（每 10 条启用一次 LLM） | ≈0.7s | ≈23 分钟 | ≈13 小时 |
| `"sample_rate": 50` | ≈0.17s | ≈6 分钟 | ≈3.2 小时 |

配置 `llm.sample_rate`（整数 N，默认 1=全开）：每条查询照常产出完整结构化结果，
LLM 增强只在每 N 条采样启用（未采样的查询 `llm_analysis.available=false`，
确定性字段不受影响）。实验建议：全量用无 LLM 或 sample_rate=50 积累知识与
结果；需要 LLM 增强样本时用 sample_rate=10 或全开跑抽样文件。

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
- 自动 query_id = `<run_id>-<序号>`，run_id 为每次运行的时戳
  （如 `20260921-143012-0001`）；`--file` 运行时 trace 与 run_log 附带
  `source` 字段记录来源文件名；
- 极端同名冲突时自动追加 `-2`、`-3` 后缀，**绝不覆盖既有文件**。

**查询知识库统计**：`mataof stats --config config.dbs.json`（逐策略
total/success/failure/成功率/平均延迟）。

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

1. **毫秒级查询的噪声**：阈值 ±5% 下，10ms 量级查询的实测抖动可能让同一条 SQL
   两次执行判为 degraded——这是真实测量波动，系统如实记录；评估策略应看
   `mataof stats` 的**累计成功率**，而非单条结果。
2. **baseline 双执行**：`measure_baseline: true` 使每次运行执行两条 SQL
   （原查询 + 策略），并带来缓存预热效应；对延迟敏感的对比实验可关闭。
3. **`SELECT last` 查询（q8）**：不属于五类查询类型，分析如实标记 unknown、
   各维度 not_applicable，按原 SQL 执行——预期行为，不是错误。
4. **BOOLEAN 传感器**：部分传感器（如某些 `s_0`）为 BOOLEAN 类型，数值谓词
   （`s_0 > -5`）会被数据库真实拒绝并记录为失败经验；这是真实数据特性，系统
   如实记录（不掩盖、不编造）。
5. **换轮次**：`results_dir` 内同名 query_id 会覆盖；新一轮建议改 `results_dir`
   为新目录。知识库是 append-only 累积的——换知识库路径即从零开始。
6. **每文件 2000 条**的 query_id 为自动编号 `query-NNNN`（同文件内唯一）；
   跨文件/跨规模分析时以 `run_log.jsonl` 所在目录区分。

## 6. 一键自检

```bash
python3 -m pytest tests/ -v      # 135+ 例，含真实 IoTDB 集成测试（服务器不可用自动跳过）
```
