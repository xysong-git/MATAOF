"""计划编译所需的 SQL 结构识别与安全改写。

这里不做完整 SQL 解析（IoTDB 方言由 query_analysis 负责），只做**计划编译器
必须精确控制**的几类文本操作：

- FROM 设备路径列表的提取/替换（设备分区）；
- WHERE 中闭合时间范围 `time >= A AND time <= B` 的识别/替换（时间分区）；
- 窗口 GROUP BY `([A, B), interval)` 的识别/替换（窗口聚合时间分区）；
- LIMIT/OFFSET/HAVING/UNION/DISTINCT/LAST/ALIGN BY DEVICE 等会破坏分区
  等价性的结构识别——识别到就拒绝生成对应计划（安全优先）。

所有函数在无法**确定**改写安全时返回 None，由生成器排除该计划；绝不猜测。
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Optional

_WS_RE = re.compile(r"\s+")

# FROM 到后续子句关键字之间的路径列表（与 query_analysis/parser.py 同口径）
_FROM_RE = re.compile(
    r"\bFROM\s+(?P<paths>[A-Za-z0-9_.*,\s]+?)"
    r"(?=\s+(?:WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|OFFSET|ALIGN|UNION)\b|\s*$)",
    re.IGNORECASE,
)

# 规范顺序的闭合时间范围：time >= A AND time <= B（允许 < / > 变体）
_TIME_RANGE_RE = re.compile(
    r"(?P<col>\btime\b|\btimestamp\b)\s*(?P<op1>>=|>)\s*(?P<start>-?\d+)\s+AND\s+"
    r"(?P<col2>\btime\b|\btimestamp\b)\s*(?P<op2><=|<)\s*(?P<end>-?\d+)",
    re.IGNORECASE,
)

_WINDOW_RE = re.compile(
    r"GROUP\s+BY\s*\(\s*\[\s*(?P<start>\d+)\s*,\s*(?P<end>\d+)\s*\)\s*,\s*"
    r"(?P<interval>[A-Za-z0-9.]+)"
    r"(?:\s*,\s*(?P<slide>[A-Za-z0-9.]+))?\s*\)",
    re.IGNORECASE,
)

_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\s+(?P<body>.+?)(?=\s+LIMIT\b|\s+OFFSET\b|\s*$)",
                          re.IGNORECASE | re.DOTALL)
_WHERE_RE = re.compile(r"\bWHERE\b(?P<body>.*?)(?=\s+GROUP\s+BY\b|\s+HAVING\b|\s+ORDER\s+BY\b|\s+LIMIT\b|\s+OFFSET\b|\s+ALIGN\b|\s*$)",
                       re.IGNORECASE | re.DOTALL)

_FULL_PATH_IN_WHERE_RE = re.compile(
    r"\broot\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+",
    re.IGNORECASE,
)


def normalize_query(query: str) -> str:
    """去掉首尾空白和末尾分号；保留内部原貌（改写可追踪）。"""
    return (query or "").strip().rstrip(";").strip()


def from_clause(query: str) -> Optional[dict]:
    """返回 {'start','end','paths','raw'} 或 None（无 FROM/子查询）。"""
    m = _FROM_RE.search(query)
    if not m:
        return None
    raw = m.group("paths").strip()
    if not raw or raw.startswith("("):
        return None
    paths = [p.strip() for p in raw.split(",") if p.strip()]
    if not paths or not all(re.match(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_*]+)*$", p)
                            for p in paths):
        return None
    return {
        "start": m.start("paths"),
        "end": m.end("paths"),
        "paths": paths,
        "raw": raw,
    }


def replace_from_paths(query: str, paths: list[str]) -> Optional[str]:
    """把 FROM 路径列表替换为给定路径（设备分区/通配符展开）。"""
    clause = from_clause(query)
    if clause is None or not paths:
        return None
    return query[:clause["start"]] + ", ".join(paths) + query[clause["end"]:]


def where_body(query: str) -> Optional[str]:
    m = _WHERE_RE.search(query)
    return m.group("body") if m else None


def has_full_path_filter(query: str) -> bool:
    """WHERE 中是否出现完整测点路径（四段 root.db.group.device.series）。

    IoTDB 对“带完整路径值的谓词 + ALIGN BY DEVICE”拒绝（701）；这类查询
    不生成该改写/分区计划（安全门禁）。
    """
    body = where_body(query)
    return bool(body and _FULL_PATH_IN_WHERE_RE.search(body))


def has_limit(query: str) -> bool:
    return bool(re.search(r"\bLIMIT\b", query, re.IGNORECASE))


def has_offset(query: str) -> bool:
    return bool(re.search(r"\bOFFSET\b", query, re.IGNORECASE))


def has_having(query: str) -> bool:
    return bool(re.search(r"\bHAVING\b", query, re.IGNORECASE))


def has_union(query: str) -> bool:
    return bool(re.search(r"\bUNION\b", query, re.IGNORECASE))


def has_distinct(query: str) -> bool:
    return bool(re.search(r"\bSELECT\s+DISTINCT\b", query, re.IGNORECASE))


def has_last(query: str) -> bool:
    return bool(re.search(r"\bSELECT\s+LAST\b", query, re.IGNORECASE))


def has_align_by_device(query: str) -> bool:
    return bool(re.search(r"\bALIGN\s+BY\s+DEVICE\b", query, re.IGNORECASE))


def has_order_by(query: str) -> bool:
    return bool(_ORDER_BY_RE.search(query))


def order_by_direction(query: str) -> Optional[str]:
    """返回 'asc'/'desc'；无 ORDER BY 返回 'none'；按非时间列排序返回 None。"""
    m = _ORDER_BY_RE.search(query)
    if not m:
        return "none"
    body = m.group("body").strip()
    if not re.search(r"\btime\b|\btimestamp\b", body, re.IGNORECASE):
        return None   # 非时间排序：分区合并无法保证顺序 → 不生成分区计划
    return "desc" if re.search(r"\bDESC\b", body, re.IGNORECASE) else "asc"


def closed_time_range(query: str) -> Optional[tuple[int, int]]:
    """识别规范顺序的闭合时间范围；无法确定/多处出现 → None。"""
    matches = list(_TIME_RANGE_RE.finditer(query))
    if len(matches) != 1:
        return None
    m = matches[0]
    if m.group("op1") != ">=" or m.group("op2") != "<=":
        return None
    try:
        return int(m.group("start")), int(m.group("end"))
    except ValueError:
        return None


def replace_closed_time_range(query: str, start: int, end: int,
                              upper_inclusive: bool = True) -> Optional[str]:
    """把规范的闭合时间范围替换为 [start, end] 或 [start, end)。

    只匹配唯一的 `time >= A AND time <= B`；其他形式返回 None（不猜）。
    """
    matches = list(_TIME_RANGE_RE.finditer(query))
    if len(matches) != 1:
        return None
    m = matches[0]
    if m.group("op1") != ">=" or m.group("op2") != "<=":
        return None
    col1, col2 = m.group("col"), m.group("col2")
    upper_op = "<=" if upper_inclusive else "<"
    repl = f"{col1} >= {int(start)} AND {col2} {upper_op} {int(end)}"
    return query[:m.start()] + repl + query[m.end():]


def window_info(query: str) -> Optional[dict]:
    """识别 IoTDB 窗口 GROUP BY；滑动窗口/多处窗口 → None。"""
    matches = list(_WINDOW_RE.finditer(query))
    if len(matches) != 1:
        return None
    m = matches[0]
    if m.group("slide"):
        return None   # 滑动窗口会重叠，分区合并不等价
    try:
        return {
            "start": int(m.group("start")),
            "end": int(m.group("end")),
            "interval": m.group("interval"),
        }
    except ValueError:
        return None


def replace_window_range(query: str, start: int, end: int) -> Optional[str]:
    """替换窗口 GROUP BY 的 [start, end)；滑动窗口/多处 → None。"""
    matches = list(_WINDOW_RE.finditer(query))
    if len(matches) != 1 or matches[0].group("slide"):
        return None
    m = matches[0]
    repl = (f"GROUP BY ([{int(start)}, {int(end)}), "
            f"{m.group('interval')})")
    return query[:m.start()] + repl + query[m.end():]


def has_window_group_by(query: str) -> bool:
    return window_info(query) is not None or bool(_WINDOW_RE.search(query))


def expand_device_paths(paths: list[str], inventory: Any) -> Optional[list[str]]:
    """展开 FROM 路径中的通配符，返回完整设备路径列表；无法确定 → None。

    inventory 支持：
    - list[str]：完整设备路径（如 root.dbl.g_0.d_1）；
    - dict[str, list[str]]：通配路径 → 设备列表；
    - None/缺失：含通配符 → None（不猜测设备数）。
    """
    if not paths:
        return None
    if isinstance(inventory, dict):
        out: list[str] = []
        for p in paths:
            if "*" not in p:
                out.append(p)
                continue
            vals = inventory.get(p)
            if not isinstance(vals, list) or not vals:
                return None
            out.extend(str(v) for v in vals)
        return list(dict.fromkeys(out))

    inv = inventory if isinstance(inventory, list) else []
    inv = [str(v) for v in inv if isinstance(v, str) and v]
    out = []
    for p in paths:
        if "*" not in p:
            out.append(p)
            continue
        # 把 IoTDB 通配符转成 fnmatch 模式：** → *，单 * → 单段
        pattern = p.replace("**", "*")
        matched = [v for v in inv if fnmatch.fnmatchcase(v, pattern)]
        if not matched:
            return None
        out.extend(matched)
    return list(dict.fromkeys(out))


def split_time(start: int, end: int, parts: int,
               window_interval_ms: Optional[int] = None) -> Optional[list[tuple[int, int]]]:
    """把 [start, end] 切成 parts 个连续区间；无法安全切分 → None。

    - 窗口聚合按 interval 对齐边界（避免把一个窗口切成两半）；
    - 原始扫描允许任意边界；最后一个区间闭合，其余半开。
    """
    if parts < 2 or not isinstance(start, int) or not isinstance(end, int) or end <= start:
        return None
    span = end - start
    if span < parts:
        return None
    if window_interval_ms and window_interval_ms > 0:
        raw_bounds = []
        for i in range(parts):
            b = start + round(i * span / parts / window_interval_ms) * window_interval_ms
            raw_bounds.append(b)
        bounds = [start]
        for b in raw_bounds[1:]:
            if b > bounds[-1]:
                bounds.append(b)
        if end > bounds[-1]:
            bounds.append(end)
        if len(bounds) < 2:
            return None
        return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    bounds = [start + (span * i) // parts for i in range(parts)] + [end]
    bounds = sorted(set(bounds))
    if len(bounds) < 2:
        return None
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def split_devices(devices: list[str], parts: int) -> Optional[list[list[str]]]:
    """按 round-robin 把设备分到 parts 组（基准实测优于连续分组）。"""
    if parts < 2 or len(devices) < 2:
        return None
    parts = min(parts, len(devices))
    groups = [list(devices[i::parts]) for i in range(parts)]
    groups = [g for g in groups if g]
    return groups if len(groups) >= 2 else None


def to_one_line(sql: str) -> str:
    """清理多余空白（仅用于描述/日志，不改变 SQL 语义）。"""
    return _WS_RE.sub(" ", (sql or "").strip())


# ---------------------------------------------------------------------------
# 结果列契约（用于合并后的结构校验）
# ---------------------------------------------------------------------------

_SELECT_RE = re.compile(r"\bSELECT\s+(?P<body>.+?)\s+FROM\b", re.IGNORECASE | re.DOTALL)
_FROM_EXPR_RE = re.compile(r"^(?P<fn>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<arg>.*)\)$", re.DOTALL)
_RAW_EXPR_RE = re.compile(r"^([A-Za-z0-9_]+)$")


def _split_top_level(text: str) -> Optional[list[str]]:
    """按顶层逗号切分 SELECT 列表（忽略括号内逗号）；不配对 → None。"""
    out, buf, depth = [], [], 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
        if ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if depth != 0:
        return None
    out.append("".join(buf).strip())
    return [x for x in out if x]


def select_items(query: str) -> Optional[list[tuple]]:
    """解析 SELECT 列表为 [("raw", name) | ("func", fn, arg)]。

    无法确定（SELECT *、别名、LAST、嵌套表达式、通配测点）→ None；
    调用方据此关闭严格列校验（但仍保留非空/列集合一致性校验）。
    """
    m = _SELECT_RE.search(query)
    if not m:
        return None
    body = m.group("body").strip()
    if re.search(r"\bDISTINCT\b", body, re.IGNORECASE):
        return None
    parts = _split_top_level(body)
    if not parts:
        return None
    items: list[tuple] = []
    for item in parts:
        if re.search(r"\bAS\b", item, re.IGNORECASE):
            return None
        if re.match(r"^LAST\b", item, re.IGNORECASE):
            return None
        fm = _FROM_EXPR_RE.match(item)
        if fm:
            fn, arg = fm.group("fn").lower(), fm.group("arg").strip()
            if "*" in arg or "." in arg or "(" in arg:
                return None
            if not _RAW_EXPR_RE.match(arg):
                return None
            items.append(("func", fn, arg))
            continue
        if "*" in item or not _RAW_EXPR_RE.match(item):
            return None
        items.append(("raw", item))
    return items


def expected_columns(query: str, devices: list[str]) -> Optional[list[str]]:
    """给定设备清单，推导结果列名（Time 除外，按引擎原生列序）；无法确定 → None。

    列序与 IoTDB 默认对齐一致：**测点优先**——先列出第一个 SELECT 测点在所有
    设备上的列，再列第二个测点，依此类推（实测：
    `SELECT s_1666, s_1716 FROM d_81, d_82` → d_81.s_1666, d_82.s_1666,
    d_81.s_1716, d_82.s_1716）。该顺序用于合并后的列重排，保证结果列序
    与原生执行一致。

    仅覆盖可确定的简单 SELECT（测点名/单层函数）；别名、LAST、SELECT *、
    测点通配等一律返回 None，由合并层退化为结构校验。
    """
    items = select_items(query)
    if items is None or not devices:
        return None
    cols: list[str] = []
    for item in items:
        for device in devices:
            if item[0] == "raw":
                cols.append(f"{device}.{item[1]}")
            else:
                _, fn, arg = item
                cols.append(f"{fn}({device}.{arg})")
    return list(dict.fromkeys(cols))
