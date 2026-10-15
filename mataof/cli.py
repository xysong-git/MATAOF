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
import os
import sys
import time
from typing import Optional

from mataof.runner import PipelineRunner, RunnerConfig, load_config_file


def _build_runner(args) -> PipelineRunner:
    config = RunnerConfig()
    if getattr(args, "config", None):
        config = load_config_file(args.config)
    if getattr(args, "results_dir", None):
        config.results_dir = args.results_dir
    if getattr(args, "run_id", None):
        config.run_id = args.run_id
    return PipelineRunner(config)


def _cmd_run(args) -> int:
    runner = _build_runner(args)
    pos_queries = list(getattr(args, "query", None) or [])

    # --file 支持多值（含 shell 通配展开）：扁平化 + 严格校验
    files: list = []
    for group in (getattr(args, "file", None) or []):
        files.extend(group)
    for f in files:
        if not os.path.exists(f):
            print(f"错误: 查询文件不存在: {f}（若使用通配符，请确认展开结果）", file=sys.stderr)
            return 2
        if not f.endswith((".sql", ".jsonl")):
            print(f"错误: --file 只接受 .sql/.jsonl 文件: {f}", file=sys.stderr)
            return 2

    # 位置参数若像文件路径（如通配符展开残片）→ 明确报错，防止把路径当 SQL 执行
    for q in pos_queries:
        if q.endswith((".sql", ".jsonl")) or "*" in q or os.path.exists(q):
            print(f"错误: 位置参数看起来是文件路径（{q}）——查询文件请用 --file 指定，"
                  f"且 --file 须放在位置参数之后", file=sys.stderr)
            return 2

    # 位置参数查询：全局 --limit 截断（--file 模式下 --limit 自动作为每文件上限）
    global_limit = getattr(args, "limit", None)
    if files and getattr(args, "per_file_limit", None) is None:
        args.per_file_limit = global_limit
        global_limit = None
    if global_limit:
        pos_queries = pos_queries[: int(global_limit)]

    if not pos_queries and not files:
        print("错误：请提供查询（位置参数或 --file）", file=sys.stderr)
        return 2

    t_batch = time.time()
    traces: list = []
    # tqdm 进度条：实时显示每条查询完成（stderr；非 TTY 或 --json 自动禁用）
    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None
    use_bar = (tqdm is not None) and (not args.json) and sys.stderr.isatty()

    from mataof.report import batch_summary, format_batch_summary

    if pos_queries:
        bar = tqdm(total=len(pos_queries), desc="位置参数查询", unit="条",
                   file=sys.stderr) if use_bar else None
        t0 = time.time()
        for q in pos_queries:
            traces.append(runner.run_query(q))
            if bar is not None:
                bar.update(1)
        if bar is not None:
            bar.close()
        pos_time = round(time.time() - t0, 2)
        runner.append_batch_row(pos_time, len(pos_queries), source="位置参数")

    total_files = len(files)
    for idx, f in enumerate(files, 1):
        desc = f"[{idx}/{total_files}] {os.path.basename(f)}"
        bar = tqdm(unit="条", desc=desc, file=sys.stderr) if use_bar else None
        t0 = time.time()
        file_traces = runner.run_file(
            f,
            per_file_limit=getattr(args, "per_file_limit", None),
            sample_mode=getattr(args, "sample", None),
            sample_seed=getattr(args, "seed", None),
            workers=getattr(args, "workers", 1),
            progress_fn=(lambda: bar.update(1)) if bar is not None else None,
            # 多文件连续运行：不逐文件排空异步增强（推迟到全部文件后统一排空）
            drain_llm=False,
        )
        if bar is not None:
            bar.close()
        file_time = round(time.time() - t0, 2)
        traces.extend(file_traces)
        # 每文件 = 一个批次：立即打印该批次汇总 + 追加 batch 行（最终汇总按批次求和）
        runner.append_batch_row(file_time, len(file_traces), source=os.path.basename(f))
        if not args.json:
            print()
            print(format_batch_summary(
                batch_summary(file_traces, total_time_s=file_time),
                title=f"批次：{os.path.basename(f)} [{idx}/{total_files}]"))
    total_time_s = round(time.time() - t_batch, 2)
    # B4：按配置排空异步增强（默认 0 秒不等待——主流程不被 LLM 补写阻塞）
    pending = runner.wait_llm_tasks(timeout_s=runner.config.async_drain_timeout)
    if pending:
        print(f"[runner] 异步 LLM 增强：{pending} 个补写任务未完成（未等待；"
              f"对应查询的 llm_analysis 保持未完成语义，确定性产物不受影响。"
              f"如需完整补写，配置 async_drain_timeout 为等待秒数）", file=sys.stderr)
    # 关闭后台线程池（不等待）：ThreadPoolExecutor 的 atexit 钩子会在进程退出时
    # join 未完成任务——大轮次下未完成补写可积压数小时，导致汇总打印后进程"卡住"
    runner.stop_llm_executor()
    # batch 行已按文件/位置参数分组追加（不再重复追加整轮行）
    for trace in traces:
        mon = trace["monitoring"]
        # --json 模式：摘要走 stderr，stdout 只输出纯 JSON（机器可读）
        out = sys.stderr if args.json else sys.stdout
        print(f"[{trace['query_id']}] type={trace['analysis']['query_type']} | "
              f"strategy={trace['decision']['selected_strategy']['strategy_id']} | "
              f"status={mon['execution_status']} | assessment={mon['performance_assessment']} | "
              f"anomalies={len(mon['anomalies'])} | "
              f"knowledge_conf={trace['knowledge_retrieval']['knowledge_confidence']}",
              file=out)

    # ---- 批次执行汇总（P50/P95/P99 等统计只在此生成：多样本 + 真实数据）----
    from mataof.report import batch_summary, format_batch_summary
    summary = batch_summary(traces, total_time_s=total_time_s)
    if args.json:
        # stdout 纯 JSON：{traces, batch_summary}（机器可读）
        print(json.dumps({"traces": traces, "batch_summary": summary}, ensure_ascii=False))
    else:
        print()
        print(format_batch_summary(summary, title="执行汇总（本轮总体）"))
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
    p_run.add_argument("query", nargs="*", help="查询 SQL（可多条；若与 --file 混用，须放在 --file 之前）")
    p_run.add_argument("--file", nargs="+", action="append",
                       help="查询文件（.sql 按分号拆分；.jsonl 每行 {query_id, query}）；"
                            "可多个、可重复，支持 shell 通配符展开")
    p_run.add_argument("--limit", type=int, help="最多运行的查询数（位置参数截取；与 --file 联用时等价于 --per-file-limit）")
    p_run.add_argument("--per-file-limit", type=int,
                       help="每个 SQL 文件最多取多少条语句（--file 模式；默认全量）")
    p_run.add_argument("--sample", choices=["seq", "random"], default=None,
                       help="每文件取样方式：seq=顺序前 N 条（默认）/ random=随机抽样")
    p_run.add_argument("--seed", type=int, default=None,
                       help="random 抽样种子（默认 42；同种子 → 同抽样结果，可复现）")
    p_run.add_argument("--workers", type=int, default=1,
                       help="并行处理查询数（B2；>1 时查询级并发，结果按输入顺序输出）")
    p_run.add_argument("--run-id", default=None,
                       help="本轮运行标识（缺省自动按时戳生成；用于按轮次过滤汇总）")
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
