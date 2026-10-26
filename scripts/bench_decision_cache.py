#!/usr/bin/env python3
"""LLM 决策特征化 prompt 的唯一数统计（decision_mode 全量可行性验证）。

统计 33 文件查询集上决策 prompt 的唯一数（空经验摘要 = 轮起始状态）。
唯一数 × 轮内摘要分桶状态数 ≈ 首轮冷 LLM 调用上界——决定全量实验可行性
（特征化 prompt + 输出缓存：同质查询命中缓存，实际 LLM 调用有界）。

用法：
  python3 scripts/bench_decision_cache.py [--per-file-limit N] [--files q1,q4]
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.query_analysis import analyze  # noqa: E402
from mataof.agents.optimization_decision.context import normalize_input  # noqa: E402
from mataof.agents.optimization_decision.llm_enhance import (  # noqa: E402
    build_decision_prompt,
)
from mataof.runner import split_sql_statements  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--per-file-limit", type=int, default=500,
                   help="每文件取样条数（默认 500；文件内查询同质，特征组合会饱和）")
    p.add_argument("--files", default="",
                   help="只统计指定文件（逗号分隔 q 名，默认全部 33 个）")
    args = p.parse_args(argv)

    want = {f"q{x.strip()}" for x in args.files.split(",") if x.strip()}
    files = sorted(glob.glob("query/queries_*/q*.sql"))
    prompts: set = set()
    by_scale: dict = {}
    total = 0
    for f in files:
        name = os.path.basename(f).removesuffix(".sql")
        if want and name not in want:
            continue
        scale = re.search(r"queries_(\w+)", f)
        scale = scale.group(1) if scale else "unknown"
        db_state = {"data_scale_level": {"dbs": "small", "dbm": "medium",
                                         "dbl": "large"}.get(scale, "unknown")}
        stmts = split_sql_statements(open(f, encoding="utf-8").read())
        if args.per_file_limit:
            stmts = stmts[: args.per_file_limit]
        n_file = 0
        for q in stmts:
            an = analyze(q, query_id="cache-check")
            decision_input = {
                "query_analysis": an,
                "database_state": db_state,
                "system_state": None,
                "candidate_strategies": None,
            }
            ctx = normalize_input(decision_input)
            prompt = build_decision_prompt(decision_input, ctx.candidates)
            prompts.add(prompt)
            by_scale[scale] = by_scale.get(scale, set()) | {prompt}
            n_file += 1
        total += n_file
        print(f"{scale}/{name}: {n_file} 条（累计唯一 prompt {len(prompts)}）")

    print(f"\n查询总数（抽样）: {total}")
    print(f"轮起始唯一决策 prompt: {len(prompts)}")
    for scale, s in sorted(by_scale.items()):
        print(f"  {scale}: {len(s)} 个唯一 prompt")
    print("\n解读：")
    print("- 轮起始空经验下唯一 prompt 数 = 冷 LLM 调用的下界（每轮开头各自调用一次）")
    print("- 轮内经验增长使摘要分桶变化 → prompt 变化次数有界（count 7 档 × "
          "ratio 20 档 × latency 7 档），实际冷调用 = 唯一 prompt × 分桶状态数，"
          "预计数百~千次，全量实验可行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
