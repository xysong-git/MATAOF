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
DIMENSIONS = ("alignment", "fetch_size")


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


# ---------------------------------------------------------------------------
# LLM 参与决策（decision_mode="llm_primary"）：LLM 选择策略，确定性为兜底
# ---------------------------------------------------------------------------

DECISION_SYSTEM_PROMPT = (
    "你是时序数据库查询优化策略选择器。你依据查询特征、数据库/系统状态与历史"
    "执行经验，从给定候选策略中为每个适用维度选择策略。\n"
    "严格约束：\n"
    "1. 只能从候选列表中选，绝不提出候选之外的策略；不适用维度输出 null；\n"
    "2. 只依据给定的事实与历史证据，禁止编造任何统计、数据量或执行结果；\n"
    "3. 不得声称全局最优，只表述为当前信息条件下的选择；\n"
    "4. 只输出一个 JSON 对象，不要输出任何其他文字或代码块标记。"
)


def _span_bucket(span) -> str:
    """时间跨度分桶（毫秒）：连续值量化——prompt 只随结构变化而变。"""
    if not isinstance(span, (int, float)):
        return "n/a"
    for lo, hi, label in ((0, 60_000, "<1m"), (60_000, 3_600_000, "1m-1h"),
                          (3_600_000, 86_400_000, "1h-1d"),
                          (86_400_000, 604_800_000, "1d-1w")):
        if lo <= span < hi:
            return label
    return ">1w"


def _decision_context(decision_input: dict) -> dict:
    """特征化决策上下文（量化：与确定性核心同一特征抽象，不引入新事实）。

    连续值全部分桶/分档——同质查询生成完全相同的上下文，保证 prompt 确定性
    与 LLM 输出缓存可命中（全量实验可行）；sort_keys 序列化保证 prompt 稳定。
    """
    analysis = decision_input.get("query_analysis") or {}
    qf = analysis.get("query_features") or {}
    t = qf.get("time") or {}
    d = qf.get("device") or {}
    fl = qf.get("filter") or {}
    agg = qf.get("aggregation") or {}
    counts = fl.get("type_counts") or {}
    return {
        "query_type": analysis.get("query_type") or "unknown",
        "query_features": {
            "has_time_filter": bool(t.get("has_time_filter")),
            "range_level": t.get("range_level") or "unknown",
            "time_span": _span_bucket(t.get("time_span")),
            "multi_device": d.get("multi_device"),
            "device_count": _count_bucket(d.get("device_count")),
            "filter_type_counts": {str(k): _count_bucket(v)
                                   for k, v in sorted(counts.items())},
            "has_aggregation": bool(agg.get("has_aggregation")),
            "has_group_by": bool(agg.get("has_group_by")),
            "has_window": bool(agg.get("has_window")),
            "functions": sorted({f.get("function")
                                 for f in agg.get("aggregation_functions") or []}),
            "scan_level": (qf.get("scan") or {}).get("scan_level"),
        },
        "database_state": decision_input.get("database_state") or {},
        "system_state": decision_input.get("system_state") or {},
    }


def _candidate_names(candidates: dict) -> dict:
    """每维度候选策略名列表（提示 LLM 的有界选择空间）。"""
    return {
        dim: [c["name"] if isinstance(c, dict) else c for c in (candidates.get(dim) or [])]
        for dim in DIMENSIONS
    }


def _count_bucket(n) -> str:
    if not isinstance(n, (int, float)) or n < 1:
        return "0"
    for lo, hi in ((1, 2), (3, 5), (6, 10), (11, 20), (21, 50), (51, 100)):
        if lo <= n <= hi:
            return f"{lo}-{hi}"
    return "100+"


def _ratio_bucket(r) -> str:
    """成功率分桶（0.05 粒度）：只随实质性变化改变。"""
    if not isinstance(r, (int, float)):
        return "n/a"
    return f"{(round(float(r) / 0.05) * 0.05):.2f}"


def _latency_bucket(l) -> str:
    if not isinstance(l, (int, float)):
        return "n/a"
    for lo, hi in ((0, 5), (5, 10), (10, 20), (20, 50), (50, 100),
                   (100, 500)):
        if lo <= l < hi:
            return f"{lo}-{hi}ms"
    return "500+ms"


def _experience_digest(decision_input: dict) -> dict:
    """学习经验的聚合摘要（评价表按当前查询类型过滤 + 分桶量化）。

    分桶使摘要只在经验发生**实质性变化**时改变——同质查询轮内 prompt 高度
    稳定，LLM 输出缓存高命中（全量实验可行）；同时把已学到的经验（各策略的
    累计成功率/延迟分布）喂给 LLM，这正是"学习"的体现。
    """
    evals = decision_input.get("strategy_evaluations") or {}
    qtype = ((decision_input.get("query_analysis") or {}).get("query_type")
             or "unknown")
    rows = {}
    for key, e in evals.items():
        parts = str(key).split("|")
        if len(parts) != 3 or parts[0] != qtype:
            continue
        dim, strategy = parts[1], parts[2]
        rows[f"{dim}:{strategy}"] = {
            "count": _count_bucket(e.get("count", 0) if isinstance(e, dict) else 0),
            "success_ratio": _ratio_bucket(e.get("success_ratio")
                                           if isinstance(e, dict) else None),
            "avg_latency_ms": _latency_bucket(e.get("avg_latency_ms")
                                              if isinstance(e, dict) else None),
        }
    return rows


def build_decision_prompt(decision_input: dict, candidates: dict) -> str:
    return (
        "决策上下文：\n" + json.dumps(_decision_context(decision_input),
                                       ensure_ascii=False, sort_keys=True) + "\n\n"
        "候选策略（每维度的合法选择空间）：\n"
        + json.dumps(_candidate_names(candidates), ensure_ascii=False, sort_keys=True)
        + "\n\n"
        "历史学习经验（本查询类型下各策略的累计表现，分桶量化；无则空对象）：\n"
        + json.dumps(_experience_digest(decision_input), ensure_ascii=False,
                     sort_keys=True)
        + "\n\n"
        "请输出 JSON：\n"
        '{"decisions": {"time_pruning": "候选名或null", "filter_order": "...", '
        '"aggregation_placement": "..."}, '
        '"reasons": {"维度名": "<50字以内的选择理由>", ...}}\n'
        "只使用候选列表中出现的策略名；不适用维度 decisions 给 null。"
    )


def validate_llm_decisions(raw: object, candidates: dict, applicable: dict,
                           requirements_met: dict, notes: list) -> dict:
    """合法性校验：策略名 ∈（候选 ∩ 目录）且维度适用且需求满足。非法条目丢弃。

    applicable      ：{dim: bool}（确定性评估的维度适用性）
    requirements_met：{dim: {name: bool}}（确定性评估的需求检查）
    返回 {dim: name}（全部通过校验的维度选择）。
    """
    out: dict = {}
    if not isinstance(raw, dict):
        notes.append("LLM 决策输出不是对象，忽略，回退确定性决策")
        return out
    for dim in DIMENSIONS:
        name = raw.get(dim)
        if name is None:
            continue   # LLM 认为不适用 → 该维度保持确定性决策
        if not applicable.get(dim, False):
            notes.append(f"LLM 决策忽略（维度不适用）：{dim}={name}")
            continue
        allowed = {c["name"] if isinstance(c, dict) else c
                   for c in (candidates.get(dim) or [])}
        if name not in allowed:
            notes.append(f"LLM 决策忽略（候选外策略名）：{dim}={name}")
            continue
        if requirements_met.get(dim, {}).get(name) is not True:
            notes.append(f"LLM 决策忽略（需求不满足）：{dim}={name}")
            continue
        out[dim] = name
    return out


def request_llm_decision(llm: LLMClient, decision_input: dict, candidates: dict,
                         applicable: dict, requirements_met: dict,
                         notes: list) -> dict:
    """请求 LLM 决策（decision_mode=llm_primary）。失败/非法 → 返回校验后的
    空/部分结果；调用方对未覆盖维度保持确定性决策。"""
    try:
        text = llm.chat([
            {"role": "system", "content": DECISION_SYSTEM_PROMPT},
            {"role": "user", "content": build_decision_prompt(decision_input,
                                                              candidates)},
        ])
    except Exception as exc:
        notes.append(f"LLM 决策请求异常（{type(exc).__name__}），回退确定性决策")
        return {}
    if text is None:
        notes.append("LLM 决策请求失败（超时/网络/服务不可用），回退确定性决策")
        return {}
    data = parse_llm_json(text, notes)
    if data is None:
        notes.append("LLM 决策输出解析失败，回退确定性决策")
        return {}
    decisions = validate_llm_decisions(data.get("decisions"), candidates,
                                       applicable, requirements_met, notes)
    reasons = data.get("reasons") if isinstance(data, dict) else None
    if decisions:
        if isinstance(reasons, dict):
            return {"decisions": decisions, "reasons": reasons}
        return {"decisions": decisions, "reasons": {}}
    return {}


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
