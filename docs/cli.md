# MATAOF 运行手册（正式入口）

## 1. 安装

```bash
cd /opt/program-code/MATAOF
pip install -e .            # 安装后提供 mataof 命令；不安装也可用 python -m mataof
```

## 2. 命令一览

```bash
mataof run "SELECT ..."                    # 运行一条查询（四 Agent 闭环）
mataof run --file queries.sql              # 批量运行（.sql 按分号拆分；.jsonl 每行 {query_id, query}）
mataof retrieve "SELECT ..."               # 仅检索历史知识（不执行、不入库）
mataof stats                               # 知识库累计统计
```

公共参数：`--config CONFIG`（JSON 配置文件）、`--json`（stdout 输出机器可读 JSON）。
`run` 另有 `--results-dir DIR` 覆盖追踪输出目录。

## 3. 配置文件（config.example.json 为模板）

```json
{
  "knowledge_store": "data/knowledge.json",     // 知识库文件（不填 → 仅内存，进程结束即失）
  "results_dir": "results",                     // 追踪输出目录（不填 → 不落盘）
  "executor": {"type": "null"},                 // 执行层，见 §4
  "database_state": {},                         // 决策上下文（统计信息/分区信息等）
  "system_state": {},                           // CPU/内存/IO 等运行时状态
  "candidate_strategies": null,                 // 不填 → 使用策略目录全集
  "baseline_strategy": null,                    // 不填 → 内置安全 baseline
  "thresholds": {},                             // 监控异常阈值覆盖
  "reference_time": null                        // 历史时效衰减参考点（epoch 毫秒）
}
```

## 4. 执行层对接（executor）

系统不碰数据库内核；执行层的职责是：执行决策 Agent 选出的策略并采集真实指标。

执行约定（混合模型）：`strategy["equivalent_sql"]` **非空时优先执行该等价 SQL**
（由候选提供方生成，决策 Agent 仅选择）；为 null 时执行原查询，并按
`strategy_id` / `strategy_parameters` 应用执行级策略（分区裁剪、聚合下推等执行层
动作）。等价 SQL 可通过候选策略的富条目形式提供：

```json
"candidate_strategies": {
  "filter_order": [
    {"strategy": "time_first",
     "equivalent_sql": "SELECT ... WHERE time >= ... AND s1 > 10 AND t1 = 'v'"}
  ]
}
```

| 配置 | 说明 |
|---|---|
| `{"type": "null"}` | 不执行任何查询（默认）。状态 unknown、无任何指标——**不编造执行结果**，监控如实输出 insufficient_evidence |
| `{"type": "file", "results_file": "execution_results.json"}` | 从预先采集的真实执行结果回放（离线实验）。文件格式：`{"results": [{"query_id", "execution_status", "failure_reason", "metrics", "baseline_metrics", "timestamp"}]}`；按 query_id 查找，找不到 → unknown |
| **`{"type": "iotdb", ...}`（真实执行）** | 接入 Apache IoTDB：见下方"真实 IoTDB 执行" |
| 自定义对象（编程方式） | 实现 `Executor` 协议（`mataof/executors/base.py`）：`execute(query, query_id, strategy) -> ExecutionResult`，返回真实采集的数据 |

## 4.1 真实 IoTDB 执行（iotdb 执行器）

配置模板见 `config.iotdb.example.json`：

```json
"executor": {
  "type": "iotdb",
  "host": "127.0.0.1", "port": 6667,
  "user": "root", "password": "root",
  "fetch_size": 1024,
  "measure_baseline": true
}
```

执行约定（`mataof/executors/iotdb.py`）：
- `equivalent_sql` 非空 → 优先执行该等价 SQL；否则执行原查询
  （分区裁剪/聚合位置等执行级策略由 IoTDB 引擎按其自身能力执行，
  系统不做超出 SQL 的能力假设）；
- **真实指标**：延迟 = 墙钟时间（含结果集完整消费）；CPU/内存 = 执行期间采样
  （psutil）；单次执行不产生分位数（P50/P95/P99 置空，不编造）；
- **可选 baseline 实测**：`measure_baseline: true` 时先执行原查询作为 baseline，
  为监控 Agent 提供真实对比数据（每次运行执行两次查询）；
- **错误分类**（真实异常信息 → 状态）：超时类 → timeout；连接类 → failed（连接异常）；
  SQL 错误 → failed（数据库原始错误信息）。失败原因逐字记录，绝不编造；
- 会话复用（懒连接），执行失败自动在下次重连。

启动/停止 IoTDB（本机内置发行包，版本 2.0.6）：

```bash
IOTDB=/opt/program-code/iotdb/distribution/target/apache-iotdb-2.0.6-all-bin/apache-iotdb-2.0.6-all-bin
bash $IOTDB/sbin/start-standalone.sh     # 启动（默认 6667 端口）
bash $IOTDB/sbin/stop-standalone.sh      # 停止
```

## 5. 编程入口（PipelineRunner）

```python
from mataof.runner import PipelineRunner, RunnerConfig

runner = PipelineRunner(RunnerConfig(
    knowledge_store="data/knowledge.json",
    results_dir="results",
    executor={"type": "file", "results_file": "execution_results.json"},
    database_state={"partition_info": {"total_partitions": 128, "covered_partitions": 2}},
))
trace = runner.run_query("SELECT ...", query_id="q1")   # 单次闭环，返回完整追踪记录
traces = runner.run_file("queries.sql")                  # 批量
runner.stats()                                           # 知识库统计
```

## 6. 追踪输出（results_dir）

每次运行落盘两个文件（可追溯性要求）：

- `<query_id>.json`：完整决策链——analysis → knowledge_retrieval → decision →
  execution（原始事实）→ monitoring → knowledge_update；
- `run_log.jsonl`：追加的汇总行（query_id、query_type、strategy_id、decision_status、
  execution_status、performance_assessment、anomaly_count、response_time_ms、
  knowledge_confidence、stored），可直接用于实验统计。

## 7. 运行语义（安全路径）

- 无执行数据（NullExecutor / 结果文件缺该查询）→ 系统照常闭环：
  监控输出 unknown + insufficient_evidence，知识库照实记录，绝不编造；
- 无数据库状态/无历史 → 决策走安全 baseline / fallback；
- 知识库存放在配置的 `knowledge_store` 文件（append-only），跨进程保留。

## 8. 验证

```bash
python3 -m pytest tests/ -v     # 117 例（含 runner/CLI 入口测试）
```
