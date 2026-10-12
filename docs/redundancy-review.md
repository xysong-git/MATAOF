# Agent 设计与流程冗余审查报告

日期：2026-10-12（H1/H2/H3 已实施并回归通过；M/L 组按计划保留或后续处理）

实施状态：
- ✅ **H1 相似度单次计算**：`matched_records_for_decision` 携带 `similarity`；
  `build_history_evidence` 优先复用（无该字段的旧格式输入回退现场计算），
  测试覆盖"预计算值被使用（monkeypatch 验证不重算）"与"兼容回退"。
- ✅ **H2 统计逻辑收敛**：公共统计函数（`percentile_nearest_rank` / `latency_stats` /
  `resource_stats`）上收至 `mataof/report.py`；`aggregate_agent.py` 与
  `aggregate_native.py` 复用（脚本加路径引导），排版也复用 `format_batch_summary`；
  空数据场景优雅输出。
- ✅ **H3 配置生成**：`scripts/gen_configs.py` 按 规模×模式 生成全部 10 份配置
  （llm 片段共享、fixed 策略共享），已重新生成并冒烟通过；
  `config.example.json` 的 llm 节补齐全部新键。

## 总览

系统整体设计无明显结构性冗余（四 Agent 职责边界清晰、异步/缓存/采样机制各司其职），
发现的问题集中在三类：**重复计算**（2 处有实际开销）、**重复实现**（3 处维护成本）、
**孤儿字段/配置漂移**（3 处）。按优先级列出如下。

---

## 高优先级（有实际开销或高维护成本）

### H1. 相似度双重计算（每条查询多算一遍 top-100）

- **证据**：KM 检索对全库算相似度（`component_similarities`）后输出
  `matched_records_for_decision`（**不携带 similarity**）；决策 Agent 的
  `build_history_evidence` 对传入记录**重新计算**相似度（`similarity.py:265`）。
- **影响**：每条查询重复计算 ~100 条相似度（当前约 3~40ms；随库增长到 top-K 后
  恒定，量不大但纯属浪费）。
- **建议**：契约小改——`matched_records_for_decision` 每条携带 `similarity`，
  决策端 `build_history_evidence` 检测已有值时跳过重算（保留对无 similarity 输入的
  兼容回退）。

### H2. 批次统计逻辑三份重复实现

- **证据**：`mataof/report.py`（CLI 每批次/本轮汇总）、`scripts/aggregate_agent.py`
  （最终汇总）、`scripts/aggregate_native.py` 各自实现 percentile/延迟统计/资源统计。
- **影响**：统计口径三处维护，已出现过的字段名漂移（avg vs avg_ms）就是信号；
  后续加指标要改三处。
- **建议**：抽出公共统计模块（如 `mataof/report.py` 供全部三方 import；
  scripts 当初为"纯 stdlib 独立"而复制，但项目内脚本无此必要），
  或至少让 aggregate_agent 复用 `report.batch_summary` 的统计函数。

### H3. 配置漂移：14+ 配置文件手工维护，config.example 已过时

- **证据**：`config.example.json` 的 llm 节缺 `sample_rate/async_enhance/
  async_workers/cache_file/preference_bonus`（实测确认）；dbs/dbm/dbl ×
  {基础, llm, fixed} 共 9 份 + local-llm 等，字段逐份复制。
- **影响**：新配置项靠手工同步，容易漏；example 与真实配置不一致会误导。
- **建议**：写一个配置生成脚本（按规模+模式组合生成，llm 片段共享），
  或在文档声明"以 config.dbs.llm.json 为准"。

---

## 中优先级（冗余但代价小）

### M1. `strategy_priority_change` 是孤儿字段

- **证据**：KM update 输出 `knowledge_update.strategy_priority_change`，但全代码
  无任何下游消费者（D1 落地时改为成功率先验，priority_change 退化为纯报告字段）。
- **建议**：保留（更新结果里的可追踪信息）但明确标注为观测字段；或在论文/实验
  统计中消费它（如漂移检测），否则考虑删除。

### M2. `matched_records` 与 `matched_records_for_decision` 双列表重叠

- **证据**：KM retrieve 同时输出两份列表（展示格式 vs 决策契约格式），内容同源。
- **建议**：保留（两个下游的字段契约不同，转换层薄）；如追求精简可只输出
  决策格式 + 展示端按需投影。

### M3. native 模式取样逻辑重复实现

- **证据**：`run_experiment.sh` 内嵌的 `sample_lines` python 代码与
  `runner.sample_items` 同语义（seq/random+种子），两份实现。
- **建议**：脚本改为调用 `python3 -m mataof` 的一个取样子命令
  （或 `python3 -c "from mataof.runner import sample_items..."`），单点维护。

### M4. `compact_features` 每条查询计算两遍

- **证据**：KM retrieve 与决策 `normalize_input` 各自对分析输出做特征压缩。
- **影响**：纯函数、微秒级，几乎无开销。
- **建议**：不必改；若做 H1 契约改造可顺带由 KM 输出携带 compact 特征。

### M5. QA 语义摘要与决策解读的内容部分重叠

- **证据**：llm 模式每条 3 次调用（QA 摘要 / 决策解读 / 合并监控+KM），前两者
  都描述"查询在做什么"与策略选择。
- **影响**：异步模式下不阻塞；同步模式省一次调用可再降 ~2s/条。
- **建议**：可选优化——QA 摘要并入决策解读调用（决策解读 prompt 已含查询特征，
  增加"先一句话描述查询语义"要求），QA 的 llm_analysis 改由该调用填充；
  需权衡"QA 输出独立于决策"的模块化价值。

---

## 低优先级 / 设计取舍记录（不算冗余，保留）

- **L1. `_execute_single` 薄包装**：只是 `_run_sql` 的别名层，可内联（1 行收益）。
- **L2. `feature_vector` 当前无消费者**：为学习模型预留的标准接口（D2 设计意图），
  保留；文档已注明。
- **L3. `LocalHTTPClient` 未启用**：双后端能力（用户当前用 OpenAI 兼容 vLLM），保留。
- **L4. `statistics()`（按 strategy_id 合并串）与 `evaluations()`（按类型×维度×策略）
  两套统计并存**：用途不同；若 M1 决定删除 priority_change，statistics 可随之简化。
- **L5. `--limit` 与 `--per-file-limit` 并存**：向后兼容，文档已说明映射关系。
- **L6. 执行层单 Session 加锁**：workers 并行收益集中在 LLM 侧（DB 执行仅 ~30ms）；
  若未来 DB 成为瓶颈再考虑 per-thread Session 池。
- **L7. 三重存储（trace JSON / run_log / 知识库）**：用途不同（审计快照/快速统计/
  持久知识），保留；全量运行时注意体积（trace 文件可考虑紧凑序列化）。

---

## 结论与建议实施顺序

1. **H1 + H3**（半日内）：相似度单次计算 + 配置生成脚本——消除实际浪费与漂移风险；
2. **H2**（半日内）：统计逻辑收敛到公共模块——为后续加指标减维护；
3. **M 组**：按需随下次功能迭代顺带处理（M3 在改脚本时一并做）；
4. L 组不动，作为设计取舍记录在案。
