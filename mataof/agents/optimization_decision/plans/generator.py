"""计划生成器：把一条查询编译为**可执行**的候选计划集合。

生成原则（与决策 Agent 的界限一致）：
- 本模块属于 Agent 内置候选生成器：只有能编译出真实子查询 + 合并规格的计划才会输出；
- 决策 Agent 只从输出中选择，不生成/改写 SQL；
- 任何结构无法确定（LIMIT/OFFSET/HAVING/UNION/DISTINCT/LAST/非时间排序/
  无法展开的通配设备/非规范时间范围/不可分解聚合）→ 该计划不生成，
  绝不做"猜测式"改写。

计划族：
- baseline：原查询单条执行（DataFrame 结果消费）；
- exec_mode：执行级参数（fetch_size）变化；
- sql_rewrite：语义等价的 SQL 改写（ALIGN BY DEVICE；结果组织形态不同）；
- partition：时间/设备/混合分区并行执行 + 合并（原始扫描）；
- partial_aggregation：分区 + 部分聚合 + 合并（标量聚合/窗口聚合）。

分区等价性规则：
- 时间分区：半开区间 [b_i, b_{i+1})，最后一个区间闭合（原始扫描）或保持
  [b_i, b_{i+1})（窗口聚合）；窗口按 interval 对齐，滑动窗口不分区；
- 设备分区：按 round-robin 分组，FROM 路径替换为组内设备；
- 混合：设备组 × 时间区间，组内按时间纵向合并、组间按时间横向合并；
- 标量聚合的时间分区：count/sum/min/max 可分解；avg/stddev/distinct 等不生成；
- 窗口聚合的时间分区：窗口不重叠，纵向拼接即可；设备分区：各设备列互斥，
  横向拼接即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .descriptor import (
    AggregationSpec,
    ExecParams,
    PartitionSpec,
    PlanDescriptor,
    RewriteSpec,
)
from .sql import (
    closed_time_range,
    expected_columns,
    expand_device_paths,
    from_clause,
    has_align_by_device,
    has_distinct,
    has_full_path_filter,
    has_having,
    has_last,
    has_limit,
    has_offset,
    has_union,
    normalize_query,
    order_by_direction,
    replace_closed_time_range,
    replace_from_paths,
    replace_window_range,
    split_devices,
    split_time,
    window_info,
)
from mataof.schemas import parse_interval_to_ms

# 计划级常量（与实测基准同一口径；不随查询变化）
DEFAULT_FETCH_SIZE = 1024
LARGE_FETCH_SIZE = 10000
MAX_DEVICE_PARTS = 10
# 分区计划的"值得做"下限：时间跨度 <1s 的查询（尤其点查）拆分会引入
# 多轮网络往返而几乎没有可并行的工作；直接不生成分区候选（安全优先）。
MIN_PARTITION_SPAN_MS = 1000
DECOMPOSABLE_FUNCTIONS = ("count", "sum", "min", "max")
_FUNCTION_MERGE = {"count": "sum", "sum": "sum", "min": "min", "max": "max"}
# ALIGN BY DEVICE 会改变结果行的组织方式：凡会改变“返回哪些行/列”的结构，
# 一律不允许追加该子句（避免把不等价结果声明为 content_equivalent）。
_ALIGN_BLOCKED = {
    "limit", "offset", "having", "union", "distinct", "last",
    "align_by_device", "unknown_shape", "no_from", "from_not_device",
    "non_time_order", "full_path_filter",
}
# 设备分区粒度：2/4/5/8/10 与此前实测一致；3 组在宽设备扫描上实测
# 与 4/5 组接近（q12：3 组 36.77ms vs 5 组 36.38ms），作为额外可选计划。
DEVICE_PARTS = (2, 3, 4, 5, 8, 10)
# 时间分区粒度：2/4/8 有 pilot 实测方向；3/6 是结构上可执行的中间粒度
# （半开区间/窗口对齐规则相同），先验按相邻粒度插值——成本模型可以选，
# 但先验只表达"方向性预期"，不是实测承诺。
TIME_PARTS = (2, 3, 4, 6, 8)


@dataclass(frozen=True)
class QueryPlanInfo:
    """一条查询在计划层面的可编译视图（只含可从 SQL/分析结果确定的事实）。"""

    sql: str
    shape: str                       # raw_scan / scalar_agg / window_agg / last / unknown
    from_paths: tuple[str, ...]
    devices: tuple[str, ...]         # 展开后的完整设备路径；无法展开 → 空
    device_count: Optional[int]      # 已知设备数（显式列表或 inventory 展开）
    time_range: Optional[tuple[int, int]]
    time_span_ms: Optional[int]      # 时间跨度（分析结果或窗口范围；未知 → None）
    time_source: str                 # where / window / none
    window_interval_ms: Optional[int]
    order: Optional[str]             # none / asc / desc / None（非时间排序）
    functions: tuple[str, ...]
    restrictions: tuple[str, ...]    # 阻止某类计划的事实（用于排除说明）
    notes: tuple[str, ...]

    @property
    def partition_blocked(self) -> bool:
        """分区计划是否被结构性事实阻止。"""
        # 注意：wildcard_inventory_missing **不**阻止分区——时间分区不需要
        # 展开设备（FROM 通配原样保留），只有设备/混合分区需要设备清单
        # （由 device_partition_ok 的 devices 检查单独拦截）。
        blocking = {
            "limit", "offset", "having", "union", "distinct", "last",
            "align_by_device", "unknown_shape", "no_from", "from_not_device",
            "non_time_order",
        }
        return bool(blocking & set(self.restrictions))

    @property
    def device_partition_ok(self) -> bool:
        return (not self.partition_blocked and self.shape != "last"
                and "full_path_filter" not in self.restrictions
                and (self.time_span_ms is None
                     or self.time_span_ms >= MIN_PARTITION_SPAN_MS)
                and not any("*" in p for p in self.devices)
                and len(self.devices) >= 2)

    @property
    def time_partition_ok(self) -> bool:
        return (not self.partition_blocked and self.time_range is not None
                and self.time_source in ("where", "window")
                and (self.time_span_ms is None
                     or self.time_span_ms >= MIN_PARTITION_SPAN_MS))


def inspect_query(query: str, analysis: Optional[dict],
                  device_inventory: Any = None) -> QueryPlanInfo:
    """从 SQL + Query Analysis 输出提取计划编译所需事实；不猜测。"""
    sql = normalize_query(query)
    qf = (analysis or {}).get("query_features") or {}
    agg = qf.get("aggregation") or {}
    devices_feature = qf.get("device") or {}
    notes: list[str] = []
    restrictions: list[str] = []

    # ---- 查询形状 ----
    if has_last(sql):
        shape = "last"
        restrictions.append("last")
    elif agg.get("has_aggregation"):
        if agg.get("has_window"):
            shape = "window_agg"
        elif not agg.get("has_group_by"):
            shape = "scalar_agg"
        else:
            shape = "unknown"
            restrictions.append("unknown_shape")
            notes.append("GROUP BY 非时间窗口（路径/列分组）暂不生成分区计划")
    elif agg.get("has_group_by"):
        shape = "unknown"
        restrictions.append("unknown_shape")
        notes.append("无聚合但存在 GROUP BY，暂不生成分区计划")
    else:
        shape = "raw_scan"

    # ---- FROM / 设备 ----
    clause = from_clause(sql)
    if clause is None:
        from_paths: tuple[str, ...] = ()
        restrictions.append("no_from")
        notes.append("FROM 缺失或为子查询，无法做设备分区")
    else:
        from_paths = tuple(clause["paths"])
        bad = [p for p in from_paths
               if len([x for x in p.split(".") if x]) > 4
               or len([x for x in p.split(".") if x]) < 2]
        if bad:
            restrictions.append("from_not_device")
            notes.append(f"FROM 路径不是设备级路径（{bad}），不做设备分区")
    if from_paths:
        expanded = expand_device_paths(list(from_paths), device_inventory)
        if expanded is None:
            devices: tuple[str, ...] = ()
            if any("*" in p for p in from_paths):
                restrictions.append("wildcard_inventory_missing")
                notes.append("FROM 含通配路径但未提供设备清单，设备分区不可用")
        else:
            devices = tuple(expanded)
    else:
        devices = ()
    device_count = len(devices) if devices else None
    if device_count is None:
        dc = devices_feature.get("device_count")
        if isinstance(dc, int) and not isinstance(dc, bool) and dc >= 0:
            device_count = dc

    # ---- 时间范围 ----
    time_range: Optional[tuple[int, int]] = None
    time_source = "none"
    window_interval_ms: Optional[int] = None
    where_range = closed_time_range(sql)
    win = window_info(sql) if shape == "window_agg" else None
    if shape == "window_agg":
        if win is not None and where_range is None:
            time_range = (win["start"], win["end"])
            time_source = "window"
            window_interval_ms = parse_interval_to_ms(win["interval"])
        elif win is not None and where_range is not None:
            notes.append("窗口 GROUP BY 与 WHERE 时间范围同时存在，暂不生成时间分区")
        else:
            notes.append("窗口 GROUP BY 形式无法确定（滑动窗口/非显式范围），不做时间分区")
    elif shape in ("raw_scan", "scalar_agg"):
        if where_range is not None:
            time_range = where_range
            time_source = "where"
        elif (qf.get("time") or {}).get("has_time_filter"):
            notes.append("时间条件形式非规范闭区间（或含 OR），不做时间分区")
    if time_range is not None and time_range[1] <= time_range[0]:
        time_range = None
        notes.append("时间范围为空，时间分区不可用")
    time_span_ms: Optional[int] = None
    if time_range is not None:
        time_span_ms = time_range[1] - time_range[0]
    else:
        ts = (qf.get("time") or {}).get("time_span")
        if isinstance(ts, int) and not isinstance(ts, bool) and ts >= 0:
            time_span_ms = ts
    if time_span_ms is not None and time_span_ms < MIN_PARTITION_SPAN_MS:
        notes.append(
            f"时间跨度 {time_span_ms}ms < {MIN_PARTITION_SPAN_MS}ms（点查/短查询），"
            "不生成分区计划（多轮往返无收益）"
        )

    # ---- 结构门禁 ----
    for flag, name in ((has_limit(sql), "limit"), (has_offset(sql), "offset"),
                       (has_having(sql), "having"), (has_union(sql), "union"),
                       (has_distinct(sql), "distinct"),
                       (has_align_by_device(sql), "align_by_device")):
        if flag:
            restrictions.append(name)
    if has_full_path_filter(sql):
        restrictions.append("full_path_filter")
        notes.append("WHERE 含完整测点路径谓词：不做设备分区/ALIGN BY DEVICE 改写（引擎 701）")
    order = order_by_direction(sql)
    if order is None:
        restrictions.append("non_time_order")
        notes.append("ORDER BY 非时间列，分区合并无法保证顺序，不做分区")

    # ---- 聚合函数 ----
    funcs = tuple(sorted({
        str(r.get("function")).lower()
        for r in (agg.get("aggregation_functions") or [])
        if isinstance(r, dict) and r.get("function")
    }))
    if shape == "scalar_agg" and funcs:
        undecomposable = [f for f in funcs if f not in DECOMPOSABLE_FUNCTIONS]
        if undecomposable:
            restrictions.append("non_decomposable_aggregation")
            notes.append(f"标量聚合含不可分解函数 {undecomposable}，不做时间分区")
    if shape == "scalar_agg":
        # count(distinct ...) 不可分解（分析结果给出 distinct 标记）
        for r in (agg.get("aggregation_functions") or []):
            if isinstance(r, dict) and r.get("distinct") is True:
                restrictions.append("distinct_aggregation")
                notes.append("聚合含 DISTINCT，不做部分聚合")
                break

    return QueryPlanInfo(
        sql=sql, shape=shape, from_paths=from_paths, devices=devices,
        device_count=device_count, time_range=time_range,
        time_span_ms=time_span_ms,
        time_source=time_source, window_interval_ms=window_interval_ms,
        order=order, functions=funcs,
        restrictions=tuple(dict.fromkeys(restrictions)),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# 计划构造
# ---------------------------------------------------------------------------

def _native_column_order_known(info: QueryPlanInfo) -> bool:
    """是否能用设备清单复现引擎原生列序。

    显式 FROM 设备列表：列序 = FROM 顺序（实测）。
    通配 FROM（root.db.g.*）：引擎按自身设备枚举顺序展开，清单顺序无法保证
    与之一致 → 不做列序契约（仅保留列集合契约），避免把“内容等价”误声明为
    “逐列同序”。
    """
    return bool(info.from_paths) and not any("*" in p for p in info.from_paths)


def _stats(info: QueryPlanInfo) -> dict:
    return {
        "source_devices": info.device_count,
        "time_span_ms": (info.time_range[1] - info.time_range[0]
                         if info.time_range else None),
        "query_shape": info.shape,
    }


def _plan(plan_id: str, family: str, info: QueryPlanInfo,
          subqueries: list[str], merge: dict,
          partition: Optional[PartitionSpec] = None,
          aggregation: Optional[AggregationSpec] = None,
          rewrite: Optional[RewriteSpec] = None,
          exec_params: Optional[ExecParams] = None,
          requirements: tuple[str, ...] = (),
          equivalence: str = "exact", risk: str = "low",
          cost_prior: Optional[dict] = None,
          description: str = "") -> dict:
    descriptor = PlanDescriptor(
        plan_id=plan_id,
        family=family,
        query_shape=info.shape,
        partition=partition,
        aggregation=aggregation or AggregationSpec(),
        rewrite=rewrite or RewriteSpec(),
        exec_params=exec_params or ExecParams(),
        subqueries=tuple(subqueries),
        merge=merge,
        requirements=requirements,
        equivalence=equivalence,
        risk=risk,
        cost_prior=cost_prior or {},
        description=description,
    )
    out = descriptor.to_dict()
    out["stats"] = _stats(info)
    return out


def _time_priors(info: QueryPlanInfo, parts: int) -> float:
    """时间分区的初始收益先验（2/4/8 来自 pilot 实测方向；3/6 为插值）。

    先验只用于"是否值得尝试"的保守判断，不是实测承诺；成本模型的理论并行
    公式只能把预期调低、不能凭空超过该上限。
    """
    devices = info.device_count
    if info.shape in ("scalar_agg", "window_agg"):
        table = {2: 0.90, 3: 0.88, 4: 0.85, 6: 0.80, 8: 0.75}
    elif devices is not None and devices >= 10:
        table = {2: 1.10, 3: 1.05, 4: 1.00, 6: 0.85, 8: 0.75}
    elif devices is not None and devices <= 2:
        table = {2: 0.80, 3: 0.75, 4: 0.70, 6: 0.62, 8: 0.55}
    else:
        table = {2: 0.95, 3: 0.93, 4: 0.90, 6: 0.85, 8: 0.80}
    return table.get(parts, 0.8)


def _device_priors(info: QueryPlanInfo, parts: int) -> float:
    if info.shape in ("scalar_agg", "window_agg"):
        table = {2: 0.95, 3: 0.93, 4: 0.90, 5: 0.90, 8: 0.85, 10: 0.80}
    elif info.device_count is not None and info.device_count >= 10:
        # q12 实测（15 reps，单条 47.25ms）：2 组 1.27×、4 组 1.40×、
        # 5 组 1.43×、8 组 0.94×；3 组补充实测 1.27×。
        table = {2: 1.25, 3: 1.38, 4: 1.40, 5: 1.42, 8: 1.02, 10: 0.94}
    else:
        table = {2: 0.95, 3: 0.93, 4: 0.90, 5: 0.90, 8: 0.85, 10: 0.80}
    return table.get(parts, 0.85)


def _hybrid_prior(info: QueryPlanInfo, tparts: int, dparts: int) -> float:
    if info.shape in ("scalar_agg", "window_agg"):
        return 0.85
    if info.device_count is not None and info.device_count >= 10:
        table = {(2, 2): 1.15, (2, 4): 1.05, (4, 2): 1.10}
        return table.get((tparts, dparts), 1.0)
    return 0.85


def _partition_merge(info: QueryPlanInfo, axis: str,
                     boundaries: Optional[list] = None,
                     device_groups: Optional[list] = None,
                     time_parts: int = 1,
                     aggregation_kind: Optional[str] = None) -> dict:
    sort_by_time = info.order in ("asc", "desc")
    # 只有标量聚合需要跨分区再聚合；窗口聚合的窗口互不重叠，按行合并即可
    # （kind 仅作为追踪标签保留）。
    merge: dict = {
        "mode": ("aggregate" if (aggregation_kind or "").startswith("scalar")
                 else "rows"),
        "axis": axis,
        "sort_by_time": sort_by_time,
        "descending": info.order == "desc",
        "time_parts": int(time_parts),
    }
    if boundaries:
        merge["boundaries"] = list(boundaries)
    if device_groups is not None:
        merge["device_groups"] = [list(g) for g in device_groups]
    if aggregation_kind is not None:
        merge["kind"] = aggregation_kind
        merge["functions"] = dict(_FUNCTION_MERGE)
    return merge


def _exec_params(parts: int, max_parallelism: int,
                 fetch_size: int = LARGE_FETCH_SIZE) -> ExecParams:
    return ExecParams(
        result_mode="df",
        fetch_size=fetch_size,
        parallelism=max(1, min(parts, max_parallelism)),
        session_reuse=True,
    )


def _base_plans(info: QueryPlanInfo, max_parallelism: int) -> list[dict]:
    """baseline / exec_mode / sql_rewrite 计划（所有查询都有）。"""
    plans: list[dict] = []
    plans.append(_plan(
        "baseline_single_df", "baseline", info,
        subqueries=[info.sql],
        merge={"mode": "none"},
        exec_params=_exec_params(1, max_parallelism, DEFAULT_FETCH_SIZE),
        equivalence="exact", risk="low",
        cost_prior={"parallel_efficiency": 0.0, "per_part_overhead_ms": 0.0,
                    "merge_overhead_ms": 0.0, "prior_speedup": 1.0},
        description="原查询单条执行（DataFrame 批次消费；安全 baseline）",
    ))
    plans.append(_plan(
        "exec_fetch_10000_df", "exec_mode", info,
        subqueries=[info.sql],
        merge={"mode": "none"},
        exec_params=_exec_params(1, max_parallelism, LARGE_FETCH_SIZE),
        equivalence="exact", risk="low",
        cost_prior={"parallel_efficiency": 0.0, "per_part_overhead_ms": 0.0,
                    "merge_overhead_ms": 0.0, "prior_speedup": 1.08},
        description="执行级参数：fetch_size=10000（减少分页往返；不改变 SQL）",
    ))
    # ALIGN BY DEVICE：只有在结果行/列组织不会被其它结构改变时才生成。
    # （LIMIT/OFFSET/HAVING/DISTINCT/UNION/LAST/非时间排序/完整路径谓词等
    # 会改变返回集合或触发引擎拒绝，一律不生成。）
    if not (_ALIGN_BLOCKED & set(info.restrictions)) \
            and not has_align_by_device(info.sql) \
            and (info.device_count is None or info.device_count >= 2) \
            and info.time_span_ms != 0:
        # 单设备无对齐收益，且会把宽表改成长表（列名变化）——不生成；
        # 点查（跨度 0）同样不改写：没有需要对齐的时间维度。
        devices = info.device_count
        if devices is not None and devices >= 10:
            prior = 0.15       # 结果按设备展开，行数随设备数爆炸（实测显著更慢）
        elif devices is not None and devices <= 5:
            prior = 1.08       # 少量设备下略优
        else:
            prior = 0.85
        aligned = f"{info.sql} ALIGN BY DEVICE"
        plans.append(_plan(
            "align_by_device_df", "sql_rewrite", info,
            subqueries=[aligned],
            merge={"mode": "none"},
            rewrite=RewriteSpec(alignment="align_by_device"),
            exec_params=_exec_params(1, max_parallelism, DEFAULT_FETCH_SIZE),
            equivalence="content_equivalent", risk="low",
            cost_prior={"parallel_efficiency": 0.0, "per_part_overhead_ms": 0.0,
                        "merge_overhead_ms": 0.0, "prior_speedup": prior},
            description="SQL 改写：追加 ALIGN BY DEVICE（结果内容等价、组织形态不同）",
        ))
    return plans


def _time_plans(info: QueryPlanInfo, max_parallelism: int) -> list[dict]:
    if not info.time_partition_ok or info.time_range is None:
        return []
    if info.shape == "scalar_agg" and (
            "non_decomposable_aggregation" in info.restrictions
            or "distinct_aggregation" in info.restrictions):
        return []
    plans: list[dict] = []
    start, end = info.time_range
    is_window = info.time_source == "window"
    prefix = {"raw_scan": "raw", "scalar_agg": "scalar",
              "window_agg": "window"}.get(info.shape, "unknown")
    for parts in TIME_PARTS:
        bounds = split_time(start, end, parts,
                            info.window_interval_ms if is_window else None)
        if bounds is None or len(bounds) < 2:
            continue
        subqueries: list[str] = []
        ok = True
        for i, (a, b) in enumerate(bounds):
            if is_window:
                sql = replace_window_range(info.sql, a, b)
            else:
                sql = replace_closed_time_range(
                    info.sql, a, b, upper_inclusive=(i == len(bounds) - 1))
            if sql is None:
                ok = False
                break
            subqueries.append(sql)
        if not ok or len(subqueries) < 2:
            continue
        kind = None if info.shape == "raw_scan" else (
            "scalar_time" if info.shape == "scalar_agg" else "window_time")
        merge = _partition_merge(info, "vertical", boundaries=[a for a, _ in bounds],
                                 time_parts=len(bounds),
                                 aggregation_kind=kind)
        if kind is not None:
            merge["kind"] = kind
        expected = expected_columns(info.sql, list(info.devices))
        if expected:
            merge["expected_columns"] = expected
            if _native_column_order_known(info):
                merge["output_columns"] = expected
        plan_id = f"{prefix}_time_split_{len(bounds)}"
        family = "partition" if info.shape == "raw_scan" else "partial_aggregation"
        risk = "low" if info.shape == "raw_scan" else "medium"
        plans.append(_plan(
            plan_id, family, info,
            subqueries=subqueries,
            merge=merge,
            partition=PartitionSpec(
                key="time", parts=len(bounds), assignment="range",
                time_bounds=(start, end),
                boundaries=tuple(a for a, _ in bounds),
                window_interval_ms=info.window_interval_ms if is_window else None),
            aggregation=AggregationSpec(
                mode="partial" if kind else "none",
                merge="rows" if info.shape == "window_agg" else
                      ("aggregate" if kind else "none"),
                functions=info.functions,
                group_by_time=(info.shape == "window_agg")),
            exec_params=_exec_params(len(bounds), max_parallelism),
            requirements=tuple(["closed_time_range"] + (
                ["window_aligned_ranges"] if is_window else [])),
            equivalence="exact", risk=risk,
            cost_prior={"parallel_efficiency": 0.30 if not is_window else 0.20,
                        "per_part_overhead_ms": 1.8,
                        "merge_overhead_ms": 1.2,
                        "prior_speedup": _time_priors(info, len(bounds))},
            description=f"时间分区 {len(bounds)} 段"
                        + ("（窗口边界对齐）" if is_window else "（半开区间）"),
        ))
    return plans


def _device_plans(info: QueryPlanInfo, max_parallelism: int) -> list[dict]:
    if not info.device_partition_ok:
        return []
    plans: list[dict] = []
    devices = list(info.devices)
    prefix = {"raw_scan": "raw", "scalar_agg": "scalar",
              "window_agg": "window"}.get(info.shape, "unknown")
    for parts in DEVICE_PARTS:
        if parts > MAX_DEVICE_PARTS:
            continue
        groups = split_devices(devices, parts)
        if groups is None or len(groups) < 2:
            continue
        subqueries = [replace_from_paths(info.sql, g) for g in groups]
        if any(s is None for s in subqueries):
            continue
        kind = None if info.shape == "raw_scan" else (
            "scalar_device" if info.shape == "scalar_agg" else "window_device")
        merge = _partition_merge(info, "horizontal",
                                 device_groups=groups, time_parts=1,
                                 aggregation_kind=kind)
        expected_groups = [expected_columns(info.sql, g) for g in groups]
        if any(expected_groups):
            merge["expected_group_columns"] = expected_groups
        expected_all = expected_columns(info.sql, devices)
        if expected_all and _native_column_order_known(info):
            merge["output_columns"] = expected_all
        plan_id = f"{prefix}_device_split_{len(groups)}"
        family = "partition" if info.shape == "raw_scan" else "partial_aggregation"
        risk = "low" if info.shape == "raw_scan" else "medium"
        plans.append(_plan(
            plan_id, family, info,
            subqueries=list(subqueries),
            merge=merge,
            partition=PartitionSpec(
                key="device", parts=len(groups), assignment="round_robin",
                device_groups=tuple(tuple(g) for g in groups)),
            aggregation=AggregationSpec(
                mode="partial" if kind else "none",
                merge="horizontal" if kind else "none",
                functions=info.functions,
                group_by_time=(info.shape == "window_agg")),
            exec_params=_exec_params(len(groups), max_parallelism),
            requirements=("explicit_device_inventory",),
            equivalence="exact", risk=risk,
            cost_prior={"parallel_efficiency": 0.30,
                        "per_part_overhead_ms": 1.8,
                        "merge_overhead_ms": 1.5,
                        "prior_speedup": _device_priors(info, len(groups))},
            description=f"设备分区 {len(groups)} 组（round-robin；组内设备互斥）",
        ))
    return plans


def _hybrid_plans(info: QueryPlanInfo, max_parallelism: int) -> list[dict]:
    if not (info.time_partition_ok and info.device_partition_ok):
        return []
    if info.shape == "scalar_agg" and (
            "non_decomposable_aggregation" in info.restrictions
            or "distinct_aggregation" in info.restrictions):
        return []
    if info.time_range is None:
        return []
    plans: list[dict] = []
    start, end = info.time_range
    is_window = info.time_source == "window"
    devices = list(info.devices)
    prefix = {"raw_scan": "raw", "scalar_agg": "scalar",
              "window_agg": "window"}.get(info.shape, "unknown")
    for tparts in (2, 4):
        bounds = split_time(start, end, tparts,
                            info.window_interval_ms if is_window else None)
        if bounds is None or len(bounds) < 2:
            continue
        for dparts in (2, 4):
            groups = split_devices(devices, dparts)
            if groups is None or len(groups) < 2:
                continue
            subqueries: list[str] = []
            ok = True
            for g in groups:
                base = replace_from_paths(info.sql, g)
                if base is None:
                    ok = False
                    break
                for i, (a, b) in enumerate(bounds):
                    if is_window:
                        sql = replace_window_range(base, a, b)
                    else:
                        sql = replace_closed_time_range(
                            base, a, b, upper_inclusive=(i == len(bounds) - 1))
                    if sql is None:
                        ok = False
                        break
                    subqueries.append(sql)
                if not ok:
                    break
            if not ok or len(subqueries) < 4:
                continue
            kind = None if info.shape == "raw_scan" else (
                "scalar_hybrid" if info.shape == "scalar_agg" else "window_hybrid")
            merge = _partition_merge(info, "hybrid",
                                     boundaries=[a for a, _ in bounds],
                                     device_groups=groups,
                                     time_parts=len(bounds),
                                     aggregation_kind=kind)
            expected_groups = [expected_columns(info.sql, g) for g in groups]
            if any(expected_groups):
                merge["expected_group_columns"] = expected_groups
            expected_all = expected_columns(info.sql, devices)
            if expected_all and _native_column_order_known(info):
                merge["output_columns"] = expected_all
            plan_id = f"{prefix}_hybrid_t{len(bounds)}_d{len(groups)}"
            family = "partition" if info.shape == "raw_scan" else "partial_aggregation"
            risk = "low" if info.shape == "raw_scan" else "medium"
            plans.append(_plan(
                plan_id, family, info,
                subqueries=subqueries,
                merge=merge,
                partition=PartitionSpec(
                    key="hybrid", parts=len(groups) * len(bounds),
                    assignment="device_x_time",
                    time_bounds=(start, end),
                    boundaries=tuple(a for a, _ in bounds),
                    device_groups=tuple(tuple(g) for g in groups),
                    window_interval_ms=info.window_interval_ms if is_window else None),
                aggregation=AggregationSpec(
                    mode="partial" if kind else "none",
                    merge="hybrid" if kind else "none",
                    functions=info.functions,
                    group_by_time=(info.shape == "window_agg")),
                exec_params=_exec_params(len(subqueries), max_parallelism),
                requirements=tuple(["closed_time_range", "explicit_device_inventory"]),
                equivalence="exact", risk=risk,
                cost_prior={"parallel_efficiency": 0.30,
                            "per_part_overhead_ms": 1.8,
                            "merge_overhead_ms": 2.0,
                            "prior_speedup": _hybrid_prior(info, len(bounds), len(groups))},
                description=f"混合分区：{len(groups)} 设备组 × {len(bounds)} 时间区间",
            ))
    return plans


def build_plan_candidates(query: str, analysis: Optional[dict],
                          device_inventory: Any = None,
                          max_parallelism: int = 8) -> list[dict]:
    """生成当前查询的全部可执行候选计划（确定性顺序）。

    device_inventory：通配设备展开清单（list[str] 或 dict[pattern, list[str]]）；
    缺失 → 通配查询不生成设备/混合分区计划（时间分区仍可用）。
    """
    info = inspect_query(query, analysis, device_inventory)
    plans = _base_plans(info, max_parallelism)
    plans.extend(_time_plans(info, max_parallelism))
    plans.extend(_device_plans(info, max_parallelism))
    plans.extend(_hybrid_plans(info, max_parallelism))
    # 去重（同一 plan_id 只保留首个）并保持确定性顺序
    seen: set[str] = set()
    out: list[dict] = []
    for p in plans:
        pid = p.get("plan_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append(p)
    return out


def plan_generation_notes(query: str, analysis: Optional[dict],
                          device_inventory: Any = None) -> list[str]:
    """生成器事实说明（用于决策 notes；不含任何策略建议）。"""
    info = inspect_query(query, analysis, device_inventory)
    notes = list(info.notes)
    if info.time_range is not None:
        notes.append(f"时间分区可用范围：{info.time_range[0]}~{info.time_range[1]}"
                     f"（来源 {info.time_source}）")
    if info.device_count is not None:
        notes.append(f"设备清单已知：{info.device_count} 台")
    return notes
