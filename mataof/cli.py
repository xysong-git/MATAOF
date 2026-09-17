"""MATAOF 命令行入口。

用法（安装后或 python -m mataof）：
    mataof run "SELECT ..."                       # 运行一条查询
    mataof run --file queries.sql                 # 批量运行 .sql / .jsonl
    mataof retrieve "SELECT ..."                  # 仅检索历史知识
    mataof stats                                  # 知识库统计

公共参数：
    --config CONFIG       JSON 配置文件（见 config.example.json）
    --results-dir DIR     覆盖追踪输出目录
    --json                机器可读输出（stdout 为 JSON）
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from mataof.runner import PipelineRunner, RunnerConfig, load_config_file


def _build_runner(args) -> PipelineRunner:
    config = RunnerConfig()
    if getattr(args, "config", None):
        config = load_config_file(args.config)
    if getattr(args, "results_dir", None):
        config.results_dir = args.results_dir
    return PipelineRunner(config)


def _cmd_run(args) -> int:
    runner = _build_runner(args)
    queries: list[tuple[str, str]] = []
    for q in getattr(args, "query", None) or []:
        queries.append((q, ""))
    if getattr(args, "file", None):
        if args.file.endswith(".jsonl"):
            with open(args.file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    item = json.loads(line)
                    queries.append((str(item.get("query") or ""), str(item.get("query_id") or "")))
        else:
            with open(args.file, encoding="utf-8") as f:
                text = f.read()
            queries.extend((s.strip(), "") for s in text.split(";") if s.strip())
    if not queries:
        print("错误：请提供查询（位置参数或 --file）", file=sys.stderr)
        return 2

    traces = []
    for query, query_id in queries:
        trace = runner.run_query(query, query_id=query_id)
        traces.append(trace)
        mon = trace["monitoring"]
        # --json 模式：摘要走 stderr，stdout 只输出纯 JSON（机器可读）
        out = sys.stderr if args.json else sys.stdout
        print(f"[{trace['query_id']}] type={trace['analysis']['query_type']} | "
              f"strategy={trace['decision']['selected_strategy']['strategy_id']} | "
              f"status={mon['execution_status']} | assessment={mon['performance_assessment']} | "
              f"anomalies={len(mon['anomalies'])} | "
              f"knowledge_conf={trace['knowledge_retrieval']['knowledge_confidence']}",
              file=out)
    if args.json:
        print(json.dumps(traces, ensure_ascii=False))
    return 0


def _cmd_retrieve(args) -> int:
    runner = _build_runner(args)
    result = runner.retrieve(args.query, query_id=args.query_id or "")
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print(f"匹配记录：{len(result['matched_records'])} 条，"
          f"knowledge_confidence={result['knowledge_confidence']}")
    for m in result["matched_records"][:20]:
        print(f"  {m['record_id']}  sim={m['similarity']} ({m['match_level']}) "
              f"strategy={m['strategy_id']} result={m['execution_result']}")
    if result["historical_summary"]["successful_strategies"]:
        print("成功模式：")
        for s in result["historical_summary"]["successful_strategies"]:
            print(f"  {s['strategy_id']} count={s['count']} ratio={s['success_ratio']}")
    if result["historical_summary"]["failed_strategies"]:
        print("失败经验：")
        for s in result["historical_summary"]["failed_strategies"]:
            print(f"  {s['strategy_id']} count={s['count']} types={s['failure_types']}")
    return 0


def _cmd_stats(args) -> int:
    runner = _build_runner(args)
    stats = runner.stats()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    print(f"知识库记录总数：{stats['total_records']}")
    for sid, s in sorted(stats["strategy_statistics"].items()):
        print(f"  {sid}: total={s['total']} success={s['success']} failure={s['failure']} "
              f"ratio={s['success_ratio']} avg_latency_ms={s['avg_latency_ms']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mataof",
        description="MATAOF：面向时序数据库的多智能体自适应查询优化系统",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="运行查询（四 Agent 闭环）")
    p_run.add_argument("query", nargs="*", help="查询 SQL（可多条）")
    p_run.add_argument("--file", help="查询文件（.sql 按分号拆分；.jsonl 每行 {query_id, query}）")
    p_run.add_argument("--config", help="JSON 配置文件")
    p_run.add_argument("--results-dir", help="追踪输出目录（覆盖配置）")
    p_run.add_argument("--json", action="store_true", help="stdout 输出 JSON")
    p_run.set_defaults(func=_cmd_run)

    p_ret = sub.add_parser("retrieve", help="仅检索历史知识（不执行、不入库）")
    p_ret.add_argument("query", help="查询 SQL")
    p_ret.add_argument("--query-id", default="", help="查询标识")
    p_ret.add_argument("--config", help="JSON 配置文件")
    p_ret.add_argument("--json", action="store_true", help="stdout 输出 JSON")
    p_ret.set_defaults(func=_cmd_retrieve)

    p_stats = sub.add_parser("stats", help="知识库统计")
    p_stats.add_argument("--config", help="JSON 配置文件")
    p_stats.add_argument("--json", action="store_true", help="stdout 输出 JSON")
    p_stats.set_defaults(func=_cmd_stats)
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
