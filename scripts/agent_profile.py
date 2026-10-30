#!/usr/bin/env python3
"""Agent 行为剖面：从 run_log.jsonl 汇总“Agent 是否真的在行动”。

用途（2×2 实验的行为证据，而非仅架构声明）：
- 控制流分布：pipeline / agent / fixed / legacy；
- 每个 Agent 的步数、终止原因、动作序列哈希（不同控制流数量）；
- 事件触发率：replan / recall / QA LLM / EM LLM / KM 经验 / 自适应扩 K；
- 总线 intent 分布（只作路由统计，不把消息数本身当协作证据）；
- 成本与结果：overhead 分位数、执行状态、效果分布、replan/recall 子集结果。

输入：一个或多个 run_log.jsonl；跳过 batch 汇总行；--run-id 过滤本轮。
输出：可读表格（stdout）+ 可选 JSON（--json-out）。

运行示例：
  python3 scripts/agent_profile.py --run-id round-20261030-120000 \
      results/round_pipeline_dbs/run_log.jsonl \
      results/round_agent_dbs/run_log.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.2f}%" if d else "n/a"


def _quantile(values: list[float], q: float) -> float | None:
    """最近秩法分位数（与 report.py 口径一致；空样本 → None）。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = max(0, min(len(ordered) - 1,
                     int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "mean": None, "p50": None,
                "p95": None, "max": None}
    return {
        "count": len(values),
        "mean": round(statistics.mean(values), 3),
        "p50": round(_quantile(values, 0.50), 3),
        "p95": round(_quantile(values, 0.95), 3),
        "max": round(max(values), 3),
    }


def _load_rows(paths: list[str], run_id: str | None) -> tuple[list[dict],
                                                              list[str]]:
    rows: list[dict] = []
    missing: list[str] = []
    for path in paths:
        if not os.path.exists(path):
            missing.append(path)
            continue
        run_dir = os.path.basename(os.path.dirname(os.path.abspath(path)))
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("batch"):
                    continue
                if run_id and row.get("run_id") != run_id:
                    continue
                row["_run_dir"] = run_dir
                rows.append(row)
    return rows, missing


def _trigger(row: dict, key: str) -> bool:
    return bool((row.get("agent_triggers") or {}).get(key))


def _outcome_summary(rows: list[dict]) -> dict:
    assessments = Counter(str(r.get("performance_assessment"))
                          for r in rows)
    statuses = Counter(str(r.get("execution_status")) for r in rows)
    total = len(rows)
    return {
        "count": total,
        "execution_status": dict(statuses),
        "performance_assessment": dict(assessments),
        "improved_rate": (
            assessments.get("improved", 0) / total if total else None),
        "degraded_rate": (
            assessments.get("degraded", 0) / total if total else None),
    }


def summarize(rows: list[dict], run_id: str | None,
              missing: list[str]) -> dict:
    total = len(rows)
    control = Counter(str(r.get("control_flow")) for r in rows)
    llm_enabled = Counter(str(r.get("llm_enabled")) for r in rows)
    decision_mode = Counter(str(r.get("decision_mode")) for r in rows)
    agents_enabled = Counter(
        ",".join(sorted(r.get("agents_enabled") or [])) or "<none>"
        for r in rows)

    steps_by_agent: dict[str, list[int]] = defaultdict(list)
    terminations: dict[str, Counter] = defaultdict(Counter)
    actions: Counter = Counter()
    action_hashes: Counter = Counter()
    trigger_counts: Counter = Counter()
    trigger_combos: Counter = Counter()
    llm_engaged: Counter = Counter()
    bus_intents: Counter = Counter()
    overhead_values: dict[str, list[float]] = defaultdict(list)
    replan_outcomes: list[dict] = []
    recall_outcomes: list[dict] = []
    km_new_note_outcomes: list[dict] = []

    for row in rows:
        for name, steps in (row.get("agent_steps_by_agent") or {}).items():
            if isinstance(steps, (int, float)) and not isinstance(steps, bool):
                steps_by_agent[name].append(int(steps))
        for name, term in (row.get("agent_terminations") or {}).items():
            terminations[name][str(term)] += 1
        for action, n in (row.get("agent_action_counts") or {}).items():
            if isinstance(n, int):
                actions[str(action)] += n
        if row.get("agent_action_hash"):
            action_hashes[str(row["agent_action_hash"])] += 1
        trig = row.get("agent_triggers") or {}
        active = tuple(sorted(k for k, v in trig.items()
                              if isinstance(v, bool) and v))
        if active:
            trigger_combos[",".join(active)] += 1
        for key in ("replan", "recall", "qa_llm", "em_llm", "km_llm",
                    "km_note_reused", "km_adaptive_expand",
                    "od_llm_decision"):
            if trig.get(key) is True:
                trigger_counts[key] += 1
        if isinstance(trig.get("llm_engaged_count"), int):
            llm_engaged[trig["llm_engaged_count"]] += 1
        for intent, n in (row.get("bus_intents") or {}).items():
            if isinstance(n, int):
                bus_intents[str(intent)] += n
        for key in ("pipeline_overhead_ms", "post_llm_ms",
                    "knowledge_update_ms", "end_to_end_ms", "decision_ms",
                    "retrieval_ms", "analysis_ms", "monitoring_ms"):
            value = row.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                overhead_values[key].append(float(value))
        if trig.get("replan") is True:
            replan_outcomes.append(row)
        if trig.get("recall") is True:
            recall_outcomes.append(row)
        if trig.get("km_llm") is True:
            km_new_note_outcomes.append(row)

    return {
        "run_id": run_id,
        "queries": total,
        "run_dirs": sorted({r.get("_run_dir") for r in rows}),
        "missing_files": missing,
        "control_flow": dict(control),
        "llm_enabled": dict(llm_enabled),
        "decision_mode": dict(decision_mode),
        "agents_enabled": dict(agents_enabled),
        "steps_by_agent": {
            name: {
                "count": len(values),
                "mean": round(statistics.mean(values), 3),
                "p50": _quantile(values, 0.50),
                "p95": _quantile(values, 0.95),
                "max": max(values),
                "histogram": dict(Counter(values)),
            }
            for name, values in sorted(steps_by_agent.items())
        },
        "terminations": {
            name: dict(counter.most_common())
            for name, counter in sorted(terminations.items())
        },
        "actions": dict(actions.most_common()),
        "action_sequences": {
            "distinct_hashes": len(action_hashes),
            "top": action_hashes.most_common(10),
        },
        "triggers": {
            key: {
                "count": trigger_counts.get(key, 0),
                "rate": (trigger_counts.get(key, 0) / total
                         if total else None),
            }
            for key in ("replan", "recall", "qa_llm", "em_llm", "km_llm",
                        "km_note_reused", "km_adaptive_expand",
                        "od_llm_decision")
        },
        "trigger_combinations": dict(trigger_combos.most_common(15)),
        "llm_engaged_count": dict(sorted(llm_engaged.items())),
        "bus_intents": dict(bus_intents.most_common()),
        "overhead_ms": {key: _stats(values)
                        for key, values in sorted(overhead_values.items())},
        "outcomes": _outcome_summary(rows),
        "replan_subset": _outcome_summary(replan_outcomes),
        "recall_subset": _outcome_summary(recall_outcomes),
        "km_new_note_subset": _outcome_summary(km_new_note_outcomes),
    }


def _print_counter(title: str, counter: dict, total: int | None = None) -> None:
    print(f"\n-- {title} --")
    if not counter:
        print("  (none)")
        return
    for key, value in counter.items():
        if total and isinstance(value, int):
            print(f"  {key}: {value} ({_pct(value, total)})")
        else:
            print(f"  {key}: {value}")


def print_report(s: dict) -> None:
    n = s["queries"]
    print("=" * 72)
    print(f"Agent Activation Profile  run_id={s['run_id'] or '<all>'}  "
          f"queries={n}")
    print(f"run_dirs: {', '.join(s['run_dirs']) or '(none)'}")
    if s["missing_files"]:
        print(f"missing files: {', '.join(s['missing_files'])}")
    print("=" * 72)

    _print_counter("Control flow", s["control_flow"], n)
    _print_counter("LLM enabled", s["llm_enabled"], n)
    _print_counter("Decision mode", s["decision_mode"], n)
    _print_counter("Agents enabled", s["agents_enabled"], n)

    print("\n-- Steps by agent --")
    if not s["steps_by_agent"]:
        print("  (none)")
    for name, st in s["steps_by_agent"].items():
        print(f"  {name}: n={st['count']} mean={st['mean']} "
              f"p50={st['p50']} p95={st['p95']} max={st['max']} "
              f"hist={st['histogram']}")

    print("\n-- Terminations --")
    if not s["terminations"]:
        print("  (none)")
    for name, terms in s["terminations"].items():
        top = ", ".join(f"{k}={v}" for k, v in list(terms.items())[:8])
        print(f"  {name}: {top}")

    print("\n-- Trigger rates --")
    for key, st in s["triggers"].items():
        print(f"  {key}: {st['count']} ({_pct(st['count'], n)})")

    _print_counter("LLM engaged per query", s["llm_engaged_count"], n)
    _print_counter("Trigger combinations", s["trigger_combinations"], n)
    _print_counter("Bus intents", s["bus_intents"])

    print(f"\n-- Action sequences --\n  distinct_hashes="
          f"{s['action_sequences']['distinct_hashes']}")
    for digest, count in s["action_sequences"]["top"]:
        print(f"  {digest}: {count}")

    print("\n-- Overhead (ms) --")
    for key, st in s["overhead_ms"].items():
        if st["count"]:
            print(f"  {key}: mean={st['mean']} p50={st['p50']} "
                  f"p95={st['p95']} max={st['max']}")

    def _outcome(label: str, o: dict) -> None:
        print(f"\n-- {label} --")
        print(f"  n={o['count']} improved={o['improved_rate']} "
              f"degraded={o['degraded_rate']} "
              f"status={o['execution_status']}")

    _outcome("All queries", s["outcomes"])
    _outcome("Replan subset", s["replan_subset"])
    _outcome("Recall subset", s["recall_subset"])
    _outcome("KM new-note subset", s["km_new_note_subset"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="汇总 MATAOF run_log 的 Agent 行为剖面")
    parser.add_argument("paths", nargs="+", help="run_log.jsonl 路径")
    parser.add_argument("--run-id", default=None, help="只统计该轮 run_id")
    parser.add_argument("--json-out", default=None,
                        help="把机器可读汇总写入该 JSON 文件")
    args = parser.parse_args(argv)

    rows, missing = _load_rows(args.paths, args.run_id)
    if not rows:
        print("错误：没有可统计的 run_log 行（检查路径/--run-id）",
              file=sys.stderr)
        return 1
    summary = summarize(rows, args.run_id, missing)
    print_report(summary)
    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)),
                    exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 汇总已写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
