# Execution Monitoring Agent（执行监控智能体）规格说明

版本：v1.0
实现：`mataof/agents/execution_monitoring/`

## 1. 职责与边界

**核心职责**：监控当前查询的实际执行情况，采集真实运行指标，建立
**Query + Selected Strategy + Actual Execution Result** 的关联，生成结构化执行反馈。

**这是"事实采集 Agent"**：输出回答"实际执行发生了什么"，而不是"下一次应该使用什么
策略"。策略调整交给 Optimization Decision Agent 与 Knowledge Memory Agent。

**严格界限**：
- 所有性能指标必须来自真实执行环境或系统提供的监控接口；
- 禁止：根据经验估计延迟、根据查询类型猜测 CPU、根据数据规模猜测内存、
  虚构吞吐量、虚构 P95/P99；
- 某项数据无法获得 → `null`/`unknown`，并在 `notes` 记录原因；
- 无 baseline 时不生成比较结果；
- 异常只记录，不修改优化策略；
- 单次实验结果不得推断"策略永久有效"（`feedback.scope = "single_execution"`）；
- 确定性纯函数：相同输入 → 相同输出。

## 2. 输入契约

```json
{
  "query_id": "q1",
  "strategy_id": "time_pruning=partition_pruning|filter_order=time_first|...",
  "execution_status": "success",            // 可选：success/timeout/failed/cancelled/unknown
  "failure_reason": null,                   // failed 时的原因（原始记录）
  "metrics": {                              // 实测指标，全部可选；无效值 → null
    "response_time_ms": 12.3, "p50_latency_ms": 11.0,
    "p95_latency_ms": 14.0, "p99_latency_ms": 15.5,
    "throughput": 320.5, "cpu_utilization": 0.42,
    "memory_utilization": 0.35, "io_throughput": 120.0
  },
  "baseline_metrics": { ... },              // 可选：系统提供的 baseline 执行结果
  "historical_reference": {"response_time_ms": 20.0},  // 可选：历史典型值
  "thresholds": { ... }                     // 可选：异常/判断阈值覆盖
}
```

指标校验规则：
- 时间/吞吐类：非负数值，原样保留（单位由监控接口定义）；
- 利用率：0~1 小数或 0~100 百分数，统一归一化为 0~1（>1 视为百分数 ÷100）；
- 布尔值、字符串、负值 → 无效 → null + 说明；
- 执行状态缺失/非法 → `unknown`（不推断）；`failed` 未给原因 → 记录说明。

## 3. Baseline 比较（`comparison.py`）

- 仅当 `baseline_metrics` 存在且至少一对有效数据时才比较；否则 `available=false`，
  全部变化字段 null（不生成比较结果）；
- 变化百分比 = (实测 − baseline) / baseline × 100，保留 2 位；baseline 为 0 → null + 说明；
- 对比项：response_time / p95 / p99 / throughput / cpu / memory / io（io 为扩展字段）；
- `performance_change` 方向标签（扩展；|变化| ≥ 5% 才生成）：
  `latency_reduction` / `latency_increase`、`p95_improvement` / `p95_degradation`、
  `throughput_improvement` / `throughput_decrease`、`cpu_increase` / `cpu_decrease`、
  `memory_increase` / `memory_decrease`、`io_increase` / `io_decrease`。

## 4. 策略效果判断（`assessment.py`）

| 执行状态 | 判断 | 置信度 |
|---|---|---|
| failed / timeout | failed | 0.7（失败被观测，归因不确定） |
| cancelled / unknown | insufficient_evidence | 0.0 |
| success + 主指标可比（baseline 优先，否则历史典型值） | 变化 ≤ −5% → improved；≥ +5% → degraded；其余 unchanged | 0.6 起，baseline 来源 +0.1，p95/p99 同向佐证 +0.1，上限 0.8 |
| success 但无 baseline 且无历史典型值 | insufficient_evidence | 0.0 |

- 主指标 = `response_time_ms`；缺失时退到 `p95_latency_ms`（basis 中说明）；
- `feedback.strategy_effective` 映射：improved → true；degraded/failed → false；
  unchanged/insufficient_evidence → null；
- `feedback.assessment_basis` 记录判断依据（可追踪的实测事实）；
- 判断只基于实际测量结果，单次观察不夸大（scope=single_execution）。

## 5. 异常识别（`anomalies.py`，只记录）

| 类型 | 触发条件（默认阈值，可覆盖） | 严重度 |
|---|---|---|
| execution_timeout / execution_failed / execution_cancelled | 执行状态触发 | critical / critical / warning |
| latency_high | response_time > 10 s | critical |
| cpu_high / memory_high | 利用率 > 0.9 | warning |
| io_anomaly | IO 相对 baseline > 1.5× | warning |
| latency_deviation | 延迟相对 baseline > 2× | critical |
| historical_deviation | 延迟相对历史典型值 > 2×（策略结果与历史表现明显偏离） | warning |

无参照（无 baseline/历史）时**不生成**相对偏离类异常——不猜测"正常水平"。
每条异常含 `{type, severity, metric, observed, reference, description}`，
只记录、不处理。

## 6. 输出契约（schema v1.0）

顶层：`query_id`、`strategy_id`、`execution_status`、`failure_reason`、`metrics`、
`baseline_comparison`、`performance_change`（扩展）、`performance_assessment`、
`anomalies`、`feedback`（含扩展 `assessment_basis` / `scope`）、`notes`（扩展）。

## 7. LLM 语义增强（混合增强模式）

事实采集是权威；LLM 只做观测事实的复述与关联（`llm_analysis` 节）：

```json
"llm_analysis": {
  "available": true,
  "performance_summary": "本次执行延迟 12.3 ms，相对 baseline 降低 38.5%，CPU 利用率 0.42。",
  "anomaly_summary": "执行失败与超时观测同时发生。",
  "notes": []
}
```

- `performance_summary`：性能复述（llm_generated）；
- `anomaly_summary`：异常关联解读——只允许"同时发生/同时观测到"，**禁止因果归因**
  与策略建议（监控只记录，不改策略）；无异常 → null；
- **数字级防虚构校验**：LLM 输出中的每个数字（量级）必须出现在给定监控数据中，
  出现编造数字 → 对应字段置 null + notes——"绝不虚构实验结果"落实到数字级；
- **确定性兜底**：LLM 未启用/失败/非法输出 → `available=false` + notes，
  事实字段完全不变；
- 非确定性说明：`llm_analysis` 节受模型随机性影响；metrics / 判断 / 异常等
  事实字段保持确定性。

接入方式：`monitor(monitoring_input, llm=llm_client)`；客户端由共享层构造
（`mataof/llm.py`，配置见 docs/cli.md 的 llm 节）。

## 8. 与上下游的接口约定

- 上游：执行层/监控接口提供实测指标与 baseline；Optimization Decision Agent 提供
  `strategy_id`（建立 Query ↔ Strategy ↔ Result 关联）；
- 下游（Knowledge Memory Agent）：`query_id + strategy_id + metrics +
  performance_assessment + anomalies` 可直接作为历史记录的事实来源
  （记录装配与相似检索由 Knowledge Memory Agent 完成）。

## 8. 验证方式

- 单元测试：`pytest tests/test_execution_monitoring_agent.py`（26 例）；
- 全量回归：`pytest tests/`。
