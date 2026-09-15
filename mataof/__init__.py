"""MATAOF：面向时序数据库的多智能体自适应查询优化系统。

研究场景为时序数据库动态查询优化，重点覆盖三类优化问题：
- Time Pruning
- Filter Order
- Aggregation Placement

所有优化决策必须以查询信息、数据库状态、历史记录或运行时反馈为依据；
无法确定的信息必须标记为 unknown，不得猜测。
"""

__version__ = "0.1.0"
