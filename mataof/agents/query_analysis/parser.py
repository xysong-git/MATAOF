"""SQL 解析层：sqlglot 包装 + IoTDB 特殊语法预处理。

sqlglot（mysql 方言）无法直接解析的部分 IoTDB 语法在预处理阶段处理：
1. `GROUP BY ([start, end), interval[, sliding_step])` → 提取窗口信息后替换为占位符；
2. `ALIGN BY DEVICE` → 移除并记录标志；
3. FROM 路径中的通配符（`root.sg.*` / `root.**`）→ 先按原始文本提取设备路径，
   再把通配符替换为占位标识符供 sqlglot 解析其余部分。

本模块只负责"解析"，不做任何特征推断；解析失败时优雅降级（ast=None + parse_error），
由上层 Agent 将相应字段标记为 unknown。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import sqlglot
from sqlglot import exp

# sqlglot 对无法解析的语句（如 SHOW）会向 stderr 打印降级警告；
# 本流水线对这类输入有明确的降级处理（unknown 标记），故静默其日志。
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

from mataof.schemas import parse_interval_to_ms

# GROUP BY ([0, 4102444800000), 1h)                 → 定长窗口
# GROUP BY ([0, 4102444800000), 1h, 30m)            → 滑动窗口（30m 滑动步长）
# GROUP BY ([1640966400000, 1640966650000),20000ms) → 毫秒间隔
_GROUP_BY_WINDOW_RE = re.compile(
    r"\bGROUP\s+BY\s*\(\s*\[\s*(\d+)\s*,\s*(\d+)\s*\)\s*,\s*([A-Za-z0-9.]+)"
    r"(?:\s*,\s*([A-Za-z0-9.]+))?\s*\)",
    re.IGNORECASE,
)

_ALIGN_BY_DEVICE_RE = re.compile(r"\bALIGN\s+BY\s+DEVICE\b", re.IGNORECASE)

# 从 FROM 到后续子句关键字之间提取设备路径列表（不含子查询 FROM）
_FROM_PATHS_RE = re.compile(
    r"\bFROM\s+(?P<paths>[A-Za-z0-9_.*,\s]+?)(?=\s+(?:WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|OFFSET|ALIGN|UNION)\b|\s*$)",
    re.IGNORECASE,
)
_VALID_PATH_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_*]+)*$")
_WILDCARD_RE = re.compile(r"\*+")

# 替换通配符用的占位标识符（仅出现在预处理后的解析文本中，不进入输出）
_WILDCARD_PLACEHOLDER = "__wc__"


def _strip_comment_lines(sql: str) -> str:
    """去掉 `--` 行注释（仅用于正则预处理；原始文本保留给输出回显）。"""
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        lines.append(line)
    return "\n".join(lines)


def split_statements(sql: str) -> list[str]:
    """按分号拆分语句，返回去除空白后的非空语句列表。"""
    return [s.strip() for s in sql.split(";") if s.strip()]


@dataclass
class ParseContext:
    """一次解析的完整上下文，供各特征提取器使用。"""

    original_sql: str                       # 原始查询文本（回显用）
    statement: str                          # 实际参与分析的语句文本（多条语句时取第一条）
    ast: Optional[exp.Expression] = None    # sqlglot AST；解析失败为 None
    parse_error: Optional[str] = None       # 解析错误信息；成功为 None
    multi_statement: bool = False           # 输入含多条语句
    align_by_device: bool = False           # 是否含 ALIGN BY DEVICE
    group_by_window: Optional[dict] = None  # IoTDB 窗口 GROUP BY 提取结果
    from_paths: list[str] = field(default_factory=list)   # FROM 设备路径原文（含通配符）
    from_is_subquery: bool = False          # FROM 为子查询，无法提取设备路径

    @property
    def is_analyzed_query(self) -> bool:
        """是否为可分析的查询（SELECT / UNION）。SHOW 等命令返回 False。"""
        return self.ast is not None and self.ast.key in ("select", "union")


def parse_query(sql: str) -> ParseContext:
    """解析一条（或一组）查询。任何输入都不抛异常，失败信息记录在 parse_error。"""
    ctx = ParseContext(original_sql=sql, statement="")

    statements = split_statements(sql)
    if not statements:
        ctx.parse_error = "输入为空，无可用语句"
        return ctx

    ctx.multi_statement = len(statements) > 1
    ctx.statement = statements[0]

    # ---- 预处理（基于去注释文本）----
    text = _strip_comment_lines(ctx.statement)

    # 1) ALIGN BY DEVICE
    if _ALIGN_BY_DEVICE_RE.search(text):
        ctx.align_by_device = True
        text = _ALIGN_BY_DEVICE_RE.sub("", text)

    # 2) IoTDB 窗口 GROUP BY
    m = _GROUP_BY_WINDOW_RE.search(text)
    if m:
        ctx.group_by_window = {
            "start": int(m.group(1)),
            "end": int(m.group(2)),
            "interval": m.group(3),
            "interval_ms": parse_interval_to_ms(m.group(3)),
            "sliding_step": m.group(4),
            "sliding_step_ms": parse_interval_to_ms(m.group(4)),
        }
        text = _GROUP_BY_WINDOW_RE.sub("GROUP BY 1", text, count=1)

    # 3) FROM 设备路径（仅当 FROM 不是子查询开头）
    fm = _FROM_PATHS_RE.search(text)
    if fm:
        raw = fm.group("paths").strip()
        if raw.startswith("("):
            ctx.from_is_subquery = True
        else:
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            valid = all(_VALID_PATH_RE.match(p) for p in parts)
            if valid:
                ctx.from_paths = parts
                # 替换 FROM 子句内的通配符，使 sqlglot 可解析
                sanitized = _WILDCARD_RE.sub(_WILDCARD_PLACEHOLDER, raw)
                text = text.replace(raw, sanitized)

    # ---- 解析 ----
    try:
        ctx.ast = sqlglot.parse_one(text, read="mysql")
    except Exception as exc:  # ParseError 等一切解析问题都降级处理
        ctx.ast = None
        ctx.parse_error = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc).splitlines() else exc}"
    return ctx
