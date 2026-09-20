"""Query Analysis Agent 的 LLM 语义增强（混合增强模式）。

原则：确定性提取（sqlglot 规则）是权威，LLM 只做**附加语义标注**：
- semantic_summary   ：查询意图的自然语言摘要（人类可读/论文素材）；
- query_type_hint    ：确定性分类为 unknown 时，LLM 给出的类型提示（仅提示，
                      不改变 query_type，不得强行分类原则不变）；
- condition_hints    ：tag_or_attribute 歧义条件的判别建议（"tag"/"attribute"/
                      "unclear"，仅建议，不改变确定性类型标记）。

LLM 输出只进 "llm_analysis" 节，确定性字段一律不变；LLM 不可用/超时/输出非法 →
available=false + notes，绝不影响既有链路（确定性兜底）。

注意：启用 LLM 后，llm_analysis 节不再严格确定（模型输出有随机性）；
核心特征字段保持确定性。
"""

from __future__ import annotations

import json
import re
from typing import Optional

from mataof.llm import LLMClient

SYSTEM_PROMPT = (
    "你是一个时序数据库查询分析助手。你的任务是基于给定的 SQL 与结构化特征，"
    "为查询分析补充语义标注。\n"
    "严格约束：\n"
    "1. 只依据给定的 SQL 与特征作答，禁止编造任何统计信息、数据量、选择率或执行结果；\n"
    "2. 无法判断时必须回答 unknown（类型提示）或 unclear（判别建议），不得猜测为确定事实；\n"
    "3. 只输出一个 JSON 对象，不要输出任何其他文字或代码块标记。"
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_VALID_HINTS = ("tag", "attribute", "unclear")


def _features_summary(analysis: dict) -> dict:
    """从确定性分析结果压缩出给 LLM 的上下文（不引入任何新事实）。"""
    qf = analysis.get("query_features") or {}
    conditions = []
    for c in (qf.get("filter") or {}).get("conditions") or []:
        if c.get("type") == "tag_or_attribute" and c.get("column"):
            conditions.append({"expression": c.get("expression"), "column": c.get("column")})
    return {
        "query_type": analysis.get("query_type") or "unknown",
        "query_features": {
            "time": qf.get("time") or {},
            "device": qf.get("device") or {},
            "filter": {
                "filter_count": (qf.get("filter") or {}).get("filter_count"),
                "type_counts": (qf.get("filter") or {}).get("type_counts"),
            },
            "aggregation": {
                "has_aggregation": (qf.get("aggregation") or {}).get("has_aggregation"),
                "has_group_by": (qf.get("aggregation") or {}).get("has_group_by"),
                "has_window": (qf.get("aggregation") or {}).get("has_window"),
                "functions": [f.get("function") for f in
                              (qf.get("aggregation") or {}).get("aggregation_functions") or []],
            },
        },
        "unknown_features": analysis.get("unknown_features") or [],
        "ambiguous_conditions": conditions,
    }


def build_user_prompt(query: str, analysis: dict) -> str:
    return (
        "SQL：\n" + query + "\n\n"
        "结构化特征（确定性提取，权威）：\n"
        + json.dumps(_features_summary(analysis), ensure_ascii=False, indent=2) + "\n\n"
        "请输出 JSON：\n"
        '{"query_type_hint": "<六选一：point_query/range_query/predicate_filter_query/'
        'window_aggregation_query/complex_composite_query/unknown>（仅当确定性类型为 '
        'unknown 时给出提示，否则填 unknown）,\n'
        ' "semantic_summary": "<一句话描述查询意图，中文>",\n'
        ' "condition_hints": {"<条件表达式原文>": "tag|attribute|unclear", ...}}\n'
        "condition_hints 只覆盖 ambiguous_conditions 中列出的条件；"
        "无法判断填 unclear。"
    )


def parse_llm_response(text: str, notes: list[str]) -> Optional[dict]:
    """从 LLM 输出解析并校验 JSON；非法 → None + 说明。"""
    m = _JSON_RE.search(text or "")
    if not m:
        notes.append("LLM 输出不是合法 JSON，忽略本次增强")
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        notes.append("LLM 输出 JSON 解析失败，忽略本次增强")
        return None
    if not isinstance(data, dict):
        notes.append("LLM 输出不是 JSON 对象，忽略本次增强")
        return None
    return data


def build_llm_analysis(query: str, analysis: dict,
                       llm: Optional[LLMClient]) -> dict:
    """生成 llm_analysis 节。llm 为 None 或调用失败 → available=false（兜底）。"""
    section = {
        "available": False,
        "semantic_summary": None,
        "query_type_hint": None,
        "condition_hints": {},
        "notes": [],
    }
    if llm is None:
        section["notes"].append("LLM 未启用（provider=none 或不可用）")
        return section

    try:
        text = llm.chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(query, analysis)},
        ])
    except Exception as exc:  # 自定义客户端可能抛异常——一律降级
        section["notes"].append(
            f"LLM 调用异常（{type(exc).__name__}），回退确定性路径"
        )
        return section
    if text is None:
        section["notes"].append("LLM 调用失败（超时/网络/服务不可用），回退确定性路径")
        return section

    data = parse_llm_response(text, section["notes"])
    if data is None:
        return section

    section["available"] = True
    hint = data.get("query_type_hint")
    if isinstance(hint, str) and hint.strip() in (
        "point_query", "range_query", "predicate_filter_query",
        "window_aggregation_query", "complex_composite_query", "unknown",
    ):
        section["query_type_hint"] = hint.strip()
    else:
        section["notes"].append("LLM query_type_hint 非法或缺失，置为 null")

    summary = data.get("semantic_summary")
    section["semantic_summary"] = summary.strip() if isinstance(summary, str) and summary.strip() else None
    if section["semantic_summary"] is None:
        section["notes"].append("LLM 未给出语义摘要，置为 null")

    hints = data.get("condition_hints")
    if isinstance(hints, dict):
        ambiguous = [
            c for c in (analysis.get("query_features") or {}).get("filter", {}).get("conditions") or []
            if c.get("type") == "tag_or_attribute"
        ]
        for cond in ambiguous:
            expr = cond.get("expression")
            v = hints.get(expr)
            if v in _VALID_HINTS:
                section["condition_hints"][expr] = v
            else:
                section["condition_hints"][expr] = "unclear"
    else:
        section["notes"].append("LLM 未给出 condition_hints，全部标记 unclear")
    return section
