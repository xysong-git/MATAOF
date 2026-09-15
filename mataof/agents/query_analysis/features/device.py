"""设备维度特征提取（任务 3）。

提取规则：
- 设备条件来自 FROM 子句的设备路径（IoTDB 的设备过滤通过 FROM 路径表达）；
- 路径含通配符（* / **）时，匹配的设备数量无法从 SQL 确定 → device_count 为 null；
- `root` / `root.*` / `root.**` 等全库级路径不构成"具体设备过滤"；
- FROM 为子查询时，设备范围由子查询决定，主查询不可确定 → 全部 unknown；
- 绝不依据 SQL 之外的信息猜测设备数量。
"""

from __future__ import annotations

from sqlglot import exp

from mataof.agents.query_analysis.parser import ParseContext
from mataof.agents.query_analysis.features.base import full_ref, select_blocks


def _is_full_wildcard(path: str) -> bool:
    """全库级通配路径（root / root.* / root.**），不构成具体设备过滤。"""
    p = path.strip().lower()
    if p == "root":
        return True
    if p in ("root.*", "root.**"):
        return True
    # root.*** 之类多层通配同样视为全库级
    if p.startswith("root.") and set(p[5:]) <= {"*", "."}:
        return True
    return False


def _has_wildcard(path: str) -> bool:
    return "*" in path


def extract_device_features(ctx: ParseContext, notes: list[str]) -> dict:
    """返回 query_features.device 节。"""
    section = {
        "has_device_filter": False,
        "device_count": None,
        "multi_device": None,
        "device_paths": [],
        "device_condition_count": 0,
    }

    if not ctx.is_analyzed_query:
        return section

    # 优先使用预处理层从原始 SQL 提取的路径（保留通配符原文）
    paths: list[str] = []
    if ctx.from_paths:
        paths = ctx.from_paths
    elif ctx.from_is_subquery:
        notes.append("设备范围：unknown（FROM 为子查询，设备范围由子查询决定，主查询无法确定）")
        return section
    else:
        # 回退：从 AST 的 FROM 表引用重建路径（通配符已被占位符替换的场景）
        for block in select_blocks(ctx.ast):
            from_ = block.args.get("from_")
            if from_ is None:
                continue
            for t in from_.find_all(exp.Table):
                ref = full_ref(t)
                if "__wc__" in ref:
                    # 占位符场景理论上已被 from_paths 覆盖；此处保守标记 unknown
                    notes.append("设备范围：unknown（FROM 路径含通配符，无法确定匹配设备数）")
                    section["has_device_filter"] = True
                    return section
                paths.append(ref)
        paths = list(dict.fromkeys(paths))  # 去重保序

    if not paths:
        # 无 FROM（如 SELECT 常量）→ 不存在设备过滤
        return section

    section["device_paths"] = paths
    section["device_condition_count"] = len(paths)

    concrete = [p for p in paths if not _has_wildcard(p)]
    wildcard_paths = [p for p in paths if _has_wildcard(p)]

    # 全部为全库级通配 → 不构成具体设备过滤
    if all(_is_full_wildcard(p) for p in paths):
        section["has_device_filter"] = False
        section["device_count"] = None
        section["multi_device"] = None
        notes.append("设备数量：unknown（FROM 为全库级路径，SQL 未限定具体设备，无法确定设备数）")
        return section

    section["has_device_filter"] = True
    if wildcard_paths:
        section["device_count"] = None
        section["multi_device"] = None
        notes.append("设备数量：unknown（FROM 路径含通配符，匹配的设备数量无法从 SQL 确定）")
        return section

    section["device_count"] = len(concrete)
    section["multi_device"] = section["device_count"] > 1
    return section
