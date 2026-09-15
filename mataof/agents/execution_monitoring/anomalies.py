"""异常识别（只记录，不修改任何策略）。

识别的异常类型（全部基于实测数据与给定阈值/参照，无参照则不判断）：
- execution_timeout / execution_failed / execution_cancelled ：由执行状态触发；
- latency_high        ：response_time 超过绝对阈值（默认 10 s）；
- cpu_high / memory_high：利用率超过阈值（默认 0.9）；
- io_anomaly          ：IO 吞吐相对 baseline 偏离超过倍数（默认 1.5×）；
- latency_deviation   ：延迟相对 baseline 偏离超过倍数（默认 2×）；
- historical_deviation：延迟相对历史典型值偏离超过倍数（默认 2×）。

异常条目：{"type", "severity"(critical|warning), "metric", "observed", "reference", "description"}。
"""

from __future__ import annotations

from typing import Any, Optional


def _anomaly(type_: str, severity: str, metric: Optional[str], observed: Any,
             reference: Any, description: str) -> dict:
    return {
        "type": type_,
        "severity": severity,
        "metric": metric,
        "observed": observed,
        "reference": reference,
        "description": description,
    }


def detect_anomalies(status: str, failure_reason: Optional[str], metrics: dict,
                      baseline: dict, historical_reference: Optional[dict],
                      thresholds: dict) -> list[dict]:
    out: list[dict] = []

    # ---- 执行状态类异常 ----
    if status == "timeout":
        out.append(_anomaly("execution_timeout", "critical", None, "timeout", None,
                            "查询执行超时"))
    elif status == "failed":
        out.append(_anomaly("execution_failed", "critical", None, "failed",
                            failure_reason or "未提供失败原因",
                            f"查询执行失败：{failure_reason or '未提供失败原因'}"))
    elif status == "cancelled":
        out.append(_anomaly("execution_cancelled", "warning", None, "cancelled", None,
                            "查询执行被取消"))

    rt = metrics.get("response_time_ms")

    # ---- 绝对延迟异常 ----
    if rt is not None and rt > thresholds["response_time_timeout_ms"]:
        out.append(_anomaly("latency_high", "critical", "response_time_ms", rt,
                            thresholds["response_time_timeout_ms"],
                            f"延迟 {rt} ms 超过阈值 {thresholds['response_time_timeout_ms']} ms"))

    # ---- 资源利用率异常 ----
    cpu = metrics.get("cpu_utilization")
    if cpu is not None and cpu > thresholds["cpu_threshold"]:
        out.append(_anomaly("cpu_high", "warning", "cpu_utilization", cpu,
                            thresholds["cpu_threshold"],
                            f"CPU 利用率 {cpu:.2f} 超过阈值 {thresholds['cpu_threshold']}"))
    mem = metrics.get("memory_utilization")
    if mem is not None and mem > thresholds["memory_threshold"]:
        out.append(_anomaly("memory_high", "warning", "memory_utilization", mem,
                            thresholds["memory_threshold"],
                            f"内存利用率 {mem:.2f} 超过阈值 {thresholds['memory_threshold']}"))

    # ---- 相对 baseline 的偏离 ----
    base_rt = baseline.get("response_time_ms")
    if rt is not None and base_rt is not None and rt > base_rt * thresholds["latency_deviation_factor"]:
        out.append(_anomaly("latency_deviation", "critical", "response_time_ms", rt, base_rt,
                            f"延迟 {rt} ms 相对 baseline {base_rt} ms 偏离超过 "
                            f"{thresholds['latency_deviation_factor']}×"))
    io = metrics.get("io_throughput")
    base_io = baseline.get("io_throughput")
    if io is not None and base_io is not None and io > base_io * thresholds["io_deviation_factor"]:
        out.append(_anomaly("io_anomaly", "warning", "io_throughput", io, base_io,
                            f"IO 吞吐 {io} 相对 baseline {base_io} 偏离超过 "
                            f"{thresholds['io_deviation_factor']}×"))

    # ---- 相对历史表现的偏离（策略执行结果与历史表现明显偏离）----
    if isinstance(historical_reference, dict):
        hist_rt = historical_reference.get("response_time_ms")
        if (rt is not None and isinstance(hist_rt, (int, float))
                and not isinstance(hist_rt, bool) and hist_rt > 0
                and rt > hist_rt * thresholds["history_deviation_factor"]):
            out.append(_anomaly("historical_deviation", "warning", "response_time_ms",
                                rt, hist_rt,
                                f"延迟 {rt} ms 相对历史典型值 {hist_rt} ms 偏离超过 "
                                f"{thresholds['history_deviation_factor']}×"))

    return out
