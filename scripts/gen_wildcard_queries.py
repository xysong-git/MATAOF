#!/usr/bin/env python3
"""生成 q12 通配宽扫描查询集（对齐方式杠杆的载体）。

动机（D11 实测）：alignment 杠杆（ALIGN BY DEVICE vs 默认对齐，实测 15~25×）
只对 FROM 含一级通配（如 root.<scale>.g_0.*）的查询有效；原语料 q1~q11
全部使用显式设备路径列表，该杠杆无载体。本脚本为三规模各生成 2000 条
通配查询（q12.sql），与既有语料同构（同传感器区间 s_333~s_532、同时间域、
同种子可复现）。

用法：python3 scripts/gen_wildcard_queries.py
输出：query/queries_{dbs,dbm,dbl}/q12.sql
"""

from __future__ import annotations

import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

T_START = 1640966400000        # 数据域起点（与既有语料一致）
T_END = 1640972449000          # 数据域终点
SCALES = ("dbs", "dbm", "dbl")
N = 2000
SEED = 42

TEMPLATES = (
    "SELECT s_333 FROM {db}.* WHERE time >= {t0} AND time <= {t1};",
    "SELECT s_333, s_433 FROM {db}.* WHERE time >= {t0} AND time <= {t1};",
    "SELECT count(s_333) FROM {db}.* WHERE time >= {t0} AND time <= {t1};",
)
# 注意：通配路径下部分设备的 s_333 为 BOOLEAN——数值谓词（> -5）与数值聚合
# （avg/sum）会因混合类型被引擎拒绝（701），故 q12 只用点扫/计数模板。


def generate(scale: str) -> str:
    db = f"root.{scale}.g_0"
    rng = random.Random(SEED)
    lines = [f"-- Q12 generated {N} queries using s_333~s_532（通配宽扫描）"]
    for _ in range(N):
        t0 = rng.randint(T_START, T_END - 600_000)
        span = rng.choice((300_000, 900_000))
        t1 = t0 + span
        tmpl = rng.choice(TEMPLATES)
        lines.append(tmpl.format(db=db, t0=t0, t1=t1))
    return "\n".join(lines) + "\n"


def main() -> int:
    for scale in SCALES:
        path = os.path.join(ROOT, "query", f"queries_{scale}", "q12.sql")
        with open(path, "w", encoding="utf-8") as f:
            f.write(generate(scale))
        print(f"生成 {path}（{N} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
