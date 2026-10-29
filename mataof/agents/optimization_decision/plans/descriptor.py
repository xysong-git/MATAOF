"""计划描述符（plan grammar）与序列化契约。

计划（plan）是决策 Agent 的可执行动作单元，由 Agent 内置生成器组装、成本模型
只做选择。一个计划包含：

- 计划族（family）：baseline / exec_mode / sql_rewrite / partition /
  partial_aggregation；
- 查询形状（query_shape）：raw_scan / scalar_agg / window_agg / last /
  order_by / unknown；
- 分区规格（partition）：时间/设备/混合分区；
- 聚合规格（aggregation）：无聚合 / 部分聚合，以及合并函数；
- SQL 改写（rewrite）：对齐方式等语义等价改写；
- 执行参数（exec_params）：结果消费模式、fetch_size、并行度；
- 子查询（subqueries）：真正送到数据库的 SQL 列表；
- 合并规格（merge）：执行器如何把多个子查询结果合并成一个结果；
- 需求（requirements）、等价性（equivalence）、风险（risk）、成本先验。

设计原则：**只有编译器能生成子查询与合并规格的计划才会进入候选**——保证
"策略可执行"不是口号，而是生成时的硬门禁。决策 Agent 不做 SQL 生成/改写，
只从本模块描述的候选里选择。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

PLAN_FAMILIES = (
    "baseline",             # 原查询单条执行
    "exec_mode",            # 执行级参数（df/批次大小/会话复用）
    "sql_rewrite",           # 语义等价 SQL 改写（对齐/窗口形式等）
    "partition",             # 时间/设备/混合分区
    "partial_aggregation",   # 分区 + 部分聚合 + 合并
)

QUERY_SHAPES = (
    "raw_scan",       # 原始行扫描
    "scalar_agg",     # 标量聚合（无 GROUP BY / 无窗口）
    "window_agg",     # 时间窗口聚合
    "last",           # LAST 查询
    "unknown",
)

PARTITION_KEYS = ("time", "device", "hybrid")
RESULT_MODES = ("row", "df")
MERGE_MODES = ("none", "rows", "aggregate")
MERGE_AXES = ("vertical", "horizontal", "hybrid")
AGGREGATE_MERGE_KINDS = ("scalar_time", "scalar_device", "scalar_hybrid")
EQUIVALENCE_KINDS = ("exact", "content_equivalent")
RISK_LEVELS = ("low", "medium", "high")
MAX_SUBQUERIES = 256
MAX_PLAN_ID_LEN = 128


@dataclass(frozen=True)
class ExecParams:
    """执行级参数（不改变 SQL 语义）。"""

    result_mode: str = "df"
    fetch_size: Optional[int] = None
    parallelism: int = 1
    session_reuse: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PartitionSpec:
    """分区规格。

    time_bounds：原始时间范围 [start, end]（毫秒，闭区间语义）；窗口聚合时
    为窗口范围 [start, end) 的 start/end。
    boundaries：时间分区的边界列表（含首尾）；设备分区时为 None。
    device_groups：设备分组（每组是完整设备路径列表）；时间分区时为 None。
    """

    key: str
    parts: int
    assignment: str = "range"
    time_bounds: Optional[tuple[int, int]] = None
    boundaries: Optional[tuple[int, ...]] = None
    device_groups: Optional[tuple[tuple[str, ...], ...]] = None
    window_interval_ms: Optional[int] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        if data.get("time_bounds") is not None:
            data["time_bounds"] = list(data["time_bounds"])
        if data.get("boundaries") is not None:
            data["boundaries"] = list(data["boundaries"])
        if data.get("device_groups") is not None:
            data["device_groups"] = [list(g) for g in data["device_groups"]]
        return data


@dataclass(frozen=True)
class AggregationSpec:
    """聚合规格。

    mode: none / partial
    merge: none / rows / sum / min / max / count（标量聚合的合并方式；
           窗口聚合通常用 rows，因为窗口不重叠）
    functions: 原查询的聚合函数（仅事实性记录）
    group_by_time: 是否按时间窗口分组
    """

    mode: str = "none"
    merge: str = "none"
    functions: tuple[str, ...] = ()
    group_by_time: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        data["functions"] = list(self.functions)
        return data


@dataclass(frozen=True)
class RewriteSpec:
    """语义等价改写规格。"""

    alignment: str = "default"       # default / time_aligned / align_by_device
    window_form: str = "default"     # default / explicit_range
    strip_order_by: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PlanDescriptor:
    """一个可执行计划的完整描述（JSON 可序列化）。"""

    plan_id: str
    family: str
    query_shape: str
    partition: Optional[PartitionSpec]
    aggregation: AggregationSpec
    rewrite: RewriteSpec
    exec_params: ExecParams
    subqueries: tuple[str, ...]
    merge: dict
    requirements: tuple[str, ...] = ()
    equivalence: str = "exact"
    risk: str = "low"
    cost_prior: dict = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "family": self.family,
            "query_shape": self.query_shape,
            "partition": self.partition.to_dict() if self.partition else None,
            "aggregation": self.aggregation.to_dict(),
            "rewrite": self.rewrite.to_dict(),
            "exec_params": self.exec_params.to_dict(),
            "subqueries": list(self.subqueries),
            "merge": dict(self.merge),
            "requirements": list(self.requirements),
            "equivalence": self.equivalence,
            "risk": self.risk,
            "cost_prior": dict(self.cost_prior),
            "description": self.description,
        }


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _validate_merge(merge: dict, n_subqueries: int) -> Optional[str]:
    """合并规格必须与子查询数量/分区形状一致（否则执行期必然失败）。"""
    mode = merge.get("mode")
    if mode == "none":
        if n_subqueries != 1:
            return f"merge.mode=none 需要恰好 1 条子查询，实际 {n_subqueries}"
        return None

    if mode == "rows":
        axis = merge.get("axis")
        if axis not in MERGE_AXES:
            return f"merge.axis 非法：{axis!r}"
        if n_subqueries < 2:
            return "分组合并至少需要 2 条子查询"
        groups = merge.get("device_groups")
        if axis in ("horizontal", "hybrid"):
            if not isinstance(groups, list) or len(groups) < 2:
                return "横向/混合合并需要至少 2 个设备组"
            if not all(_string_list(g) and g for g in groups):
                return "device_groups 必须是非空字符串列表的列表"
            if axis == "horizontal" and len(groups) != n_subqueries:
                return "横向合并的设备组数与子查询数不一致"
        if axis == "hybrid":
            tp = merge.get("time_parts")
            if not _positive_int(tp):
                return "混合合并需要正整数 time_parts"
            if len(groups) * tp != n_subqueries:
                return (f"混合合并子查询数 {n_subqueries} ≠ "
                        f"设备组 {len(groups)} × 时间片 {tp}")
        expected = merge.get("expected_columns")
        if expected is not None and not _string_list(expected):
            return "merge.expected_columns 必须是字符串列表"
        expected_groups = merge.get("expected_group_columns")
        if expected_groups is not None:
            if not isinstance(expected_groups, list) \
                    or not all(_string_list(x) for x in expected_groups):
                return "merge.expected_group_columns 必须是字符串列表的列表"
            if len(expected_groups) != len(groups or []):
                return "merge.expected_group_columns 与设备组数不一致"
        return None

    # aggregate
    kind = merge.get("kind")
    if kind not in AGGREGATE_MERGE_KINDS:
        return f"聚合合并缺少合法 kind（实际 {kind!r}）"
    if n_subqueries < 2:
        return "聚合合并至少需要 2 条子查询"
    if kind == "scalar_time":
        expected = merge.get("expected_columns")
        if expected is not None and not _string_list(expected):
            return "merge.expected_columns 必须是字符串列表"
    else:
        groups = merge.get("device_groups")
        if not isinstance(groups, list) or len(groups) < 2 \
                or not all(_string_list(g) and g for g in groups):
            return "设备聚合合并需要至少 2 个非空设备组"
        if kind == "scalar_device" and len(groups) != n_subqueries:
            return "设备聚合合并的设备组数与子查询数不一致"
        if kind == "scalar_hybrid":
            tp = merge.get("time_parts")
            if not _positive_int(tp):
                return "混合聚合合并需要正整数 time_parts"
            if len(groups) * tp != n_subqueries:
                return "混合聚合合并的子查询数与设备组×时间片不一致"
        expected_groups = merge.get("expected_group_columns")
        if expected_groups is not None:
            if not isinstance(expected_groups, list) \
                    or not all(_string_list(x) for x in expected_groups):
                return "merge.expected_group_columns 必须是字符串列表的列表"
            if len(expected_groups) != len(groups):
                return "merge.expected_group_columns 与设备组数不一致"
    return None


def _validate_partition(partition: dict) -> Optional[str]:
    if partition.get("key") not in PARTITION_KEYS:
        return f"未知分区键：{partition.get('key')!r}"
    if not _positive_int(partition.get("parts")):
        return "partition.parts 必须是正整数"
    tb = partition.get("time_bounds")
    if tb is not None:
        if not (isinstance(tb, list) and len(tb) == 2
                and all(isinstance(x, int) and not isinstance(x, bool) for x in tb)):
            return "partition.time_bounds 必须是两个整数"
    bounds = partition.get("boundaries")
    if bounds is not None:
        if not (isinstance(bounds, list) and len(bounds) >= 2
                and all(isinstance(x, int) and not isinstance(x, bool) for x in bounds)):
            return "partition.boundaries 必须是至少两个整数"
        if any(bounds[i] >= bounds[i + 1] for i in range(len(bounds) - 1)):
            return "partition.boundaries 必须严格递增"
    groups = partition.get("device_groups")
    if groups is not None:
        if not (isinstance(groups, list) and len(groups) >= 2
                and all(_string_list(g) and g for g in groups)):
            return "partition.device_groups 必须是至少两个非空字符串列表"
        flat = [d for g in groups for d in g]
        if len(flat) != len(set(flat)):
            return "partition.device_groups 中设备重复（分区不互斥）"
    if partition.get("key") == "time" and partition.get("boundaries") is None:
        return "时间分区缺少 boundaries"
    if partition.get("key") == "device" and partition.get("device_groups") is None:
        return "设备分区缺少 device_groups"
    return None


def validate_plan_dict(plan: Any) -> tuple[Optional[dict], Optional[str]]:
    """校验候选计划的结构（执行器可直接消费）。返回 (plan, error)。

    这是"可执行"的语法层硬门禁：**结构上不能执行/不能合并的计划直接拒绝**，
    不做运行时猜测。校验范围：
    - plan_id/family/query_shape/risk/equivalence 枚举；
    - subqueries 非空字符串列表（数量有界），且不含 `|` 的 plan_id；
    - merge 规格与子查询数量、分区形状一致（none=1；rows/aggregate≥2；
      horizontal=设备组数；hybrid=设备组×时间片）；
    - exec_params（result_mode/parallelism/fetch_size）；
    - partition（键、parts、边界递增、设备组互斥）；
    - aggregation/rewrite（对象与枚举）；
    - 列契约字段（expected_columns / output_columns）。

    未识别字段（如生成器的 stats/description 或外部追踪元数据）不参与执行，
    允许保留——校验只拒绝会导致执行/合并失败的**已知字段**取值。
    """
    if not isinstance(plan, dict):
        return None, "计划不是对象"
    pid = plan.get("plan_id")
    family = plan.get("family")
    if not isinstance(pid, str) or not pid.strip():
        return None, "缺少 plan_id"
    if len(pid) > MAX_PLAN_ID_LEN:
        return None, f"plan_id 过长（>{MAX_PLAN_ID_LEN}）"
    if "|" in pid:
        return None, "plan_id 不能包含 '|'（与 strategy_id 语法冲突）"
    if family not in PLAN_FAMILIES:
        return None, f"未知计划族：{family}"
    shape = plan.get("query_shape")
    if shape is not None and shape not in QUERY_SHAPES:
        return None, f"未知 query_shape：{shape}"
    risk = plan.get("risk", "low")
    if risk not in RISK_LEVELS:
        return None, f"未知风险等级：{risk}"
    equivalence = plan.get("equivalence", "exact")
    if equivalence not in EQUIVALENCE_KINDS:
        return None, f"未知等价性声明：{equivalence}"

    subqueries = plan.get("subqueries")
    if not isinstance(subqueries, list) or not subqueries:
        return None, "subqueries 必须是非空列表"
    if len(subqueries) > MAX_SUBQUERIES:
        return None, f"subqueries 数量超过上限（{MAX_SUBQUERIES}）"
    if not all(isinstance(s, str) and s.strip() for s in subqueries):
        return None, "subqueries 含空/非字符串条目"

    merge = plan.get("merge")
    if not isinstance(merge, dict) or merge.get("mode") not in MERGE_MODES:
        return None, "merge.mode 非法"
    err = _validate_merge(merge, len(subqueries))
    if err:
        return None, err
    output_columns = merge.get("output_columns")
    if output_columns is not None and not _string_list(output_columns):
        return None, "merge.output_columns 必须是字符串列表"

    ep = plan.get("exec_params") or {}
    if not isinstance(ep, dict):
        return None, "exec_params 不是对象"
    if ep.get("result_mode", "df") not in RESULT_MODES:
        return None, "exec_params.result_mode 非法"
    parallel = ep.get("parallelism", 1)
    if not _positive_int(parallel):
        return None, "exec_params.parallelism 非法"
    fetch_size = ep.get("fetch_size")
    if fetch_size is not None and not _positive_int(fetch_size):
        return None, "exec_params.fetch_size 非法"

    partition = plan.get("partition")
    if partition is not None:
        if not isinstance(partition, dict):
            return None, "partition 不是对象"
        err = _validate_partition(partition)
        if err:
            return None, err
    aggregation = plan.get("aggregation")
    if aggregation is not None:
        if not isinstance(aggregation, dict):
            return None, "aggregation 不是对象"
        if aggregation.get("mode", "none") not in ("none", "partial", "full"):
            return None, "aggregation.mode 非法"
    rewrite = plan.get("rewrite")
    if rewrite is not None and not isinstance(rewrite, dict):
        return None, "rewrite 不是对象"
    requirements = plan.get("requirements", [])
    if not _string_list(requirements):
        return None, "requirements 必须是字符串列表"
    cost_prior = plan.get("cost_prior", {})
    if not isinstance(cost_prior, dict):
        return None, "cost_prior 不是对象"
    return plan, None


def plan_signature(plan: dict) -> str:
    """计划的知识库签名键：family|plan_id（与 query_type 组合使用）。"""
    return f"{plan.get('family') or 'unknown'}|{plan.get('plan_id') or 'unknown'}"
