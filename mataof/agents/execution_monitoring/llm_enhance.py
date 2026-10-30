"""Execution Monitoring Agent 的 LLM 语义增强（混合增强模式）。

这是"事实采集"Agent：LLM 只做观测事实的复述与关联，不产生任何新事实：
1. performance_summary：性能表现的简洁复述（数字必须来自给定数据）；
2. anomaly_summary   ：异常组合的观测关联（只允许"同时发生/同时观测到"，
   禁止因果归因断言、禁止策略建议）。

**数字级防虚构校验**：LLM 输出中的每个数字必须出现在给定事实数据中
（metrics / baseline_comparison / confidence 的数值集合），出现任何编造数字 →
对应字段置 null + notes。把"绝不虚构实验结果"落实到数字级。

LLM 不可用/超时/输出非法 → available=false + notes，事实字段完全不变（兜底）。
"""

from __future__ import annotations

import json
import re
from typing import Optional

from mataof.llm import LLMClient

SYSTEM_PROMPT = (
    "你是一个执行监控解读助手。你只复述观测事实，不产生任何新事实。\n"
    "严格约束：\n"
    "1. 你输出中的每一个数字都必须原样来自给定的监控数据，"
    "禁止计算、估计或编造任何数字；\n"
    "2. 禁止因果归因断言（如「因为A所以B」），只允许说「同时发生/同时观测到」；\n"
    "3. 禁止提出任何策略建议或修改建议（监控只记录，不改策略）；\n"
    "4. 不得把单次执行结果表述为规律；\n"
    "5. 只输出一个 JSON 对象，不要输出任何其他文字或代码块标记。"
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _add_number(facts: set, value) -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        facts.add(value)


def _fact_numbers(monitoring_output: dict) -> set:
    """收集给定监控数据中的所有数值。

    包含 metrics、baseline_comparison、performance_change、confidence，
    以及 anomalies 的 observed/reference 和确定性 description 中的数字——
    这些都在 prompt 里给到 LLM，复述它们不属于编造；此前漏掉 anomalies
    会把合法的异常解读全部误判为编造（74/74 被拒）。
    """
    facts: set = set()
    for v in (monitoring_output.get("metrics") or {}).values():
        _add_number(facts, v)
    for v in (monitoring_output.get("baseline_comparison") or {}).values():
        _add_number(facts, v)
    change = monitoring_output.get("performance_change")
    for v in (change if isinstance(change, (list, tuple)) else [change]):
        _add_number(facts, v)
    conf = (monitoring_output.get("feedback") or {}).get("confidence")
    _add_number(facts, conf)
    for anomaly in monitoring_output.get("anomalies") or []:
        if not isinstance(anomaly, dict):
            continue
        for key in ("observed", "reference", "value", "threshold",
                    "deviation", "ratio"):
            _add_number(facts, anomaly.get(key))
        facts.update(_numbers_in(str(anomaly.get("description") or "")))
    return facts


def _numbers_in(text: str) -> list:
    return [float(m) for m in _NUMBER_RE.findall(text or "")]


def number_consistency_check(text: str, facts: set) -> bool:
    """数字级校验：文本中的每个数字的**量级**必须出现在事实集合（容差 1e-9）。

    - 忽略符号：数据中的 -38.5 与表述中的「降低 38.5%」是同一事实，
      符号语义由定性文字承载；
    - 允许 ×100 / ÷100 单位换算：数据 0.42 与表述「CPU 利用率 42%」是同一事实；
    - 量级校验仍能拒绝任何编造数字（如凭空出现 99.9、150、3 倍）。
    """
    magnitudes: set = set()
    for f in facts:
        m = abs(f)
        magnitudes.add(m)
        magnitudes.add(round(m * 100, 9))
        magnitudes.add(round(m / 100, 9))
    for n in _numbers_in(text):
        if not any(abs(abs(n) - m) < 1e-9 for m in magnitudes):
            return False
    return True


def _context(monitoring_output: dict) -> dict:
    return {
        "execution_status": monitoring_output.get("execution_status"),
        "failure_reason": monitoring_output.get("failure_reason"),
        "metrics": monitoring_output.get("metrics"),
        "baseline_comparison": monitoring_output.get("baseline_comparison"),
        "performance_change": monitoring_output.get("performance_change"),
        "performance_assessment": monitoring_output.get("performance_assessment"),
        "anomalies": [
            {"type": a.get("type"), "severity": a.get("severity"),
             "metric": a.get("metric"), "observed": a.get("observed"),
             "reference": a.get("reference"), "description": a.get("description")}
            for a in (monitoring_output.get("anomalies") or [])
        ],
    }


def build_prompt(monitoring_output: dict) -> str:
    return (
        "监控数据：\n" + json.dumps(_context(monitoring_output), ensure_ascii=False) + "\n\n"
        "请输出 JSON：\n"
        '{"performance_summary": "<100字以内，中文，复述本次执行的关键数字与效果判断；'
        '每个数字必须原样来自监控数据>",\n'
        ' "anomaly_summary": "<80字以内，中文，只复述异常观测及其关联（同时发生）；'
        '无异常时填 null>"}'
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


def build_post_execution_llm(llm: LLMClient, monitoring_input: dict,
                             monitoring_output: dict,
                             include_experience_note: bool = True) -> dict:
    """执行后 LLM 调用：产出一致性校验后的监控解读。

    include_experience_note=True（llm_shared=false 旧路径）：同一次调用
    额外产出 KM 经验总结；False（B/方案一）：只做监控解读，经验总结由
    KM 按查询形状单独生成（避免逐执行生成、跨执行复用）。
    输入：监控输入（含 query/strategy 等）与确定性监控输出（事实）。
    输出：{"performance_summary", "anomaly_summary", "experience_note", "notes"}
    数字级校验对 performance/anomaly 字段生效（与独立调用口径一致）；
    调用失败/非法 → 对应字段 None + notes，事实输出不受影响。
    """
    section = {
        "performance_summary": None,
        "anomaly_summary": None,
        "experience_note": None,
        "notes": [],
    }
    extra = ""
    if include_experience_note:
        extra = ("\n\n追加要求：再输出一个 experience_note 字段（<80字，中文，"
                 "总结本次执行经验：什么策略在什么条件下表现如何；只陈述事实，"
                 "单次观察不代表规律，不提建议；若执行未成功，写失败现象即可）。"
                 "输出仍是一个 JSON 对象。")
    try:
        text = llm.chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(monitoring_output) + extra
             + f"\n输入的 strategy_id 为：{monitoring_input.get('strategy_id') or ''}"},
        ])
    except Exception as exc:
        section["notes"].append(f"合并 LLM 调用异常（{type(exc).__name__}），回退确定性路径")
        return section
    if text is None:
        section["notes"].append("合并 LLM 调用失败（超时/网络/服务不可用），回退确定性路径")
        return section
    data = parse_llm_json(text, section["notes"])
    if data is None:
        return section

    facts = _fact_numbers(monitoring_output)
    summary = data.get("performance_summary")
    if isinstance(summary, str) and summary.strip():
        if number_consistency_check(summary, facts):
            section["performance_summary"] = summary.strip()
        else:
            section["notes"].append("性能复述包含给定数据之外的数字（疑似编造），置为 null")
    anomaly = data.get("anomaly_summary")
    if isinstance(anomaly, str) and anomaly.strip():
        if number_consistency_check(anomaly, facts):
            section["anomaly_summary"] = anomaly.strip()
        else:
            section["notes"].append("异常解读包含给定数据之外的数字（疑似编造），置为 null")
    note = data.get("experience_note")
    if isinstance(note, str) and note.strip():
        section["experience_note"] = note.strip()
    return section


def build_llm_analysis(llm: Optional[LLMClient], monitoring_output: dict) -> dict:
    """生成监控输出的 llm_analysis 节。llm=None/失败 → available=false（兜底）。"""
    section = {
        "available": False,
        "performance_summary": None,
        "anomaly_summary": None,
        "notes": [],
    }
    if llm is None:
        section["notes"].append("LLM 未启用（provider=none 或不可用）")
        return section
    try:
        text = llm.chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(monitoring_output)},
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

    facts = _fact_numbers(monitoring_output)

    summary = data.get("performance_summary")
    if isinstance(summary, str) and summary.strip():
        if number_consistency_check(summary, facts):
            section["available"] = True
            section["performance_summary"] = summary.strip()
        else:
            section["notes"].append("性能复述包含给定数据之外的数字（疑似编造），置为 null")
    else:
        section["notes"].append("LLM 未给出性能复述，置为 null")

    anomaly = data.get("anomaly_summary")
    if isinstance(anomaly, str) and anomaly.strip():
        if number_consistency_check(anomaly, facts):
            section["available"] = True
            section["anomaly_summary"] = anomaly.strip()
        else:
            section["notes"].append("异常解读包含给定数据之外的数字（疑似编造），置为 null")
    return section
