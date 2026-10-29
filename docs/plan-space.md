# 计划空间（Plan Space）与成本模型

本文件描述**优化决策 Agent 内置的可执行计划能力**：计划语法（descriptor）、
候选生成（generator）、成本/收益模型（cost）、执行与合并（executor/merge）、
决策集成（integration）与知识沉淀。计划不是外挂层：Agent 在 `decide()` 中
按原始 SQL + QA 输出自行生成候选计划，再用成本模型选择，执行器消费选中计划。
原则：

- **不改 IoTDB 源码**：所有计划都由现有 SQL/客户端能力表达；
- **Agent 编译、Agent 选择**：计划里的子查询与合并规格在生成时即已确定，
  决策阶段不做 SQL 生成/改写；外部只提供事实输入（query/QA/设备清单）；
- **可执行性是硬门禁**：结构上无法执行/无法合并的计划不进入候选，而不是
  "先选再失败"；
- **不声称全局最优**：成本模型只在"当前信息条件"下比较预测收益，baseline
  永远是安全动作。

代码位置（计划语法/生成/成本/选择位于 Agent 内；执行侧合并与执行器保持在下游）：

| 模块 | 职责 |
|---|---|
| `plans/descriptor.py` | 计划语法、枚举与 `validate_plan_dict` 硬校验 |
| `plans/sql.py` | SQL 结构识别与等价改写（时间范围/FROM/窗口/列契约） |
| `plans/generator.py` | 把一条查询编译为可执行计划集合 |
| `plans/cost.py` | 单条耗时估计、计划收益预测与选择 |
| `plans/integration.py` | 计划 → 决策输出字段的映射（纯函数） |
| `provider.py` | 候选策略与计划生成的内置入口（Agent 内部调用） |
| `agent.py` | `decide()` 内生成计划、选择计划、覆盖维度取值 |
| `mataof/executors/merge.py` | 子查询结果合并与结构校验（执行契约） |
| `mataof/executors/iotdb.py` | 计划执行（并行子查询、合并、失败回退） |
| `mataof/runner.py` | 设备清单加载、事实输入与追踪落盘 |

---

## 1. 计划描述符（plan grammar）

一个计划是 JSON 对象（完整示例为设备分区）：

```json
{
  "plan_id": "raw_device_split_4",
  "family": "partition",
  "query_shape": "raw_scan",
  "partition": {
    "key": "device", "parts": 4, "assignment": "round_robin",
    "time_bounds": null,
    "boundaries": null,
    "device_groups": [["root.dbl.g_0.d_0", "..."], ["..."], ["..."], ["..."]],
    "window_interval_ms": null
  },
  "aggregation": {"mode": "none", "merge": "none", "functions": [], "group_by_time": false},
  "rewrite": {"alignment": "default", "window_form": "default", "strip_order_by": false},
  "exec_params": {"result_mode": "df", "fetch_size": 10000, "parallelism": 4,
                  "session_reuse": true},
  "subqueries": ["SELECT ... FROM root.dbl.g_0.d_0, ... WHERE ...", "..."],
  "merge": {
    "mode": "rows", "axis": "horizontal", "sort_by_time": true, "descending": false,
    "time_parts": 1,
    "device_groups": [["root.dbl.g_0.d_0", "..."], "..."],
    "expected_group_columns": [["root.dbl.g_0.d_0.s_1666", "..."], "..."],
    "output_columns": ["root.dbl.g_0.d_0.s_1666", "root.dbl.g_0.d_1.s_1666", "..."]
  },
  "requirements": ["explicit_device_inventory"],
  "equivalence": "exact",
  "risk": "low",
  "cost_prior": {"parallel_efficiency": 0.30, "per_part_overhead_ms": 1.8,
                 "merge_overhead_ms": 1.5, "prior_speedup": 1.40},
  "description": "设备分区 4 组（round-robin；组内设备互斥）"
}
```

### 1.1 字段与枚举

| 字段 | 取值 | 说明 |
|---|---|---|
| `plan_id` | 字符串（≤128，不含 `\|`） | 知识库签名/策略 ID 的组成部分 |
| `family` | baseline / exec_mode / sql_rewrite / partition / partial_aggregation | 计划族 |
| `query_shape` | raw_scan / scalar_agg / window_agg / last / unknown | 生成时判定 |
| `partition.key` | time / device / hybrid | 分区轴 |
| `merge.mode` | none / rows / aggregate | 合并方式 |
| `merge.axis` | vertical / horizontal / hybrid | rows 的合并轴 |
| `merge.kind` | scalar_time / scalar_device / scalar_hybrid | aggregate 的合并类型；窗口计划在 `rows` 模式下仅携带 `window_*` 追踪标签，不参与合并函数选择 |
| `equivalence` | exact / content_equivalent | exact=逐行逐列等价；content_equivalent=内容等价、组织形态不同 |
| `risk` | low / medium / high | 成本模型的风险门控 |
| `exec_params.result_mode` | row / df | 当前生成器只产出 df（完整 DataFrame 批次消费） |

### 1.2 语法硬校验（`validate_plan_dict`）

执行器消费前必须通过校验；任何一条不满足 → 计划被拒绝（决策层丢弃并记录
原因，执行器回退原查询）：

- `plan_id` 非空、长度有界、不含 `|`（与 `strategy_id` 语法冲突）；
- `subqueries` 非空字符串列表，数量 ≤256；
- `merge` 与子查询数量一致：
  - `none` → 恰好 1 条；
  - `rows`/`aggregate` → ≥2 条；
  - `horizontal`/`scalar_device` → 设备组数 = 子查询数；
  - `hybrid`/`scalar_hybrid` → 子查询数 = 设备组数 × 时间片数；
- `exec_params`：`result_mode` 枚举、`parallelism≥1`、`fetch_size>0`；
- `partition`：键枚举、`parts≥1`、`boundaries` 严格递增、设备组非空且**互斥**
  （同一设备不能出现在两个组——否则结果重复）；
- `aggregation` 为对象且 `mode` 枚举合法；`rewrite` 为对象；
- 列契约（`expected_columns` / `expected_group_columns` / `output_columns`）
  为字符串列表且数量与分段/设备组一致；
- 未识别字段（生成器 `stats`、外部追踪元数据）不参与执行，允许保留。

> 设计取舍：校验只拒绝**会导致执行/合并失败**的已知字段取值，不做"猜测式修正"。

---

## 2. 计划族与生成规则

### 2.1 计划族

| 计划族 | plan_id | 触发条件 | 合并 |
|---|---|---|---|
| baseline | `baseline_single_df` | 恒有 | none |
| exec_mode | `exec_fetch_10000_df` | 恒有 | none |
| sql_rewrite | `align_by_device_df` | 多设备/通配，且无结构阻断 | none |
| partition（原始扫描） | `raw_time_split_{2,3,4,6,8}` | 时间范围规范且跨度 ≥1s | rows/vertical |
| partition（原始扫描） | `raw_device_split_{2,3,4,5,8,10}` | 设备集合可确定（显式列表或清单展开）且 ≥2 台 | rows/horizontal |
| partition（原始扫描） | `raw_hybrid_t{2,4}_d{2,4}` | 时间与设备分区都可行 | rows/hybrid |
| partial_aggregation（标量） | `scalar_time_split_*` / `scalar_device_split_*` / `scalar_hybrid_*` | 聚合可分解（count/sum/min/max） | aggregate |
| partial_aggregation（窗口） | `window_time_split_*` / `window_device_split_*` / `window_hybrid_*` | 窗口 GROUP BY 为显式范围 | rows（窗口不重叠） |

`ALIGN BY DEVICE` 改写不改变查询语义，但把宽表结果组织成设备×时间行
（`content_equivalent`）：

- 显式多设备列表与一级通配 FROM 均可使用（真实 IoTDB 2.0.6 复核）；
- WHERE 含完整测点路径谓词 → 引擎 701，不生成；
- LAST、已有 `ALIGN BY DEVICE`、LIMIT/OFFSET/HAVING/UNION/DISTINCT/非时间排序
  → 不生成；
- 单设备（设备数 <2）或点查（时间跨度 0）→ 不生成。

### 2.2 结构门禁（不生成，而不是生成后失败）

| 事实 | 阻止的计划 |
|---|---|
| LIMIT / OFFSET / HAVING / UNION / DISTINCT / LAST | 所有分区/部分聚合计划 |
| `ALIGN BY DEVICE` 已存在 | 该改写计划 |
| FROM 缺失/非设备级/子查询 | 所有分区（保守：设备级 FROM 无法确定） |
| 通配 FROM 且无设备清单/清单未通过完整性校验 | 设备/混合分区（时间分区仍可用） |
| WHERE 完整测点路径谓词 | 设备分区、ALIGN 改写 |
| ORDER BY 非时间列 | 所有分区（合并无法保证顺序） |
| 时间范围非规范闭区间（含 OR/无法解析） | 时间/混合分区 |
| 时间跨度 <1s 或 0 | 分区（多轮往返无收益） |
| 标量聚合含 avg/stddev/distinct 等不可分解函数 | 时间/混合分区（设备分区仍可） |
| 窗口 GROUP BY 与 WHERE 时间范围同时存在 | 时间/混合分区（保守） |

### 2.3 等价性规则

- 时间分区：半开区间 `[b_i, b_{i+1})`；原始扫描最后一段闭合，窗口聚合保持
  半开；窗口按 interval 对齐，滑动窗口不分区；
- 设备分区：round-robin 分组，FROM 路径替换为组内设备；组间列互斥；
- 混合：设备组 × 时间区间；组内纵向、组间横向；
- 标量部分聚合：`count/sum→sum`、`min→min`、`max→max`；
- 列契约：
  - `expected_columns`（纵向分段列集合）与 `expected_group_columns`（横向
    每组列集合）防止子查询返回错设备/错别名；
  - `output_columns` 只在 **FROM 为显式设备列表**时给出，用于恢复原生
    "测点优先"列序；通配 FROM 的引擎展开顺序不可复现，只校验列集合。

### 2.4 计划数量（q1~q12，带 100 台设备清单）

> 设备清单必须**完整**：设备分区按清单展开子查询，清单缺设备时合并结果会
> 静默丢列（`verify_merged` 只按计划自身展开的子查询校验列契约，发现不了）。
> 生成用 `scripts/gen_device_inventory.py`（SHOW + COUNT 双校验，不一致不写
> 文件）；运行期 `validate_device_inventory` 再用 `COUNT DEVICES` 核对一次，
> 数量不一致的模式被剔除并打印原因（全部剔除 → 不生成设备/混合分区计划）。

| 查询 | 计划数 | 说明 |
|---|---:|---|
| q1 | 2 | 点查（通配 FROM）；无分区、无对齐改写 |
| q2/q4/q5/q7/q9/q11 | 11 | 2 台设备：时间 5 + 设备 1 + 混合 2 + base 3 |
| q3/q6/q10 | 7 | 时间 5 + base 2（WHERE 含完整测点路径 → 设备分区/对齐改写被门禁阻断） |
| q8 | 2 | LAST 查询：只有 baseline + fetch |
| q12 | 18 | 100 台设备：时间 5 + 设备 6 + 混合 4 + base 3 |

---

## 3. 成本/收益模型（`cost.py`）

### 3.1 单条基线耗时 `single_ms`

优先级（前者可用则不再往下）：

1. 相似历史中 `baseline_single_df` 的加权平均延迟（`HistoryEvidence`），
   **仅当样本充分**：≥3 次且 ≥ 同查询形状全部计划证据的 10%。
   baseline 是安全动作、选择性执行，样本稀少且偏冷启动（如 q1：9 次中
   1 次 1542ms，而常态约 12ms），不能当常态基线；
2. baseline 不充分/缺失时，同查询形状下**其他计划**的加权实测典型值
   （至少 3 次执行；计划族语义等价，实测方向以护栏/中性为主），
   来源标注 `history_plan_typical`；
3. 调用方提供的数据库状态延迟字段：
   `baseline_latency_ms` / `avg_query_latency_ms` / `avg_latency_ms`；
4. 结构化先验（明确标注 prior，不声称实测）：
   - 点查/跨度 0 → 8ms；跨度 <60s → 12ms；
   - 可能大扫描 → 45ms；跨度 ≥60s 且设备 ≥10 → 50ms；
   - 其他 → 30ms。

样本充分性口径与执行监控共用（`mataof.similarity.baseline_sample_adequate`）。
稀疏冷启动 baseline 既不作为单条基线，也不参与 baseline 计划的预测修正
（见 3.3），避免虚增收益、诱导激进计划。

**证据口径（同形状优先）**：本节的"相似历史"（以及 3.3 的历史修正）优先
限定在"同查询形状 + 同数据规模"的实测子集（`same_shape_evidence`）：
除时间跨度允许 10% 相对波动（数据相关的边界抖动）外紧凑特征完全一致，
聚合函数集合与测点集合按集合相等，数据规模相等（缺失时设备规模相差 ≤10%）；
子集执行次数 ≥3 才启用，不足时退回全部相似记录（新查询形状仍可借用历史）。
近邻形状（q3/q10 测点不同、q7/q11 窗口间隔/聚合函数不同）的绝对延迟不得
混入当前形状的成本预测——否则 q3 的 3.539ms 会把 q10 的 4.10ms 基线压低。

### 3.2 计划预测延迟

```text
prior_speedup = cost_prior.prior_speedup        # 经验先验（上限）
formula_speedup = parallel_gain / (1 + overhead)

parallel_gain = 1 + eff × load_factor × (parallel - 1)
overhead      = (per_part × parts + merge_ms) / single_ms × high_load_factor
merge_ms      = cost_prior.merge_overhead_ms + min(3.0, 0.05 × 列契约数)

predicted = single_ms / min(prior_speedup, formula_speedup)   # 分区/部分聚合
predicted = single_ms / prior_speedup                          # baseline/exec/sql_rewrite
```

- `eff`：并行效率先验（默认 0.30；窗口时间分区 0.20）；
- `load_factor`：高负载 0.5、低负载 1.05、未知/正常 1.0；
- `high_load_factor`：高负载合并/并行开销放大 1.25×；
- 经验先验是**上限**：理论公式只能把预期调低，不能凭空超过实测先验。

### 3.3 历史修正

相似历史中该 `plan_id` 的加权延迟按证据量混合：

```text
alpha = min(0.60, 0.15 + 0.15 × count)
predicted = (1 - alpha) × predicted + alpha × history_latency
```

无该计划历史时不做调整（只记录"无证据"）。baseline 计划的历史若样本
不充分（<3 次或不足同形状计划证据的 10%），只记录证据条数/原始延迟，
不用于修正预测——避免一次冷启动观测把 baseline 预测拉高数百毫秒。

### 3.4 反馈与风险修正

```text
effective = predicted + eval_penalty + risk_penalty
eval_penalty   = +min(3.0, 1 + (0.5 - ratio) × 4)   # ratio < 0.5
               = -0.5                                # ratio ≥ 0.90（样本数达标）
               = 0                                   # 其他/样本不足
risk_penalty   = {low: 0, medium: 0.5, high: 2.0}
```

### 3.5 选择门槛与安全动作

```text
required = max(min_saving_ms, min_saving_ratio × single_ms)
required ×= 1.15   # medium 风险且无历史证据
required ×= 1.30   # high 风险
required += 1.0ms  # 系统高负载且计划含多子查询
saving   = single_ms - effective

可选条件：
- baseline 恒可选；
- high 风险需 ≥2 条相似历史证据；
- 非 baseline 需 saving ≥ required。
```

选择顺序：`saving` 降序 → 风险升序 → 计划族顺序 → `plan_id`（确定性）。
没有候选超过门槛 → 选 baseline（`PlanChoice.fallback=True`），baseline 永远是
安全动作。

### 3.6 选择输出

`PlanChoice` 包含：

- `chosen`（计划描述符）、`evaluations`（每个计划的预测/节省/门槛/证据/排除原因）；
- `single_ms`、`single_source`（history_baseline / history_plan_typical /
  database_state.* / prior）与 `single_count`（该估计对应的历史执行数）；
- `fallback`（是否退回 baseline）、`notes`（可追踪说明）。

决策输出 `plan_selection` 原样暴露这些字段，便于实验分析。

---

## 4. 执行与合并

### 4.1 执行路径

`IoTDBExecutor.execute()` 优先消费 `selected_strategy.execution_plan`：

1. `validate_plan_dict` 校验；非法 → 回退原查询并记录 `plan_error`；
2. 按 `exec_params.parallelism` 并行执行子查询（每个 worker slot 独立会话、
   独立 `fetch_size`）；
3. `merge_results` 按 `merge` 规格合并；`verify_merged` 做结构校验；
4. 任何异常 → `_fallback_to_query` 重跑原查询；总耗时 = 尝试 + 回退
   （不隐藏失败成本），`plan_fallback=true`、`plan_error` 可追踪。

### 4.2 指标（`ExecutionResult.metrics`）

| 指标 | 含义 |
|---|---|
| `plan_parts` / `plan_parallelism` / `plan_fetch_size` | 计划执行参数 |
| `plan_merge_mode` / `plan_merge_ms` | 合并方式与客户端合并耗时 |
| `plan_rows` / `plan_columns` | 合并结果规模 |
| `plan_fallback` | 是否回退原查询 |
| `plan_error` / `plan_attempt_ms` / `plan_fallback_ms` | 回退原因与耗时分解 |
| `response_time_ms` | 计划成功=执行+合并；回退=尝试+回退（总成本） |

### 4.3 合并模式

| 模式 | 语义 | 关键校验 |
|---|---|---|
| `none` | 单条执行，原样返回 | 恰好 1 个结果 |
| `rows/vertical` | 时间分段纵向拼接 | 各段列集合一致；Time 不重复；按计划排序 |
| `rows/horizontal` | 设备组横向拼接（Time 为索引） | 组间列不重叠；列属于该组设备 |
| `rows/hybrid` | 组内纵向、组间横向 | 段数 = 组数 × 时间片 |
| `aggregate/scalar_time` | 逐列按函数合并为一行 | 每段恰好 1 行；列名可识别函数 |
| `aggregate/scalar_device` | 设备组结果横向拼接 | 组间列不重叠 |
| `aggregate/scalar_hybrid` | 组内按函数合并、组间横向 | 段数/列契约一致 |

`output_columns` 存在时，合并结果必须**逐列同序**；列集合不一致 → `MergeError`
→ 执行器回退（绝不返回错列结果）。

---

## 5. 决策层集成（`integration.py` + `agent.py`）

计划生成是 `OptimizationDecisionAgent.decide()` 的固有步骤：输入含原始
`query` 且 `query_analysis` 有效时，Agent 先用内置生成器编译 `candidate_plans`
（失败只记录、不拖垮决策），再在维度决策之后做计划选择。外部显式注入
`candidate_plans`（含空列表）视为覆盖，用于测试/消融。

1. `select_plan` 在全部候选计划中按成本模型选择；
2. `decision.plan`：计划决策条目（与维度条目同构 + family/risk/predicted_ms/
   single_ms/saving_ms/fallback）；
3. `selected_strategy.execution_plan`：完整计划描述符（执行器优先消费）；
4. `plan_selection`：全部评估明细与单条基线来源；
5. `strategy_id` 追加 `plan=<plan_id>`；
6. `decision.alignment/fetch_size` 被**显式覆盖**为计划实际取值：
   - alignment：计划 rewrite 为 `align_by_device` → `align_by_device`；
     否则在维度适用时写 `time_aligned`；
   - fetch_size：计划 `exec_params.fetch_size >1024` → `fetch_large`，否则
     `fetch_default`；
7. `strategy_parameters` 移除被覆盖维度的旧参数，写入 `plan` 摘要与实际
   `fetch_size`；
8. `equivalent_sql`：单子查询且 `merge.mode=none` → 该 SQL；否则 `null`
   （由 `execution_plan` 承载）；
9. `decision_status=success`（baseline 也是有效动作）；`fallback_strategy=""`；
   `overall_confidence` 取计划证据强度（见下）。

计划决策置信度（0.30~0.90）：

```text
baseline: 0.60
  +0.10  单条延迟不是固定先验（来自历史/数据库状态）
  +0.03×历史条数（≤0.10）
非 baseline: 0.55
  +min(0.15, 0.5×预测节省比例)
  +0.05×历史条数（≤0.15）
  +0.05×分析置信度
  -0.05（medium）/ -0.15（high）
  非 baseline 且单条基线来自固定先验时封顶 0.75
```

兼容性：直接调用 `decide()` 但**不提供 `query`**（仅有 `query_analysis`）时
无法编译计划，Agent 走纯维度策略路径，输出与旧版逐字段一致；通过 Runner
运行时始终有 `query`，因此计划能力默认生效。`planning.enabled=false` 或显式
传入 `candidate_plans=[]` 可关闭自动生成（用于消融/对照）。

---

## 6. 知识沉淀

- 决策输出 `decision.plan.strategy` 经 `assemble_record` 写入
  `strategy.by_dimension.plan`，`strategy_id` 同时含 `plan=<plan_id>`；
- SQLite 评价表签名：`query_type|plan|<plan_id>`（与既有维度共享同一张表）；
- 计划选择时读取：
  - 相似历史中该 `plan_id` 的加权延迟（`_history_adjust`）；
  - 评价表中该 `plan_id` 的样本数与成功率（`_evaluation_feedback`）；
- 冷启动：旧知识记录没有 `plan` 维度 → 计划历史为空；可用
  `database_state.baseline_latency_ms` 提供单条基线，或先跑 baseline/exec 计划
  积累证据。

---

## 7. 配置与消融

计划能力默认随决策 Agent 生效；`RunnerConfig` 只配置事实输入与生成上限：

```json
{
  "device_inventory_file": "data/device_inventory.json",
  "planning": {"enabled": true},
  "plan_max_parallelism": 8,
  "plan_min_saving_ms": 1.0,
  "plan_min_saving_ratio": 0.03
}
```

- `device_inventory`：`list[str]`（全局清单）或 `dict[pattern, list[str]]`
  （通配模式 → 清单）；文件读取失败/格式无效 → 安全降级（不生成设备分区计划）；
- `device_inventory_file`：推荐 `data/device_inventory.json`，由
  `scripts/gen_device_inventory.py` 生成（`SHOW DEVICES` 收集 +
  `COUNT DEVICES` 核对，不一致退出码 2 且不写文件）；运行期
  `validate_device_inventory` 按模式再核对设备数，不完整/查询失败的模式
  剔除（dict 才算可校验；全局 list 原样透传并提示无法核对）；
- `planning.enabled`：默认 true；置 false 可关闭自动计划生成（消融/对照）；
- `plan_max_parallelism`：子查询并行度上限；
- `plan_min_saving_ms` / `plan_min_saving_ratio`：可选的净收益门槛覆盖；
- 直接调用 `decide()` 时可用 `planning.enabled=false` 关闭自动计划生成，
  或用 `candidate_plans=[]` 显式覆盖；两种方式都只影响计划候选，维度策略
  路径保持可用（消融/对照口径）；
- `fixed_strategy` 模式跳过决策与计划（Baseline 2 对照口径不变）；
- 计划生成耗时计入 `decision_ms`；执行/合并耗时计入执行阶段。

---

## 8. 可执行性验证（真实 IoTDB 2.0.6）

对 q1~q12 的首条语句逐计划执行子查询、合并、结构校验，并与原生查询
（同一客户端、DataFrame 全量消费）做规范化比较（Time 置首、列排序、按 Time
升序）：

| 查询 | 计划数 | 结果 |
|---|---:|---|
| q1 | 2 | 全部 exact 等价（EQ=True） |
| q2/q4/q5/q7/q9/q11 | 11 | 全部 exact 计划等价（含 count/sum 部分聚合、窗口聚合、2/3/4/6/8 时间片） |
| q3/q6/q10 | 7 | 全部 exact 等价（含空结果分段；全路径过滤查询） |
| q8 | 2 | LAST 查询：baseline/fetch 等价；不生成分区计划 |
| q12 | 18 | 全部 exact 等价（100 台设备、101 列；时间/设备/混合分区） |

`align_by_device_df` 是唯一声明的 `content_equivalent`：结果内容相同，但宽表
→ 长表（如 q12 1200×101 → 120000×3），不参与 exact 等价比较；逐计划执行时
按计划声明的等价性校验。

q12 pilot 计时（`bench_q12_plans.py`，15 次重复，客户端墙钟含合并，热缓存即
同进程连续执行；仅用于方向性判断，正式实验仍执行冷缓存协议）：

| 计划 | 中位耗时 | 相对单条（中位数） |
|---|---:|---:|
| `baseline_single_df` | 52.26ms | 1.00× |
| `raw_time_split_2` | 48.60ms | 1.08× |
| `raw_time_split_4` | 52.78ms | 0.99× |
| `raw_time_split_8` | 70.35ms | 0.74× |
| `raw_device_split_2` | 38.99ms | 1.34× |
| `raw_device_split_4` | 35.25ms | 1.48× |
| `raw_device_split_5` | 35.99ms | 1.45× |
| `raw_device_split_10` | 50.75ms | 1.03× |

另一次变体探测（3 次重复的平均耗时，单独 run，基线 38.26ms）中：
`device_split_3` 1.15×、`device_split_5` 1.13×、`device_split_6` 0.94×；
混合计划均慢于单条（`t2_d3` 0.85×、`t4_d3` 0.70×、`t2_d4` 0.79×、
`t4_d5` 0.55×、`t4_d8` 0.39×）。

结论（诚实口径）：设备分区在宽设备扫描上有真实收益（~1.3-1.5×），时间分区
收益有限，混合分区在该形状上不占优；成本模型的先验方向与实测一致，历史证据
可进一步修正，但先验不是性能承诺。

---

## 9. 局限与后续

1. 当前不修改 IoTDB 源码：`result_mode` 只生成 `df`（完整批次消费），
   行模式、会话级并发 hint 等需要引擎配合的杠杆暂不进入候选；
2. LAST 查询不生成分区计划；不可分解聚合（avg/stddev/distinct）不生成
   时间/混合分区的部分聚合计划（设备分区仍可：各设备列独立计算，
   横向拼接不跨设备再聚合）；
3. 窗口 GROUP BY 与 WHERE 时间范围同时存在时不生成时间分区（保守）；
4. 通配 FROM 的引擎列序不可复现 → 只校验列集合，不声明逐列同序；
5. 成本先验来自 pilot 实测方向，不是统计模型；收益依赖查询形状与数据分布；
6. 计划感知的 LLM 选择（LLM 在计划间选择）尚未启用：当前 LLM 仍只在
   alignment/fetch_size 维度内选择，计划选择保持确定性——避免 LLM 解锁未经
   证据支持的高风险动作；
7. 如后续证明客户端杠杆不足，再评估内核级改动（per-query DOP、扫描级聚合
   强制），并保持 pinned commit + patch + 等价性测试的实验协议。
