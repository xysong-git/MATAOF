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
mataof run --file query/queries_dbs/q*.sql # 通配符展开为多文件，每文件分别取样
mataof run --file queries.sql --workers 4  # 查询级并行（仅同步 LLM 模式有效；异步模式自动降级）
mataof retrieve "SELECT ..."               # 仅检索历史知识（不执行、不入库）
mataof stats                               # 知识库累计统计
```

`run` 参数：`--config CONFIG`、`--json`（stdout 机器可读 JSON）、
`--results-dir DIR`、`--per-file-limit N`、`--sample seq|random`、`--seed N`、
`--limit N`、`--workers N`（并行）。

**批次执行汇总**：每次 `mataof run` 结束时打印本批次统计（数据全部来自本批次
真实执行样本，绝不编造）：

```
== 执行汇总（本批次）==
查询数: 3 | 总用时: 0.13s | 吞吐: 23.0769 查询/秒
执行: 成功 3 / 失败 0 / 超时 0 / 未知 0（成功率 1.0）
效果分布: improved 3
延迟（成功执行，样本 3）: avg 5.64ms | min 5.33ms | max 5.81ms | P50 5.77ms | P95 5.81ms | P99 5.81ms
baseline 延迟（样本 3）: avg 6.46ms | P50 6.34ms | P95 7.4ms | P99 7.4ms
```

- **分位数只在此生成**：P50/P95/P99 是多样本统计，对"成功执行"的实测延迟样本
  按最近秩法计算；样本不足 2 条时如实为 null（单次执行不产生分位数）；
- 吞吐 = 本批次查询数 / 墙钟总用时；成功率 = success / 总数
  （failed/timeout/unknown 分别计数）；
- `--json` 模式 stdout 输出 `{"traces": [...], "batch_summary": {...}}`；
- 编程方式：`from mataof.report import batch_summary, format_batch_summary`。

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
  "reference_time": null,                       // 历史时效衰减参考点（epoch 毫秒）
  "agents": {                                   // 四 Agent 运行时（模板默认关闭）
    "enabled": false,                           // 全局默认；单个 Agent 可覆盖
    "llm_shared": false,                        // true：同一 LLM 按策略共享给 QA/EM/KM（KM 按形状）
    "control": "agent",                         // agent：观察驱动闭环；pipeline：固定流水线
    "query_analysis": {
      "llm_policy": "on_gap",                   // on_gap | always | never
      "confidence_threshold": 0.5,
      "deadline_ms": 10000
    },
    "optimization_decision": {                  // 旧键 "agent" 仍兼容（仅启用 OD）
      "max_attempts": 2,
      "deadline_ms": 30000,
      "replan_on": ["degraded", "failed"],
      "recall_on_empty_evidence": true,         // 初始证据为空 → 经总线请求补充检索
      "recall_max_records": null                // null = max(200, 首轮×2)
    },
    "execution_monitoring": {
      "llm_policy": "on_anomaly",               // on_anomaly | on_anomaly_or_degraded | always | never
      "deadline_ms": 5000
    },
    "knowledge_memory": {
      "min_strong": 1,                          // 已列出强匹配达到该值即充分
      "max_rounds": 2,                          // 检索轮数上限（含首轮）
      "expand_factor": 2,                       // 每轮 top-K 扩大倍数
      "deadline_ms": 2000
    }
  }
}
```

`agents.enabled` 是全局默认值，某个 Agent 自己声明 `enabled` 时优先生效，便于
单 Agent 消融。旧配置 `agent` 等价于 `agents.optimization_decision`，两者合并时
新键优先。四个 Agent 默认关闭，关闭时主链路与旧版本完全一致。

`agents.control` 切换控制流（2×2 对照的“控制流”维度）：
- `agent`（默认）：观察驱动闭环——OD 在 degraded/failed 后 replan、空证据时
  经总线 recall，KM 可按观察扩 top-K；
- `pipeline`：固定流水线——单次决策/执行、固定 top-K 检索，不做
  replan/recall/自适应扩 K；QA/EM/KM 组件与 LLM 调用点保持不变。

`agents.llm_shared` 控制 LLM 在 Agent 间的共享（默认 `false`，保持旧语义）：
- `false`：`decision_mode="llm_primary"` 下 LLM 只用于 OD 决策（1 次/查询）；
  非 primary 模式仍按旧路径把 LLM 用于解读增强；
- `true`（LLM 常驻）：同一 LLM 客户端按各 Agent 的 `llm_policy` 共享——
  QA 仅在缺口/`always` 时调用；执行后监控解读由 EM 的 `llm_policy=on_anomaly`
  （默认）门控，仅真实异常或 failed/timeout 时调用；KM 经验总结按查询形状
  收口（`experience_key`：同形状 + 策略 + 数据规模，不含精确 time_span），
  同一形状首次执行调用 1 次、后续复用。主实验配置（OD Agent +
  `llm_primary`）的调用预算：每条查询必有 1 次 OD 决策调用；QA 触发 +1；
  异常/失败触发监控解读 +1；新形状首条 KM 经验 +1。**稳态正常查询 1 次、
  单条最多 3 次**。`on_anomaly_or_degraded` 保留为消融策略。

`scripts/gen_configs.py` 生成控制流 × LLM 的 2×2 矩阵（各模式独立知识库，
避免一种配置的执行记录污染另一种的历史证据）：
- `config.*.pipeline.json`（P0）：`enabled=true`、`control="pipeline"`、无 LLM；
- `config.*.pipeline-llm.json`（P1）：`enabled=true`、`control="pipeline"`、
  `llm_shared=true`；
- `config.*.json`（A0）：`enabled=true`、`control="agent"`、无 LLM；
- `config.*.llm.json`（A1）：`enabled=true`、`control="agent"`、
  `llm_shared=true`；
- `config.*.fixed.json`：`enabled=false`（固定策略对照，跳过决策/检索）；
- `config.local-llm.json`：dbs 规模的 A1 本地配置。
启用后：

- Query Analysis：确定性分析后按 `llm_policy` 决定是否调用 LLM 标注
  （`on_gap` 默认，仅证据缺口出现时调用）；
- Optimization Decision：执行所选计划并观察监控反馈，degraded/failed 时排除该计划
  重规划；初始历史证据为空时向 Knowledge Memory 请求一次更宽检索；
- Execution Monitoring：无 baseline/无指标时跳过比较并记录原因；`llm_shared=true`
  时执行后解读由 Runner 统一发起（默认仅异常/失败触发）；
- Knowledge Memory：强证据不足且仍有未展开记录时自适应扩大 top-K；经验总结
  按查询形状生成（同一形状只调用一次 LLM，后续执行复用），不再逐执行调用。

任一 Agent 启用时，Runner 为每条查询构建 AgentBus；QA/KM/EM 以端点注册，
检索、补充证据、监控与入库都经消息路由（端点内部按配置选择 Agent 或确定性
实现）。每步行动、观察、终止原因写入 `trace["agents"]`，消息往返写入
`trace["bus"]`；run_log 增加 `agents_enabled`、`agent_steps_total`、
`bus_messages`、`bus_errors`。详细语义、trace 字段与消融方式见
[docs/agent-runtime.md](agent-runtime.md)。

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

## 4.2 LLM 配置（共享层，混合增强模式）

配置文件的 `llm` 节（缺省 provider=none → 纯确定性路径，行为不变）：

```json
"llm": {
  "provider": "none",                          // none | openai | local
  "base_url": "https://api.deepseek.com/v1",   // openai 兼容端点
  "api_key": "",                               // 或环境变量 LLM_API_KEY
  "model": "deepseek-chat",
  "timeout_s": 60,
  "max_tokens": 2048,
  "log_file": "data/llm_log.jsonl",            // 调用日志（时间/延迟/状态）
  "sample_rate": 10,                           // 每 N 条查询启用一次同步 LLM 增强（A1）
  "async_enhance": true,                       // B4：异步增强（主链路零 LLM 调用）
  "async_workers": 2,                          // 后台补写线程数
  "cache_file": "data/llm_cache.json",         // B3：LLM 输出缓存（跨轮零开销）
  "preference_bonus": false                    // LLM 参与评分（开启时强制同步模式）
}
```

- `provider=none`：不启用（默认，确定性路径）；
- `provider=openai`：任何 OpenAI 兼容 API（DeepSeek/OpenAI/自建 vLLM）；
- `provider=local`：本地推理服务（POST `{"messages", "max_tokens"}` → `{"text"}`，
  与 MAPO 的 FastAPI 服务形式一致）；
- `async_enhance=true`（B4）：主链路为确定性快路径，LLM 解读由后台线程补写进
  trace（批次结束自动排空）；`preference_bonus=true` 时强制回退同步；
- `cache_file`（B3）：缓存只存原始响应文本，各 Agent 的解析/防编造校验照常执行；
  消融实验对比 LLM 影响时清空缓存保证公平；

**本地 vLLM + Qwen3 配置要点**（本机已验证，模板见 `config.local-llm.json`）：
- `base_url` 指向 `http://127.0.0.1:8000/v1`，`model` 填服务端模型名
  （vLLM 默认即模型路径，如 `/opt/models/Qwen3-8B`）；vLLM 无鉴权时
  `api_key` 填任意非空值（如 `EMPTY`）；
- **Qwen3 需关闭 thinking**：`"extra_body": {"chat_template_kwargs":
  {"enable_thinking": false}}`——否则输出进入 `<think>` 块、消耗大量 token；
- **本地地址自动绕过系统代理**：客户端对 127.0.0.1/localhost 直连，
  不受 `http_proxy` 环境变量影响（远程 API 仍遵循系统代理）。
- **失败即兜底**：LLM 不可用/超时/输出非法 → Agent 自动回退确定性规则，
  绝不中断链路；调用统计写入 `log_file`（JSONL）。

当前四个 Agent 均已接入 LLM 增强（统一模式：确定性核心权威 + LLM 只写
`llm_analysis` 附加节 + 失败即兜底）：
- Query Analysis：语义摘要 / 类型提示 / 标签属性判别建议；
- Optimization Decision：决策解读（semantic_reason）/ 候选偏好（preferences，
  严格限于候选集合）；`llm` 节可加 `"preference_bonus": true` 开启实验性偏好加分
  （+0.05，不参与风险门控，默认关闭）；
- Knowledge Memory：检索解读（semantic_summary，禁止策略建议）/ 经验总结
  （experience_note，随记录存为 `llm_note` 元数据字段）；
- Execution Monitoring：性能复述 / 异常关联解读（禁因果归因、禁建议），
  **数字级防虚构校验**（LLM 输出数字必须 ⊆ 给定监控数据）。

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
  knowledge_confidence、stored；启用 Agent 时另有 agents_enabled、
  agent_steps_total、bus_messages、bus_errors），可直接用于实验统计。

## 7. 运行语义（安全路径）

- 无执行数据（NullExecutor / 结果文件缺该查询）→ 系统照常闭环：
  监控输出 unknown + insufficient_evidence，知识库照实记录，绝不编造；
- 无数据库状态/无历史 → 决策走安全 baseline / fallback；
- 知识库存放在配置的 `knowledge_store` 文件（append-only），跨进程保留。

## 8. 验证

```bash
python3 -m pytest tests/ -v     # 476 例通过 + 1 跳过（含 runner/CLI 入口测试）
```
