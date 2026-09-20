# Query Analysis Agent（查询分析智能体）规格说明

版本：v1.0（schema_version: 1.0）
实现：`mataof/agents/query_analysis/`

## 1. 职责与边界

**唯一核心职责**：对输入的时序数据库查询进行语义解析、结构分析和优化相关特征提取，
为后续 Optimization Decision Agent 提供结构化查询上下文。任务到"特征表示"为止。

**不负责**：优化策略选择、查询执行、SQL 修改、执行计划生成、数据库修改。

**严格限制**：
- 只依据 SQL 文本与数据库显式提供的信息（`database_state`）提取特征；
- 不虚构数据库统计信息、设备数量、数据量、历史经验；
- 无法确定的信息一律 `null` / `[]` / `"unknown"`，并在 `unknown_features` 记录原因；
- 优化相关性只陈述"属于相关候选优化维度"，不声称任何策略更优（如不输出
  "该查询应该使用 Time Pruning"，只输出 "Time Pruning 属于相关候选优化维度"）；
- Agent 是确定性纯函数：相同输入 → 相同输出（便于实验、消融与错误分析）。

## 2. 输入契约

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | str | 是 | 查询文本（IoTDB SQL；多条语句时仅分析第一条，并记录说明） |
| `query_id` | str | 否 | 查询标识，贯穿全流程的追踪键 |
| `database_state` | dict | 否 | 数据库提供的状态/统计信息 |

`database_state` 当前识别的键（未提供 → 对应字段 `unknown`，绝不估计）：

```json
{
  "statistics": {"selectivity": {"s1": 0.05, "root.sg1.d1.t1": 0.3}},
  "scan_estimate": {"time_range": [0, 1800000], "estimated_points": 120}
}
```

- `selectivity`：键为列引用（完整路径或裸列名），值为 [0,1] 的选择率；越界的值被忽略。
- `scan_estimate`：数据库给出的预计扫描时间范围（epoch 毫秒）与点数。
- 其他键当前忽略（保留扩展空间）。

## 3. 输出契约（schema v1.0）

顶层字段：`query_id`、`query`（原始查询回显）、`query_type`、`query_features`、
`optimization_relevant_features`、`unknown_features`、`analysis_confidence`、`schema_version`。

### 3.1 相对于基础模板的扩展字段

基础模板按规格给出；以下字段是规格中分析任务明确要求、但基础模板未覆盖的补充
（均标记 `# 扩展` 于 `mataof/schemas.py`）：

| 位置 | 字段 | 对应任务 |
|---|---|---|
| 顶层 | `query`（回显）、`schema_version` | 可追踪性 |
| `query_features.time` | `time_condition_form`、`time_filter_expressions` | 任务 2"时间条件形式" |
| `query_features.device` | `device_paths`、`device_condition_count` | 任务 3"设备条件数量" |
| `query_features.filter` | `conditions`、`type_counts`、`combination` | 任务 4"各过滤条件与组合关系" |
| `query_features.aggregation` | `group_by_details`、`aggregation_scope` | 任务 5"聚合涉及的数据范围" |
| `query_features.scan` | `possible_large_scan`、`has_obvious_filter_conditions`、`possible_large_intermediate_result` | 任务 6 定性信号 |

### 3.2 六类任务的提取规则

**任务 1：查询类型**（判定树，按优先级；无法准确判断 → `unknown`，不强行分类）

1. 解析失败 / 非 SELECT（SHOW 等）→ `unknown`
2. 含 UNION / 子查询（FROM 子查询、IN 子查询、EXISTS）/ 窗口函数（OVER）/ ALIGN BY DEVICE
   → `complex_composite_query`
3. 含聚合：
   - 时间窗口分组（`GROUP BY ([a,b), i)` / `GROUP BY TIME(i)`）→ `window_aggregation_query`
   - 无窗口分组 + 非时间谓词 → `complex_composite_query`（聚合+谓词复合）
   - 无窗口分组 + 仅时间范围过滤 → `complex_composite_query`（范围访问+聚合复合）
   - 无窗口分组 + 无任何过滤 → `unknown`（纯全量聚合无法在五类中准确归类）
4. 不含聚合：
   - 时间等值（单时间点）→ `point_query`
   - 时间范围 / 时间点集合（IN 列表）且无非时间谓词 → `range_query`
   - 存在非时间谓词 → `predicate_filter_query`
   - 无任何过滤 → `unknown`（无法从 SQL 判定访问意图）

**任务 2：时间特征**
- 时间过滤 = WHERE 中列名为 `time`/`timestamp`（大小写不敏感）的谓词；
- `start_time`/`end_time`/`time_span` 为 epoch 毫秒；点查询 span=0；
  时间字符串按 ISO（含时区）/`%Y-%m-%d %H:%M:%S` 解析，失败 → null + 原因；
- 时间条件形式（`time_condition_form`）：
  `equality` / `closed_range` / `half_open_left` / `half_open_right` / `open_range` /
  `between` / `lower_only` / `upper_only` / `in_list` / `in_subquery` / `or:…`（OR 析取）/
  `conflicting_range`（自相矛盾）/ 组合形式；
- 范围等级（`range_level`，阈值见 `mataof/schemas.py`，可配置）：
  span < 1h → `narrow`；1h ≤ span < 1d → `medium`；span ≥ 1d → `wide`；点查询 → `narrow`；
- UNION：各分支时间范围一致才输出；不一致 → unknown（不猜测交集/并集）。

**任务 3：设备维度**
- 设备条件来自 FROM 设备路径（IoTDB 的设备过滤通过 FROM 表达）；设备条件数量 =
  FROM 路径条目数；
- 路径含通配符 → `device_count` 为 null（不猜测匹配设备数）；
- `root` / `root.*` / `root.**` 等全库级路径不构成具体设备过滤；
- FROM 为子查询 → 设备范围 unknown。

**任务 4：过滤条件**
- 类型：`time` / `value`（完整路径测点，或裸列名出现在 SELECT/聚合参数中）/
  `tag_or_attribute`（裸列名未出现在 SELECT/聚合参数中——SQL 无法区分标签与属性，
  明确标记而非猜测）/ `subquery`（IN 子查询、EXISTS）；
- 组合关系：AND/OR 树，叶子指向 `conditions` 下标；UNION 多分支按分支分别记录；
- 选择率：只拷贝 `database_state` 提供的数据（键=列引用，值∈[0,1]），否则为空并记录原因。

**任务 5：聚合特征**
- 聚合函数 = sqlglot AggFunc + IoTDB 特有函数名（`max_value`/`min_value`/
  `first_value`/`last_value`/`extreme`/`max_by`/`min_by`/… 见 `schemas.IOTDB_AGG_FUNCTIONS`）；
- GROUP BY 形式：`time_window`（含 interval 与滑动步长，均给原始值与毫秒值）/
  `time_function`（`GROUP BY TIME(i)`）/ `path_level`（`GROUP BY LEVEL = n`）/ `columns`；
- `has_window` = 时间窗口分组 或 存在 OVER 窗口函数；
- `aggregation_scope` 只由可验证信息构成（设备路径、时间边界、窗口范围、是否含非时间过滤）。

**任务 6：扫描相关**
- `estimated_scan_range` / `scan_level`：仅在 `database_state.scan_estimate` 存在时输出，
  否则 null / `unknown`（"预计扫描范围只能在具有对应数据库统计信息时输出"）；
- `possible_large_scan`、`possible_large_intermediate_result`：仅基于 SQL 结构的定性信号
  （非代价估算）：
  - 无时间过滤 → 可能大范围扫描 = true；span 为 narrow → false；wide → true；
    medium/未知 → null（是否"大"取决于数据分布，无统计信息不下结论）；
  - UNION / 子查询 / 全范围聚合（无 GROUP BY 且无时间过滤）/ 多设备且无时间过滤
    → 可能较大中间结果 = true；有时间过滤且无 UNION/子查询 → false。

### 3.3 优化相关性陈述（`optimization_relevant_features`）

- 只基于已确认特征生成；特征 unknown → 对应维度无陈述（宁可沉默，不制造依据）；
- 语句格式统一为"…属于相关候选优化维度"，绝不出现"应该/必须使用某策略"；
- 举例：
  - `time_pruning`："查询包含明确时间过滤条件（形式 closed_range，时间跨度 25000 ms，
    范围等级 narrow），Time Pruning 属于相关候选优化维度。"
  - `filter_order`："存在 4 个非时间过滤条件（类型分布：value×4），Filter Order
    属于相关候选优化维度。"（<2 个非时间条件时不产生陈述——顺序无意义）
  - `aggregation_placement`："查询包含聚合（count），Aggregation Placement
    属于相关候选优化维度。"

### 3.4 置信度（`analysis_confidence`）

确定性公式（`confidence.py`，截断到 [0,1]，保留 2 位）：
- 解析失败 → 0.1；
- 否则从 1.0 扣减：类型 unknown −0.10；有时间过滤但起止未知 −0.10；有设备过滤但数量未知
  −0.10；存在 tag_or_attribute 歧义 −0.10；无扫描统计 −0.05；GROUP BY 形式未识别 −0.05。

## 4. 与下游 Optimization Decision Agent 的接口约定

- 下游消费 `query_features` + `optimization_relevant_features` + `unknown_features` +
  `analysis_confidence`；
- 下游在做决策前应检查 `analysis_confidence` 与 `unknown_features`：置信度低或关键特征
  unknown 时，优先选择安全 fallback，不得将 unknown 当作确定值使用；
- `query_id` 必须贯穿全流程，用于实验对齐与错误追踪。

## 5. 已知限制

- 多语句输入只分析第一条（记录在 `unknown_features`）；
- 无法从 SQL 区分标签与属性（如实标记 `tag_or_attribute`）；
- 时间字符串只支持 ISO（含时区）与 `yyyy-MM-dd HH:mm:ss` 格式；
- 定性扫描信号（`possible_large_scan` 等）不是代价估算，不得当作数据量结论使用；
- IoTDB 特有语法仅覆盖：`GROUP BY ([a,b), interval[, step])`、`GROUP BY TIME(i)`、
  `GROUP BY LEVEL = n`、`ALIGN BY DEVICE`、FROM 通配符路径；其他方言扩展按需补充预处理层。

## 6. LLM 语义增强（混合增强模式）

确定性提取是权威；LLM 只填充 `llm_analysis` 附加节（不改变任何确定性字段）：

```json
"llm_analysis": {
  "available": true,
  "semantic_summary": "查询单设备在指定时间窗口内读取测点数据。",
  "query_type_hint": "range_query",
  "condition_hints": {"t1 = 'v1'": "tag"},
  "notes": []
}
```

- `semantic_summary`：查询意图自然语言摘要（llm_generated）；
- `query_type_hint`：确定性类型为 unknown 时的 LLM 提示（**仅提示，不改写
  query_type**——"不得强行分类"原则不变）；
- `condition_hints`：tag_or_attribute 歧义条件的判别建议（tag/attribute/unclear，
  仅建议，确定性标记不变）；
- **确定性兜底**：LLM 未启用（`llm=None`/provider=none）、调用超时/失败/输出非法
  JSON → `available=false` + notes，核心输出与不启用 LLM 完全一致；
- **非确定性说明**：启用 LLM 后 `llm_analysis` 节受模型随机性影响；
  所有核心特征字段保持确定性，实验对比时以核心字段为准。

接入方式：`analyze(query, query_id, database_state, llm=llm_client)`；
客户端由共享层构造（`mataof/llm.py`，OpenAI 兼容 API / 本地 HTTP 服务双后端，
配置见 docs/cli.md 的 llm 节）。

## 7. 验证方式

- 单元测试：`pytest tests/test_query_analysis_agent.py`（37 例，覆盖六类任务与 strict 限制）；
- 真实语料鲁棒性：`pytest tests/test_sample_corpus.py`（MAPO 前序工作 5 个数据集、
  2313 条抽样语句：零崩溃、零解析失败，解析失败时优雅降级为 unknown + 置信度 0.1）。
