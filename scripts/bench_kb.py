#!/usr/bin/env python3
"""知识库持久化层基准（D8b 第四/五步）：多规模点 × 三后端对比。

后端：json（整库快照，现状）/ jsonl（追加式 JSON Lines）/ sqlite（增量 INSERT）。
三者同协议、同内存结构、同检索语义（等价性由测试保证），对比维度：
加载时间、每查询检索延迟（分阶段，快/参考双路径）、写入摊销（add/flush）、
内存 RSS、文件大小。

用法：
  python3 scripts/bench_kb.py --kb data/knowledge_dbs_llm.json \
      [--backends json,jsonl,sqlite] [--scales 1000,10000,29000,51986] \
      [--outdir DIR] [--reps 2]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 代表查询：覆盖点查/聚合窗口/多传感器过滤/last/计数（与真实查询集同源）
BENCH_QUERIES = [
    "SELECT s_0 FROM root.dbs.g_0.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000",
    "SELECT avg(s_0) FROM root.dbm.g_0.d_1 WHERE time >= 1640966400000 AND time <= 1644566405000 GROUP BY time(1h)",
    "SELECT s_1, s_2 FROM root.dbl.g_0.d_2 WHERE time >= 1640966400000 AND time <= 1644566405000 AND s_1 > -5",
    "SELECT last(s_0) FROM root.dbs.g_0.d_3",
    "SELECT count(*) FROM root.dbm.g_1.d_4 WHERE time >= 1640966400000 AND time <= 1650000000000 AND s_2 < 10",
]
DB_STATE = {"device_scale": 1000, "data_scale_level": "small"}
PHASES = ("matrix_ms", "scan_ms", "argsort_ms", "match_loop_ms", "sort_ms",
          "serialize_ms", "aggregate_ms", "evidence_ms", "evaluations_ms",
          "total_ms")


def _store_cls(backend: str):
    from mataof.agents.knowledge_memory.store import MemoryStore
    if backend == "jsonl":
        from mataof.agents.knowledge_memory.jsonl_store import JSONLStore
        return JSONLStore
    if backend == "sqlite":
        from mataof.agents.knowledge_memory.sqlite_store import SQLiteStore
        return SQLiteStore
    return MemoryStore


def _rss_mb() -> float:
    """当前进程 RSS（MB，Linux /proc/self/status）。"""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return float("nan")


def _size_mb(path: str, backend: str) -> float:
    total = os.path.getsize(path) if os.path.exists(path) else 0.0
    if backend == "jsonl" and os.path.exists(f"{path}.records"):
        total += os.path.getsize(f"{path}.records")
    return round(total / 1e6, 1)


def build_subset(src_path: str, out_dir: str, n: int) -> str:
    """从全量库按记录顺序切前 n 条，写 JSON 源子集文件并预热（派生表重建后落盘）。"""
    with open(src_path, encoding="utf-8") as f:
        data = json.load(f)
    records = data.get("records") or {}
    sub = dict(list(records.items())[:n])
    path = os.path.join(out_dir, f"src_{n}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "next_seq": n + 1, "records": sub}, f,
                  ensure_ascii=False, separators=(",", ":"))
    # 预热：触发派生表重建并保存（此后源文件含 evaluations/strategy_stats）
    from mataof.agents.knowledge_memory.store import MemoryStore
    MemoryStore(path, save_interval=1).flush()
    return path


def build_native(src_json: str, out_dir: str, n: int, backend: str) -> str:
    """把 JSON 源子集灌入指定后端，返回其原生存储路径（setup，不计入对比）。

    灌入时同步聚合评价表（与真实 km.update 流程一致），避免加载时的
    派生表重建干扰加载时间测量。
    """
    from mataof.agents.knowledge_memory.store import MemoryStore
    cls = _store_cls(backend)
    src = MemoryStore(src_json, save_interval=1)
    path = os.path.join(out_dir, f"kb_{n}.{backend}")
    if os.path.exists(path):
        import shutil
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    for extra in (f"{path}.records",):
        if os.path.exists(extra):
            os.remove(extra)
    dst = cls(path, save_interval=5000)
    for rec in src.all():
        dst.add(rec)
        dst.record_evaluation(rec)   # 与真实入库流程一致（评价表随记录聚合）
    dst._next_seq = len(src.all()) + 1   # 批量灌入后修正序号（bench 内部使用）
    dst.flush()
    if hasattr(dst, "close"):
        dst.close()
    return path


def _time_retrieves(store, queries, reps, scalar: bool) -> dict:
    """跑 reps×queries 次检索，返回各阶段平均 ms。scalar=True → 强制参考路径。

    计时前预热一次（构建矩阵/投影行缓存），测稳态检索延迟。
    """
    import builtins
    from mataof.agents.query_analysis import analyze
    from mataof.agents.knowledge_memory.retrieval import retrieve

    real_import = builtins.__import__

    def _no_numpy(name, *a, **k):
        if name == "numpy":
            raise ImportError("bench: numpy disabled")
        return real_import(name, *a, **k)

    if scalar:
        builtins.__import__ = _no_numpy
    try:
        acc = {p: [] for p in PHASES}
        for rep in range(reps):
            for i, q in enumerate(queries):
                an = analyze(q, query_id=f"b{rep}-{i}")
                timing: dict = {}
                retrieve(store, an, database_state=DB_STATE, system_state=None,
                         query_id=f"b{rep}-{i}", max_records=100, timing_out=timing)
                if rep == 0 and i == 0:
                    continue   # 预热：矩阵/投影行缓存的冷构建不计入稳态
                for p in PHASES:
                    acc[p].append(timing.get(p, 0.0))
        return {p: round(sum(v) / len(v), 2) for p, v in acc.items()}
    finally:
        if scalar:
            builtins.__import__ = real_import


def _cold_first_ms(store, queries) -> float:
    """首次检索墙钟（含矩阵 + 投影行冷构建），冷启动口径。"""
    from mataof.agents.query_analysis import analyze
    from mataof.agents.knowledge_memory.retrieval import retrieve
    an = analyze(queries[0], query_id="cold")
    t0 = time.perf_counter()
    retrieve(store, an, database_state=DB_STATE, system_state=None,
             query_id="cold", max_records=100)
    return round((time.perf_counter() - t0) * 1000.0, 2)


def _time_add(store, n: int, save_interval: int) -> float:
    """追加 n 条占位记录，返回单条摊销 ms（含落盘）。"""
    store.save_interval = save_interval
    t0 = time.perf_counter()
    for i in range(n):
        store.add({"query_id": f"bench-{i}", "strategy": {"strategy_id": "bench"},
                   "execution_result": "unchanged",
                   "execution": {"metrics": {"response_time_ms": 1.0}}})
    store.flush()
    return round((time.perf_counter() - t0) * 1000.0 / n, 2)


def run_scale(src_path: str, out_dir: str, n: int, queries: list,
              reps: int, backend: str) -> dict:
    cls = _store_cls(backend)
    subset = build_subset(src_path, out_dir, n)
    native = build_native(subset, out_dir, n, backend)

    rss0 = _rss_mb()
    t0 = time.perf_counter()
    store = cls(native, save_interval=1)
    load_ms = (time.perf_counter() - t0) * 1000.0
    rss = _rss_mb() - rss0          # 本 store 自身的内存增量（进程累计值不可比）
    cold = _cold_first_ms(store, queries)

    fast = _time_retrieves(store, queries, reps, scalar=False)
    ref = _time_retrieves(store, queries, reps, scalar=True)
    add1 = _time_add(store, 10, save_interval=1)
    add20 = _time_add(store, 10, save_interval=20)
    if hasattr(store, "close"):
        store.close()

    return {
        "scale": store.count() - 20,   # 减去基准追加的占位记录
        "file_mb": _size_mb(native, backend),
        "load_ms": round(load_ms, 1),
        "rss_mb": round(rss, 1),
        "cold_first_ms": cold,
        "retrieve_fast_ms": fast["total_ms"],
        "retrieve_ref_ms": ref["total_ms"],
        "add_ms_interval1": add1,
        "add_ms_interval20": add20,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kb", required=True, help="全量知识库 JSON（子集按其切出）")
    p.add_argument("--backends", default="json,jsonl,sqlite")
    p.add_argument("--scales", default="1000,10000,29000,51986")
    p.add_argument("--outdir", default=None, help="子集输出目录（默认 <kb 同目录>/bench_kb_tmp）")
    p.add_argument("--reps", type=int, default=2, help="每查询重复次数（默认 2）")
    args = p.parse_args(argv)

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    scales = [int(s) for s in args.scales.split(",") if s.strip()]
    out_dir = args.outdir or os.path.join(os.path.dirname(os.path.abspath(args.kb)),
                                          "bench_kb_tmp")
    os.makedirs(out_dir, exist_ok=True)

    results = {b: [] for b in backends}
    for backend in backends:
        print(f"\n===== 后端: {backend} =====")
        print(f"{'规模':>8} | {'文件MB':>7} | {'加载ms':>8} | {'冷首次ms':>9} | "
              f"{'RSS MB':>7} | {'检索·快ms':>9} | {'检索·参考ms':>11} | {'加速比':>6} | "
              f"{'add ms(si=1)':>11} | {'add ms(si=20)':>13}")
        print("-" * 115)
        for n in scales:
            r = run_scale(args.kb, out_dir, n, BENCH_QUERIES, args.reps, backend)
            results[backend].append(r)
            speedup = (r["retrieve_ref_ms"] / r["retrieve_fast_ms"]
                       if r["retrieve_fast_ms"] else 0)
            print(f"{r['scale']:>8} | {r['file_mb']:>7} | {r['load_ms']:>8.1f} | "
                  f"{r['cold_first_ms']:>9.1f} | {r['rss_mb']:>7.1f} | "
                  f"{r['retrieve_fast_ms']:>9.2f} | "
                  f"{r['retrieve_ref_ms']:>11.2f} | {speedup:>5.1f}x | "
                  f"{r['add_ms_interval1']:>11.2f} | {r['add_ms_interval20']:>13.2f}")

    # 汇总对比（最大规模点）
    if len(backends) > 1:
        print(f"\n===== 最大规模横向对比（{max(scales)} 条）=====")
        print(f"{'后端':>8} | {'加载ms':>9} | {'检索快ms':>9} | {'RSS MB':>8} | "
              f"{'add(si=1)':>10} | {'add(si=20)':>11} | {'文件MB':>7}")
        for backend in backends:
            r = results[backend][-1]
            print(f"{backend:>8} | {r['load_ms']:>9.1f} | {r['retrieve_fast_ms']:>9.2f} | "
                  f"{r['rss_mb']:>8.1f} | {r['add_ms_interval1']:>10.2f} | "
                  f"{r['add_ms_interval20']:>11.2f} | {r['file_mb']:>7}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
