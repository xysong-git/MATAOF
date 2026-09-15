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

## 快速开始

```bash
pip install -r requirements.txt        # sqlglot（解析）、pytest（测试）

# 使用
python3 -c "
from mataof.agents.query_analysis import analyze
import json
r = analyze('SELECT AVG(s_0) FROM root.test.d_0 GROUP BY ([1640966405000, 1640976405000), 1h)',
            query_id='q1')
print(json.dumps(r, ensure_ascii=False, indent=2))
"

# 示例
python3 examples/analyze_example.py

# 测试
python3 -m pytest tests/ -v
```

## 目录结构

```
MATAOF/
├── mataof/
│   ├── schemas.py                    # 统一数据对象与输出契约（模板/枚举/阈值）
│   └── agents/
│       └── query_analysis/
│           ├── agent.py              # 编排入口：解析 → 特征 → 相关性 → 置信度
│           ├── parser.py             # sqlglot 包装 + IoTDB 特殊语法预处理
│           ├── relevance.py          # 特征 → 优化维度相关性陈述（只述候选）
│           ├── confidence.py         # 确定性置信度公式
│           └── features/             # 六类特征提取器
│               ├── base.py           #   公共工具（条件分解、引用重建、时间值解析）
│               ├── query_type.py     #   任务 1：查询类型识别
│               ├── time.py           #   任务 2：时间特征
│               ├── device.py         #   任务 3：设备维度
│               ├── filter.py         #   任务 4：过滤条件
│               ├── aggregation.py    #   任务 5：聚合特征
│               └── scan.py           #   任务 6：扫描相关
├── tests/                            # 37 例单元测试 + 真实 IoTDB 语料鲁棒性验证
├── examples/analyze_example.py
└── docs/query-analysis-agent.md      # Agent 规格说明（输入/输出契约、规则、限制）
```

## 规划中的组件

- Optimization Decision Agent：基于查询上下文生成/选择优化策略（后续开发）
- 执行反馈与在线修正回路（后续开发）
