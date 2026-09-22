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


def _fact_numbers(monitoring_output: dict) -> set:
    """收集给定监控数据中的所有数值（含 baseline 对比百分比与置信度）。"""
    facts: set = set()
    metrics = monitoring_output.get("metrics") or {}
    for v in metrics.values():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            facts.add(v)
    cmp = monitoring_output.get("baseline_comparison") or {}
    for v in cmp.values():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            facts.add(v)
    conf = (monitoring_output.get("feedback") or {}).get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        facts.add(conf)
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
