# MATAOF

面向时序数据库（Time-Series Database, TSDB）的多智能体自适应查询优化系统。

研究对象是时序数据库查询优化问题；具体数据库系统（当前实验平台为 Apache IoTDB）
仅作为实验与实现平台。Agent 不修改数据库内核或源码，而是通过分析查询、数据库状态、
历史经验和运行时反馈，为查询生成或选择更合适的优化策略。

## 核心原则

- 所有优化决策必须以查询信息、数据库状态、历史记录或运行时反馈为依据；
- 不得编造数据库统计信息、历史记录、执行结果或系统状态；无法确定的信息必须明确标记为
  `unknown`，不得猜测为确定事实；
- 对每一个策略选择给出简洁的决策依据；优先保证策略安全性和可执行性，其次追求性能收益；
- 无法证明某个优化策略优于当前方案时，保留原方案或选择安全的 fallback；
- 每次决策保留可追踪信息（query_id 贯穿全流程），便于实验、消融与错误分析；
- 所有策略可被监控和评价，并能通过执行反馈进一步修正；
- Agent 间通信采用结构化 JSON（统一数据对象见 `mataof/schemas.py`）。

## 当前组件

### 1. Query Analysis Agent（查询分析智能体）

`mataof/agents/query_analysis/`

对输入的时序数据库查询进行语义解析、结构分析和优化相关特征提取，产出结构化查询上下文
（JSON），供后续 Optimization Decision Agent 使用。不选策略、不执行查询、不改数据库。

输出覆盖：
- 查询类型（Point / Range / Predicate Filter / Window Aggregation / Complex Composite / unknown）
- 时间特征（起止、跨度、范围等级、条件形式）
- 设备维度（设备过滤、设备数、多设备）
- 过滤条件（分类计数、组合关系、选择率——仅来自数据库提供的数据）
- 聚合特征（函数、GROUP BY / 窗口、聚合范围）
- 扫描相关（扫描估计——仅来自数据库统计信息；定性结构信号）
- 三大优化维度（Time Pruning / Filter Order / Aggregation Placement）的相关性候选陈述
- `unknown_features` 与 `analysis_confidence`

详细规格见 [docs/query-analysis-agent.md](docs/query-analysis-agent.md)。

### 2. Optimization Decision Agent（优化决策智能体）

`mataof/agents/optimization_decision/`

综合查询特征、数据库状态、系统状态与历史执行经验，在预定义的候选策略空间中为当前查询
选择优化策略（Time Pruning / Filter Order / Aggregation Placement 三维度）。

- **有界策略空间**：候选 = 输入候选 ∩ 策略目录，禁止自行创造策略
- **上下文感知**：仅凭 SQL 文本不决策（缺少 query_analysis → invalid_input）
- **历史知识是证据而非规则**：相似度（查询特征/数据库状态/系统状态）达标才引用，按相似度加权
- **风险控制**：证据不足时优先安全 baseline/fallback；高风险策略需更强证据
- 输出结构化策略（strategy_id + strategy_parameters）、逐维度 reason/confidence、
  实际使用的 evidence、整体置信度与 decision_status

详细规格见 [docs/optimization-decision-agent.md](docs/optimization-decision-agent.md)。

### 3. Execution Monitoring Agent（执行监控智能体）

`mataof/agents/execution_monitoring/`

监控查询的真实执行情况，采集运行指标（响应时间、P50/P95/P99、吞吐、CPU/内存/IO），
建立 Query + Selected Strategy + Actual Execution Result 的关联，生成结构化执行反馈。

- **事实采集 Agent**：只回答"实际执行发生了什么"，不回答"下一步该用什么策略"
- 所有指标必须来自真实执行环境/监控接口；无法获得 → null/unknown，绝不虚构
- baseline 比较（无 baseline 不生成比较结果）+ performance_change 方向标签
- 策略效果判断（improved / degraded / unchanged / failed / insufficient_evidence，
  基于实测；单次执行不推断永久有效）
- 异常识别（超时/失败/延迟飙升/资源异常/偏离历史表现）——只记录，不改策略

详细规格见 [docs/execution-monitoring-agent.md](docs/execution-monitoring-agent.md)。

### 4. Knowledge Memory Agent（知识记忆智能体）

`mataof/agents/knowledge_memory/`

历史查询、查询特征、优化策略、数据库状态、系统状态与实际执行结果的
结构化存储、检索、关联与更新，为 Optimization Decision Agent 提供历史决策依据。

- **append-only 存储**：不修改、不删除历史记录（原子写 JSON 持久化）
- 每次完整执行装配为一条记录（Query + Features + Context + Strategy + Execution + Timestamp）
- 检索：Strong/Moderate/Weak 三层匹配（与决策 Agent 共用同一套多维相似度口径，
  非 SQL 文本匹配）；成功策略模式提取（≥3 次且成功率 ≥0.6）；失败经验保留汇总
- 反馈更新：成功/失败经验标记、累计策略评价、priority change（单次异常不否定历史）
- 检索结果可直接作为决策 Agent 的 `historical_records` 输入（端到端已验证）

详细规格见 [docs/knowledge-memory-agent.md](docs/knowledge-memory-agent.md)。

## 快速开始

```bash
pip install -r requirements.txt        # sqlglot（解析）、pytest（测试）
pip install -e .                       # 可选：安装 mataof 命令（或用 python -m mataof）

# 正式入口：运行一条查询（四 Agent 闭环）
mataof run "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000"

# 真实 IoTDB 执行（先启动 IoTDB，配置见 config.iotdb.example.json）
mataof run "SELECT s_0 FROM root.test.g_0.d_0 WHERE time >= 1640966400000 AND time <= 1640966650000" \
           --config config.iotdb.example.json

# 批量运行 / 检索知识 / 查看知识库
mataof run --file queries.sql --config config.json
mataof retrieve "SELECT AVG(s_0) FROM root.test.d_0 GROUP BY ([0, 1000000), 1h)"
mataof stats

# 测试
python3 -m pytest tests/ -v
```

正式运行手册（配置文件、执行层对接、追踪输出）见 [docs/cli.md](docs/cli.md)；
各 Agent 的输入/输出契约见 docs/ 下的规格文档。

## 目录结构

```
MATAOF/
├── mataof/
│   ├── schemas.py                    # 统一数据对象、输出契约、历史记录契约（模板/枚举/阈值）
│   └── agents/
│       ├── query_analysis/
│       │   ├── agent.py              # 编排入口：解析 → 特征 → 相关性 → 置信度
│       │   ├── parser.py             # sqlglot 包装 + IoTDB 特殊语法预处理
│       │   ├── relevance.py          # 特征 → 优化维度相关性陈述（只述候选）
│       │   ├── confidence.py         # 确定性置信度公式
│       │   └── features/             # 六类特征提取器
│       │       ├── base.py           #   公共工具（条件分解、引用重建、时间值解析）
│       │       ├── query_type.py     #   任务 1：查询类型识别
│       │       ├── time.py           #   任务 2：时间特征
│       │       ├── device.py         #   任务 3：设备维度
│       │       ├── filter.py         #   任务 4：过滤条件
│       │       ├── aggregation.py    #   任务 5：聚合特征
│       │       └── scan.py           #   任务 6：扫描相关
│       └── optimization_decision/
│           ├── agent.py              # 编排：评估 → 门控 → 选择/回退 → 证据 → 置信度
│           ├── catalog.py            # 有界策略目录（需求/风险/baseline）
│           ├── context.py            # 输入规范化与派生信号
│           ├── similarity.py         # 历史记录相似度与加权（证据而非规则）
│           ├── scoring.py            # 候选评分 + 风险门控 + 维度内选择
│           └── confidence.py         # 维度/整体置信度公式
│       └── execution_monitoring/
│           ├── agent.py              # 编排：采集 → 比较 → 判断 → 异常 → 反馈
│           ├── normalize.py          # 指标校验与归一化（事实采集，不虚构）
│           ├── comparison.py         # baseline 比较 + performance_change 标签
│           ├── assessment.py         # 策略效果判断（单次执行不夸大）
│           └── anomalies.py          # 异常识别（只记录，不改策略）
│       └── knowledge_memory/
│           ├── agent.py              # 编排：检索 / 反馈更新 / 全库统计
│           ├── store.py              # append-only JSON 持久化存储
│           ├── record.py             # 历史记录装配（三 Agent 输出关联）
│           └── retrieval.py          # 三层匹配检索 + 成功/失败模式汇总
│   ├── similarity.py                 # 共享相似度口径（KM 检索与决策证据共用）
│   ├── runner.py                     # 正式运行入口：四 Agent 闭环 + 追踪落盘
│   ├── cli.py                        # 命令行入口（run/retrieve/stats）
│   ├── __main__.py                   # python -m mataof
│   └── executors/                    # 执行层抽象：Null / File（回放）/ IoTDB（真实执行）
├── pyproject.toml                    # 可安装（console script: mataof）
├── config.example.json               # 配置文件模板
├── tests/                            # 117 例单元测试 + 真实 IoTDB 语料鲁棒性验证
├── examples/
│   ├── analyze_example.py
│   ├── decide_example.py
│   ├── monitor_example.py
│   └── knowledge_example.py
└── docs/
    ├── cli.md                        # 运行手册（正式入口）
    ├── query-analysis-agent.md       # Query Analysis Agent 规格说明
    ├── optimization-decision-agent.md # Optimization Decision Agent 规格说明
    ├── execution-monitoring-agent.md  # Execution Monitoring Agent 规格说明
    └── knowledge-memory-agent.md      # Knowledge Memory Agent 规格说明
```

## 系统闭环

```
Query ──► Query Analysis ──► Optimization Decision ──► 执行层
            (特征提取)          (策略选择)               │
                                  ▲                     ▼
                                  │          Execution Monitoring
                                  │            (事实采集)
                                  │                     │
                                  └── Knowledge Memory ◄┘
                                       (经验存储/检索)
```

在线修正回路：监控反馈 → 知识入库 → 相似查询检索 → 作为决策证据（历史是证据而非
规则）→ 影响后续选择。
