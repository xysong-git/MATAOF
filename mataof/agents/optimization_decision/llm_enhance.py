"""Optimization Decision Agent 的 LLM 语义增强（混合增强模式）。

原则：确定性评分与风险门控是权威；LLM 只提供：
1. preferences      ：各维度候选偏好排序（仅在候选集合内，严格校验）；
                      默认仅作展示（llm_analysis 节）；开启 preference_bonus 时，
                      第一偏好获得 +0.05 加分——加分不参与风险门控（LLM 不能解锁
                      高风险策略），不能改变"证据不足回退 baseline"的安全路径。
2. semantic_reason  ：对最终决策的自然语言解读（基于结构化证据，标注 llm_generated）。

LLM 不可用/超时/输出非法 → available=false + notes，确定性决策完全不变（兜底）。
"""

from __future__ import annotations

import json
import re
from typing import Optional

from mataof.llm import LLMClient

SYSTEM_PROMPT = (
    "你是一个时序数据库查询优化决策解读助手。你只解读决策，不做决策。\n"
    "严格约束：\n"
    "1. 只依据给定的查询特征、数据库/系统状态、历史证据与决策结果作答，"
    "禁止编造任何统计信息、数据量或执行结果；\n"
    "2. 候选偏好只允许从给定的候选策略列表中选择，不得提出列表之外的策略；\n"
    "3. 不得声称任何策略是全局最优，只能表述为当前信息条件下的选择；\n"
    "4. 只输出一个 JSON 对象，不要输出任何其他文字或代码块标记。"
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
DIMENSIONS = ("time_pruning", "filter_order", "aggregation_placement")


def _context_summary(decision_input: dict) -> dict:
    """从决策输入压缩上下文（不引入任何新事实）。"""
    analysis = decision_input.get("query_analysis") or {}
    qf = analysis.get("query_features") or {}
    return {
        "query_type": analysis.get("query_type") or "unknown",
        "query_features": {
            "time": qf.get("time") or {},
            "device": qf.get("device") or {},
            "filter": {"type_counts": (qf.get("filter") or {}).get("type_counts")},
            "aggregation": {
                "has_aggregation": (qf.get("aggregation") or {}).get("has_aggregation"),
                "has_group_by": (qf.get("aggregation") or {}).get("has_group_by"),
                "has_window": (qf.get("aggregation") or {}).get("has_window"),
            },
            "scan": {"scan_level": (qf.get("scan") or {}).get("scan_level")},
        },
        "database_state": decision_input.get("database_state") or {},
        "system_state": decision_input.get("system_state") or {},
        "historical_records": [
            {"record_id": h.get("record_id"),
             "strategy": h.get("strategy"),
             "execution_feedback": h.get("execution_feedback")}
            for h in (decision_input.get("historical_records") or [])[:10]
        ],
        "candidate_strategies": decision_input.get("candidate_strategies"),
    }


def build_preferences_prompt(decision_input: dict) -> str:
    return (
        "决策上下文：\n" + json.dumps(_context_summary(decision_input), ensure_ascii=False) + "\n\n"
        "请输出 JSON：\n"
        '{"preferences": {"time_pruning": [按你的偏好排序的候选策略名，可空数组], '
        '"filter_order": [...], "aggregation_placement": [...]}}\n'
        "只使用候选列表中出现的策略名；不适用维度给空数组。"
    )


def build_reason_prompt(decision_input: dict, decision_output: dict) -> str:
    selected = decision_output.get("decision") or {}
    return (
        "决策上下文：\n" + json.dumps(_context_summary(decision_input), ensure_ascii=False) + "\n\n"
        "最终决策：\n" + json.dumps({
            "decision": {d: {"strategy": selected.get(d, {}).get("strategy"),
                             "reason": selected.get(d, {}).get("reason"),
                             "confidence": selected.get(d, {}).get("confidence")}
                         for d in DIMENSIONS},
            "selected_strategy": decision_output.get("selected_strategy"),
            "overall_confidence": decision_output.get("overall_confidence"),
            "decision_status": decision_output.get("decision_status"),
        }, ensure_ascii=False) + "\n\n"
        "请输出 JSON：\n"
        '{"semantic_reason": "<150字以内，中文，解读为何做出该决策、依据了什么证据、'
        '明确表述为当前信息条件下的选择>",\n'
        ' "preferences": {"time_pruning": [...], "filter_order": [...], '
        '"aggregation_placement": [...]}}\n'
        "preferences 只使用候选列表中的策略名；不适用维度给空数组。"
    )


def validate_preferences(raw: object, candidates: dict, notes: list) -> dict:
    """校验 LLM 偏好：维度正确、策略名 ∈（候选 ∩ 目录）。非法条目丢弃。"""
    out: dict = {}
    if not isinstance(raw, dict):
        notes.append("LLM preferences 不是对象，忽略")
        return out
    for dim in DIMENSIONS:
        allowed = {c["name"] if isinstance(c, dict) else c
                   for c in (candidates.get(dim) or [])}
        values = raw.get(dim)
        if not isinstance(values, list):
            continue
        valid = []
        for name in values:
            if name in allowed:
                valid.append(name)
            else:
                notes.append(f"LLM 偏好中忽略非法策略名（维度 {dim}）：{name}")
        if valid:
            out[dim] = valid
    return out


def parse_llm_json(text: str, notes: list) -> Optional[dict]:
    m = _JSON_RE.search(text or "")
    if not m:
        notes.append("LLM 输出不是合法 JSON，忽略本次增强")
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        notes.append("LLM 输出 JSON 解析失败，忽略本次增强")
        return None
    return data if isinstance(data, dict) else None


def request_preferences(llm: LLMClient, decision_input: dict, candidates: dict,
                        notes: list) -> dict:
    """评分前请求候选偏好（仅 preference_bonus 开启时调用）。失败 → {}。"""
    try:
        text = llm.chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_preferences_prompt(decision_input)},
        ])
    except Exception as exc:
        notes.append(f"LLM 偏好请求异常（{type(exc).__name__}），不使用偏好加分")
        return {}
    if text is None:
        notes.append("LLM 偏好请求失败，不使用偏好加分")
        return {}
    data = parse_llm_json(text, notes)
    if data is None:
        return {}
    return validate_preferences(data.get("preferences"), candidates, notes)


def build_llm_analysis(llm: Optional[LLMClient], decision_input: dict,
                       decision_output: dict, candidates: dict) -> dict:
    """生成决策输出的 llm_analysis 节。llm=None/失败 → available=false（兜底）。"""
    section = {
        "available": False,
        "semantic_reason": None,
        "preferences": {},
        "notes": [],
    }
    if llm is None:
        section["notes"].append("LLM 未启用（provider=none 或不可用）")
        return section
    try:
        text = llm.chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_reason_prompt(decision_input, decision_output)},
        ])
    except Exception as exc:
        section["notes"].append(f"LLM 调用异常（{type(exc).__name__}），回退确定性路径")
        return section
    if text is None:
        section["notes"].append("LLM 调用失败（超时/网络/服务不可用），回退确定性路径")
        return section
    data = parse_llm_json(text, section["notes"])
    if data is None:
        return section

    section["available"] = True
    reason = data.get("semantic_reason")
    if isinstance(reason, str) and reason.strip():
        section["semantic_reason"] = reason.strip()
    else:
        section["notes"].append("LLM 未给出决策解读，置为 null")
    section["preferences"] = validate_preferences(data.get("preferences"), candidates,
                                                  section["notes"])
    return section
