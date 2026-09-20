"""Knowledge Memory Agent 的 LLM 语义增强（混合增强模式）。

原则：历史记录是事实（append-only），相似度检索是权威；LLM 只提供解读与总结：
1. retrieve 增强：`semantic_summary` —— 对匹配结果/成功模式/失败经验的自然语言
   解读（llm_generated）。**禁止提出任何策略建议或推荐**（KM 不决策原则不变，
   prompt 与测试双重约束）；
2. update 增强：`experience_note` —— 对本次执行经验的自然语言总结，随记录存入
   知识库的 `llm_note` 字段（LLM 生成元数据，与事实字段隔离）。

LLM 不可用/超时/输出非法 → available=false + notes，检索结果与入库记录完全不变
（确定性兜底）。
"""

from __future__ import annotations

import json
import re
from typing import Optional

from mataof.llm import LLMClient

SYSTEM_PROMPT = (
    "你是一个时序数据库查询优化系统的知识记忆解读助手。你只解读历史知识，不做任何决策。\n"
    "严格约束：\n"
    "1. 只依据给定的结构化信息作答，禁止编造任何统计信息、数据量或执行结果；\n"
    "2. 禁止提出任何策略建议、推荐或「下一步应该使用什么策略」之类的表述"
    "（策略决策不属于你的职责）；\n"
    "3. 不得把单次执行结果表述为策略的永久规律；\n"
    "4. 只输出一个 JSON 对象，不要输出任何其他文字或代码块标记。"
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _retrieve_context(retrieve_output: dict) -> dict:
    """压缩检索输出为 LLM 上下文（不引入任何新事实）。"""
    return {
        "matched_records": [
            {"record_id": m.get("record_id"), "similarity": m.get("similarity"),
             "match_level": m.get("match_level"), "strategy_id": m.get("strategy_id"),
             "execution_result": m.get("execution_result"),
             "performance": m.get("performance")}
            for m in (retrieve_output.get("matched_records") or [])[:15]
        ],
        "match_counts": retrieve_output.get("match_counts") or {},
        "knowledge_confidence": retrieve_output.get("knowledge_confidence"),
        "historical_summary": {
            "successful_strategies": retrieve_output.get("historical_summary", {})
                .get("successful_strategies") or [],
            "failed_strategies": retrieve_output.get("historical_summary", {})
                .get("failed_strategies") or [],
        },
    }


def build_retrieve_prompt(retrieve_output: dict) -> str:
    return (
        "历史知识检索结果：\n"
        + json.dumps(_retrieve_context(retrieve_output), ensure_ascii=False) + "\n\n"
        "请输出 JSON：\n"
        '{"semantic_summary": "<150字以内，中文，解读：匹配到了什么历史知识、'
        '哪些策略在相似上下文中有成功/失败经验；只陈述事实，不提建议>"}'
    )


def _update_context(feedback_input: dict, update_output: dict) -> dict:
    record = feedback_input.get("query_analysis") or {}
    qf = record.get("query_features") or {}
    return {
        "query_type": record.get("query_type") or "unknown",
        "query_features": {
            "time": qf.get("time") or {},
            "filter": {"type_counts": (qf.get("filter") or {}).get("type_counts")},
            "aggregation": {
                "has_aggregation": (qf.get("aggregation") or {}).get("has_aggregation"),
                "has_window": (qf.get("aggregation") or {}).get("has_window"),
            },
        },
        "strategy_id": (feedback_input.get("decision") or {}).get("selected_strategy", {})
            .get("strategy_id") if isinstance(feedback_input.get("decision"), dict) else None,
        "execution": {
            "status": (feedback_input.get("monitoring") or {}).get("execution_status")
                      if isinstance(feedback_input.get("monitoring"), dict) else None,
            "performance_assessment": (feedback_input.get("monitoring") or {})
                .get("performance_assessment") if isinstance(feedback_input.get("monitoring"), dict) else None,
            "metrics": (feedback_input.get("monitoring") or {}).get("metrics")
                       if isinstance(feedback_input.get("monitoring"), dict) else None,
        },
        "update": {
            "stored": update_output.get("stored"),
            "strategy_effect": update_output.get("strategy_effect"),
        },
    }


def build_update_prompt(feedback_input: dict, update_output: dict) -> str:
    return (
        "本次执行与入库情况：\n"
        + json.dumps(_update_context(feedback_input, update_output), ensure_ascii=False) + "\n\n"
        "请输出 JSON：\n"
        '{"experience_note": "<80字以内，中文，总结本次执行经验：什么策略在什么'
        '条件下表现如何；只陈述事实，单次观察不代表规律，不提建议>"}'
    )


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


def _chat(llm: LLMClient, messages: list, notes: list) -> Optional[str]:
    try:
        return llm.chat(messages)
    except Exception as exc:
        notes.append(f"LLM 调用异常（{type(exc).__name__}），回退确定性路径")
        return None


def build_retrieve_llm_analysis(llm: Optional[LLMClient], retrieve_output: dict,
                                query_analysis: dict) -> dict:
    """retrieve 输出的 llm_analysis 节。"""
    section = {"available": False, "semantic_summary": None, "notes": []}
    if llm is None:
        section["notes"].append("LLM 未启用（provider=none 或不可用）")
        return section
    text = _chat(llm, [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_retrieve_prompt(retrieve_output)},
    ], section["notes"])
    if text is None:
        if not section["notes"]:
            section["notes"].append("LLM 调用失败，回退确定性路径")
        return section
    data = parse_llm_json(text, section["notes"])
    if data is None:
        return section
    summary = data.get("semantic_summary")
    if isinstance(summary, str) and summary.strip():
        section["available"] = True
        section["semantic_summary"] = summary.strip()
    else:
        section["notes"].append("LLM 未给出语义解读，置为 null")
    return section


def request_experience_note(llm: Optional[LLMClient], feedback_input: dict,
                            update_output: dict, notes: list) -> Optional[str]:
    """请求本次执行经验总结。返回 None 表示不可用（不写入记录）。"""
    if llm is None:
        notes.append("LLM 未启用（provider=none 或不可用）")
        return None
    text = _chat(llm, [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_update_prompt(feedback_input, update_output)},
    ], notes)
    if text is None:
        if not notes:
            notes.append("LLM 调用失败，不生成经验总结")
        return None
    data = parse_llm_json(text, notes)
    if data is None:
        return None
    note = data.get("experience_note")
    return note.strip() if isinstance(note, str) and note.strip() else None


def build_update_llm_analysis(experience_note: Optional[str], notes: list) -> dict:
    """update 输出的 llm_analysis 节。"""
    return {
        "available": experience_note is not None,
        "experience_note": experience_note,
        "notes": list(notes),
    }
