"""计划结果合并与结构校验。

执行器把计划编译出的多个子查询结果（pandas DataFrame）交给本模块，按计划
描述符的 merge 规格合并为一个结果。这里只做**计划已声明**的合并，不做任何
隐式猜测：

- rows/vertical：时间分区（区间互斥）→ 纵向拼接，按 Time 排序；
- rows/horizontal：设备分区（设备列互斥）→ 以 Time 为索引横向拼接；
- rows/hybrid：设备组内纵向、组间横向；
- aggregate/scalar_*：标量聚合分区 → 按列函数（count/sum→sum，min→min，
  max→max）合并；设备组间横向拼接；
- mode=none：单条执行结果，原样返回。

结构校验（verify_merged）保证：
- 纵向分区的各段列集合一致；
- 横向分区的各段除 Time 外列不重叠；
- 合并后 Time 唯一且按计划要求有序；
- 标量聚合合并后恰好 1 行、列集合等于各段并集。
任何不满足 → MergeError，执行器回退到原查询（可追踪，不返回错误结果）。
"""

from __future__ import annotations

import re
from typing import Any, Optional

MERGE_MODES = ("none", "rows", "aggregate")
_TIME = "Time"
_AGG_PREFIX_RE = re.compile(r"^\s*(count|sum|min|max)\s*\(", re.IGNORECASE)


class MergeError(Exception):
    """合并失败（结构不满足/数据异常）→ 执行器应回退原查询。"""


def _pd():
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise MergeError("pandas 不可用，无法合并分区结果") from exc
    return pd


def _merge_spec(plan: dict) -> dict:
    spec = plan.get("merge")
    if not isinstance(spec, dict) or spec.get("mode") not in MERGE_MODES:
        raise MergeError(f"非法 merge 规格：{spec!r}")
    return spec


def _column_set(df) -> set:
    return {c for c in df.columns if c != _TIME}


def _ensure_same_columns(frames: list, axis: str) -> None:
    if not frames:
        raise MergeError("没有可合并的子查询结果")
    base = _column_set(frames[0])
    for i, f in enumerate(frames[1:], 1):
        if _column_set(f) != base:
            raise MergeError(
                f"{axis} 合并的列集合不一致：第 0 段与第 {i} 段不同"
            )


def _ensure_disjoint_columns(frames: list) -> None:
    seen: set = set()
    for i, f in enumerate(frames):
        cols = _column_set(f)
        overlap = seen & cols
        if overlap:
            raise MergeError(f"横向合并的列出现重叠：{sorted(overlap)[:3]}")
        seen |= cols


def _check_group_columns(frame, group: list, label: str) -> None:
    """横向分区的每段列必须属于该段设备（列名为完整路径/函数路径）。

    防止子查询返回了别的设备/别名/空列集合而静默污染合并结果。
    """
    devices = [str(d) for d in group]
    for col in _column_set(frame):
        text = str(col)
        if not any(f"{d}." in text or text == d for d in devices):
            raise MergeError(
                f"{label} 的列不属于该设备组：{text[:120]}"
            )


def _sort_time(df, descending: bool):
    pd = _pd()
    if _TIME not in df.columns or df.empty:
        return df
    try:
        df = df.sort_values(_TIME, ascending=not descending, kind="mergesort")
    except Exception as exc:
        raise MergeError(f"按 Time 排序失败：{exc}") from exc
    return df.reset_index(drop=True)


def _check_expected_columns(frame, expected, label: str) -> None:
    if not expected:
        return
    want = set(expected)
    got = _column_set(frame)
    if got != want:
        missing = sorted(want - got)[:3]
        extra = sorted(got - want)[:3]
        raise MergeError(
            f"{label} 列集合与计划契约不一致（缺少 {missing}，多出 {extra}）"
        )


def _vertical(frames: list, sort_by_time: bool, descending: bool,
              expected_columns: Optional[list] = None):
    pd = _pd()
    _ensure_same_columns(frames, "纵向")
    if expected_columns:
        for i, f in enumerate(frames):
            _check_expected_columns(f, expected_columns, f"第 {i} 段")
    df = pd.concat(frames, axis=0, ignore_index=True, sort=False) if frames else pd.DataFrame()
    if _TIME in df.columns and df[_TIME].duplicated().any():
        raise MergeError("纵向合并后 Time 出现重复（分区边界重叠）")
    if sort_by_time:
        df = _sort_time(df, descending)
    return df


def _horizontal(frames: list, sort_by_time: bool, descending: bool,
                device_groups: Optional[list] = None,
                expected_group_columns: Optional[list] = None):
    pd = _pd()
    _ensure_disjoint_columns(frames)
    if device_groups is not None:
        if len(device_groups) != len(frames):
            raise MergeError("横向分区的设备组数与子结果数不一致")
        for i, (f, g) in enumerate(zip(frames, device_groups)):
            _check_group_columns(f, list(g), f"第 {i} 段")
    if expected_group_columns is not None:
        if len(expected_group_columns) != len(frames):
            raise MergeError("横向分区的列契约数与子结果数不一致")
        for i, (f, expected) in enumerate(zip(frames, expected_group_columns)):
            _check_expected_columns(f, expected, f"第 {i} 段")
    with_time = [f for f in frames if _TIME in f.columns]
    if not with_time:
        df = pd.concat(frames, axis=1, sort=False) if frames else pd.DataFrame()
        if df.columns.duplicated().any():
            raise MergeError("横向合并后出现重复列")
        # 多段 concat 会产生逐段块；整理成连续块，避免下游插入列时碎片化告警。
        return df.copy()
    if len(with_time) != len(frames):
        raise MergeError("横向合并的子结果 Time 列不一致（部分缺失）")
    indexed = []
    for f in frames:
        if f[_TIME].duplicated().any():
            raise MergeError("横向合并的子结果内部 Time 重复")
        indexed.append(f.set_index(_TIME))
    df = pd.concat(indexed, axis=1, join="outer", sort=False)
    # 各段 Time 索引通常不同，concat 会留下逐列块；设备数 >100 时
    # reset_index 插入 Time 列会触发 pandas PerformanceWarning（q12 宽通配场景）。
    # 先 copy 合并为连续块，既消除告警，也避免后续按列访问的低效。
    df = df.copy()
    df.index.name = _TIME
    df = df.reset_index()
    if df.columns.duplicated().any():
        raise MergeError("横向合并后出现重复列")
    if sort_by_time:
        df = _sort_time(df, descending)
    return df


def _aggregate_column(series, function: str):
    pd = _pd()
    fn = function.lower()
    numeric = pd.to_numeric(series, errors="coerce")
    if fn == "count" or fn == "sum":
        return numeric.sum(min_count=1)
    if fn == "min":
        return numeric.min(skipna=True)
    if fn == "max":
        return numeric.max(skipna=True)
    raise MergeError(f"不支持的标量聚合合并函数：{function}")


def _aggregate_vertical(frames: list, expected_columns: Optional[list] = None):
    """标量聚合的时间分段合并：逐列按函数合并为一行。"""
    pd = _pd()
    if not frames:
        raise MergeError("没有可聚合的子查询结果")
    if any(len(f) != 1 for f in frames):
        raise MergeError("标量聚合的子结果不是恰好 1 行")
    _ensure_same_columns(frames, "标量聚合")
    if expected_columns:
        for i, f in enumerate(frames):
            _check_expected_columns(f, expected_columns, f"第 {i} 段")
    columns = list(frames[0].columns)
    values: dict[str, Any] = {}
    for col in columns:
        m = _AGG_PREFIX_RE.match(str(col))
        if not m:
            raise MergeError(f"无法从列名识别聚合函数：{col}")
        function = m.group(1).lower()
        series = pd.concat([f[col] for f in frames], ignore_index=True, sort=False)
        values[col] = _aggregate_column(series, function)
    return pd.DataFrame([values], columns=columns)


def _hybrid(plan: dict, frames: list, spec: dict, aggregate: bool):
    groups = spec.get("device_groups")
    if not isinstance(groups, list) or not groups:
        raise MergeError("混合合并缺少 device_groups")
    tp = spec.get("time_parts")
    if not isinstance(tp, int) or tp < 1:
        tp = len(frames) // len(groups)
    if tp < 1 or len(frames) != tp * len(groups):
        raise MergeError(
            f"混合合并的分段数不匹配：{len(frames)} 段 ≠ {len(groups)} 组 × {tp} 时间片"
        )
    expected_groups = spec.get("expected_group_columns")
    if expected_groups is not None and len(expected_groups) != len(groups):
        raise MergeError("混合分区的列契约数与设备组数不一致")
    group_frames = []
    for i in range(len(groups)):
        chunk = frames[i * tp:(i + 1) * tp]
        expected = expected_groups[i] if expected_groups is not None else None
        group_frames.append(
            _aggregate_vertical(chunk, expected_columns=expected) if aggregate
            else _vertical(chunk, sort_by_time=False, descending=False,
                           expected_columns=expected))
        _check_group_columns(group_frames[-1], list(groups[i]), f"第 {i} 组")
    return _horizontal(group_frames,
                       sort_by_time=bool(spec.get("sort_by_time")),
                       descending=bool(spec.get("descending")),
                       device_groups=groups)


def _apply_output_columns(df, columns: Optional[list]):
    """把合并结果重排为计划声明的原生列序（列集合必须完全一致）。

    列序是结果契约的一部分：设备分区按 round-robin 执行后，列按设备组排列，
    与原生执行的“测点优先”列序不同；计划生成器给出 output_columns 时在此
    统一恢复。列集合不一致 → MergeError（执行器回退，不返回错列结果）。
    """
    if not columns:
        return df
    want = [str(c) for c in columns]
    got = _column_set(df)
    if got != set(want):
        missing = sorted(set(want) - got)[:3]
        extra = sorted(got - set(want))[:3]
        raise MergeError(
            f"合并结果列集合与 output_columns 契约不一致"
            f"（缺少 {missing}，多出 {extra}）"
        )
    if _TIME in df.columns:
        return df[[_TIME] + want]
    return df[want]


def merge_results(plan: dict, frames: list):
    """按计划声明的 merge 规格合并子查询结果。失败抛 MergeError。"""
    spec = _merge_spec(plan)
    df = _merge_results_inner(plan, frames, spec)
    return _apply_output_columns(df, spec.get("output_columns"))


def _merge_results_inner(plan: dict, frames: list, spec: dict):
    mode = spec["mode"]
    if not isinstance(frames, list):
        raise MergeError("子查询结果必须是列表")
    if mode == "none":
        if len(frames) != 1:
            raise MergeError(f"mode=none 需要恰好 1 个结果，实际 {len(frames)}")
        return frames[0]

    if not frames:
        raise MergeError("没有子查询结果")
    sort_by_time = bool(spec.get("sort_by_time"))
    descending = bool(spec.get("descending"))
    axis = spec.get("axis")

    if mode == "rows":
        if axis == "vertical":
            return _vertical(frames, sort_by_time, descending,
                             expected_columns=spec.get("expected_columns"))
        if axis == "horizontal":
            return _horizontal(frames, sort_by_time, descending,
                               device_groups=spec.get("device_groups"),
                               expected_group_columns=spec.get("expected_group_columns"))
        if axis == "hybrid":
            return _hybrid(plan, frames, spec, aggregate=False)
        raise MergeError(f"未知 rows 合并轴：{axis}")

    # aggregate
    kind = str(spec.get("kind") or "")
    if kind == "scalar_time":
        return _aggregate_vertical(frames, expected_columns=spec.get("expected_columns"))
    if kind == "scalar_device":
        return _horizontal(frames, sort_by_time=False, descending=False,
                           device_groups=spec.get("device_groups"),
                           expected_group_columns=spec.get("expected_group_columns"))
    if kind == "scalar_hybrid":
        return _hybrid(plan, frames, spec, aggregate=True)
    raise MergeError(f"未知聚合合并类型：{kind}")


def verify_merged(df, plan: dict, frames: list) -> None:
    """结构校验；不满足抛 MergeError（执行器回退）。"""
    pd = _pd()
    spec = _merge_spec(plan)
    mode = spec["mode"]
    if not isinstance(df, pd.DataFrame):
        raise MergeError("合并结果不是 DataFrame")

    expected_order = spec.get("output_columns")
    if expected_order:
        got = [str(c) for c in df.columns]
        want = ([_TIME] if _TIME in got else []) + [str(c) for c in expected_order]
        if got != want:
            raise MergeError("合并结果列序与 output_columns 契约不一致")

    if mode == "none":
        return

    if mode == "aggregate":
        if len(df) != 1:
            raise MergeError(f"标量聚合结果应为 1 行，实际 {len(df)} 行")
        expected = set()
        for f in frames:
            expected |= set(f.columns)
        if set(df.columns) != expected:
            raise MergeError("标量聚合结果的列集合与子结果并集不一致")
        if _TIME in df.columns:
            raise MergeError("标量聚合结果不应包含 Time 列")
        return

    axis = spec.get("axis")
    if axis in ("vertical", "hybrid"):
        # 混合模式在 _hybrid 内部按组校验；这里校验最终结果
        pass
    if axis in ("vertical",):
        _ensure_same_columns(frames, "纵向")
        expected = spec.get("expected_columns")
        if expected:
            for i, f in enumerate(frames):
                _check_expected_columns(f, expected, f"第 {i} 段")
    if axis in ("horizontal",):
        _ensure_disjoint_columns(frames)
        groups = spec.get("device_groups")
        if isinstance(groups, list) and len(groups) == len(frames):
            for i, (f, g) in enumerate(zip(frames, groups)):
                _check_group_columns(f, list(g), f"第 {i} 段")
        expected_groups = spec.get("expected_group_columns")
        if isinstance(expected_groups, list) and len(expected_groups) == len(frames):
            for i, (f, expected) in enumerate(zip(frames, expected_groups)):
                _check_expected_columns(f, expected, f"第 {i} 段")
    if axis in ("vertical", "horizontal", "hybrid"):
        if _TIME not in df.columns:
            raise MergeError("行结果缺少 Time 列")
        if df[_TIME].duplicated().any():
            raise MergeError("合并结果 Time 重复")
        if spec.get("sort_by_time") and not df.empty:
            values = df[_TIME].tolist()
            descending = bool(spec.get("descending"))
            ordered = all(values[i] >= values[i + 1] for i in range(len(values) - 1)) \
                if descending else all(values[i] <= values[i + 1] for i in range(len(values) - 1))
            if not ordered:
                raise MergeError("合并结果未按计划要求排序")
        if df.columns.duplicated().any():
            raise MergeError("合并结果存在重复列")
        return
    raise MergeError(f"无法校验的合并模式：{mode}/{axis}")


def canonicalize_for_compare(df):
    """测试/离线校验用：Time 置首、列名排序、按 Time 升序，消除列/行顺序差异。"""
    pd = _pd()
    if not isinstance(df, pd.DataFrame):
        return df
    cols = sorted(str(c) for c in df.columns if c != _TIME)
    out = df.rename(columns={c: str(c) for c in df.columns})
    if _TIME in out.columns:
        out = out[[_TIME] + cols]
        if not out.empty:
            out = out.sort_values(_TIME, kind="mergesort").reset_index(drop=True)
    else:
        out = out[cols]
    return out.reset_index(drop=True)
