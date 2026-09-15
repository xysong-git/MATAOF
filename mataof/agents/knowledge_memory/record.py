"""历史记录装配：把一次完整查询执行关联为一条结构化知识记录。

关联链：Query + Query Features + Database State + System State
        + Selected Strategy + Execution Metrics + Execution Result + Timestamp

装配规则：
- 一切内容只来自三个 Agent 的真实输出（query_analysis / decision / monitoring），
  绝不编造任何字段；
- 缺少 monitoring（无执行反馈）→ 不形成执行记录（返回 rejected 原因）；
- 时间戳只接受输入提供的值（执行时间）；缺失 → null + 说明（不编造）；
- strategy 的按维度映射优先取自 decision 输出，否则从 strategy_id 字符串解析
  （"dim=strategy|dim=strategy"），解析不了的部分不存储。
"""

from __future__ import annotations

from typing import Optional

from mataof.similarity import compact_features


def parse_strategy_id(strategy_id: str) -> dict:
    """从 strategy_id 字符串解析按维度策略映射；无法解析的片段跳过。"""
    out: dict = {}
    for part in (strategy_id or "").split("|"):
        if "=" not in part:
            continue
        dim, _, name = part.partition("=")
        if dim and name:
            out[dim.strip()] = name.strip()
    return out


def assemble_record(feedback_input: dict, notes: list[str]) -> tuple[Optional[dict], Optional[str]]:
    """装配一条知识记录。返回 (record, rejected_reason)。

    返回 None 记录时给出拒绝原因（不存储任何内容）。
    """
    inp = feedback_input if isinstance(feedback_input, dict) else {}

    query_id = str(inp.get("query_id") or "")
    if not query_id:
        return None, "rejected_invalid: 缺少 query_id"

    analysis = inp.get("query_analysis")
    monitoring = inp.get("monitoring")
    decision = inp.get("decision")

    # 执行反馈是形成历史记录的必要条件（无执行 → 无事实可记）
    if not isinstance(monitoring, dict):
        return None, "rejected_missing_execution: 缺少 monitoring（执行监控输出），无执行事实可记录"
    status = monitoring.get("execution_status")
    if status is None:
        return None, "rejected_missing_execution: monitoring 缺少 execution_status"

    # ---- query_record ----
    qf_compact: dict = {}
    if isinstance(analysis, dict) and analysis.get("query_features"):
        qf_compact = compact_features(analysis)
    record = {
        "query_record": {
            "query_id": query_id,
            "query": str(inp.get("query") or ""),
            "query_features": qf_compact,
        },
        # ---- context ----
        "context": {
            "database_state": inp.get("database_state")
                              if isinstance(inp.get("database_state"), dict) else {},
            "system_state": inp.get("system_state")
                            if isinstance(inp.get("system_state"), dict) else {},
        },
        # ---- strategy ----
        "strategy": {
            "strategy_id": "",
            "strategy_parameters": {},
            "by_dimension": {},
        },
        # ---- execution ----
        "execution": {
            "status": status,
            "failure_reason": monitoring.get("failure_reason"),
            "metrics": monitoring.get("metrics") if isinstance(monitoring.get("metrics"), dict) else {},
        },
        "execution_result": monitoring.get("performance_assessment") or "insufficient_evidence",
        "anomalies": monitoring.get("anomalies") if isinstance(monitoring.get("anomalies"), list) else [],
        # ---- experience ----
        "experience": {
            "effect": monitoring.get("performance_assessment") or "insufficient_evidence",
            "confidence": monitoring.get("feedback", {}).get("confidence")
                          if isinstance(monitoring.get("feedback"), dict) else None,
        },
        "timestamp": None,
    }

    # 策略来源：decision 输出优先，其次 strategy_id 字符串
    sid = ""
    if isinstance(decision, dict):
        ss = decision.get("selected_strategy") or {}
        sid = str(ss.get("strategy_id") or "")
        if isinstance(ss.get("strategy_parameters"), dict):
            record["strategy"]["strategy_parameters"] = ss["strategy_parameters"]
        by_dim = {}
        dec = decision.get("decision") or {}
        for dim, v in dec.items():
            if isinstance(v, dict) and v.get("strategy"):
                by_dim[dim] = v["strategy"]
        record["strategy"]["by_dimension"] = by_dim
    sid = sid or str(monitoring.get("strategy_id") or "")
    if not sid:
        sid = str(inp.get("strategy_id") or "")
    record["strategy"]["strategy_id"] = sid
    if not record["strategy"]["by_dimension"]:
        record["strategy"]["by_dimension"] = parse_strategy_id(sid)

    # ---- 时间戳：只接受输入提供的值 ----
    ts = inp.get("timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        record["timestamp"] = int(ts)
    else:
        notes.append("未提供执行时间戳，timestamp 置为 null（不编造）")

    return record, None
