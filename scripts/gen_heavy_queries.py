#!/usr/bin/env python3
"""把语料改写为**重查询**（保持每文件类型结构，时间维度拉满）。

动机：原语料执行延迟 3~7ms，与优化开销（5~18ms）同量级，端到端无法体现
优化收益（break-even 分析）。改写原则：
- 时间窗口：25min/5min → **全数据域**（~1.7h，带小抖动保多样性）；
- 点查/LAST：改为**全设备**（g_0.*）形式（实测 22~48ms）；
- 保持每文件原有类型与谓词结构（点查/范围/过滤/计数/求和/窗口聚合/LAST）。

标定（dbl 实测）：全设备全程 107ms、无过滤 267ms、窗口 count 43ms、
LAST 全设备 48ms——执行时间 >> 决策开销，break-even 成立。

用法：python3 scripts/gen_heavy_queries.py
输出：重写 query/queries_{dbs,dbm,dbl}/q1~q12.sql（q12 同为本重查询格式）
"""

from __future__ import annotations

import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

T0 = 1640966400000
T1 = 1640972449000        # 数据域终点
SCALES = ("dbs", "dbm", "dbl")
N = 2000
SEED = 42
JITTER = 60_000           # 窗口边界抖动（毫秒）

# 数值传感器按规模不同（原语料实测）：dbs 用 s_333~；dbm/dbl 用 s_1666+
SENSORS = {
    "dbs": {"a": "s_333", "b": "s_334", "c": "s_433", "d": "s_434"},
    "dbm": {"a": "s_1666", "b": "s_1667", "c": "s_1766", "d": "s_1717"},
    "dbl": {"a": "s_1666", "b": "s_1667", "c": "s_1716", "d": "s_1717"},
}


def _pair(rng: random.Random):
    i = rng.randint(0, 98)
    return f"d_{i}", f"d_{i + 1}"


def _t0t1(rng: random.Random):
    t0 = T0 + rng.randint(0, JITTER)
    t1 = T1 - rng.randint(0, JITTER)
    return t0, t1


def _s(scale: str, key: str) -> str:
    return SENSORS[scale][key]


def q1(scale: str, rng: random.Random) -> str:
    """点查（全设备）。"""
    t = T0 + rng.randint(0, T1 - T0)
    return f"SELECT {_s(scale, 'a')} FROM root.{scale}.g_0.* WHERE time = {t};"


def q2(scale: str, rng: random.Random) -> str:
    """范围查询（双设备全程）。"""
    d0, d1 = _pair(rng)
    t0, t1 = _t0t1(rng)
    return (f"SELECT {_s(scale, 'a')}, {_s(scale, 'c')} FROM root.{scale}.g_0.{d0}, "
            f"root.{scale}.g_0.{d1} WHERE time >= {t0} AND time <= {t1};")


def q3(scale: str, rng: random.Random) -> str:
    """范围 + 非时间谓词（双设备全程）。"""
    d0, d1 = _pair(rng)
    t0, t1 = _t0t1(rng)
    return (f"SELECT {_s(scale, 'a')}, {_s(scale, 'c')} FROM root.{scale}.g_0.{d0}, "
            f"root.{scale}.g_0.{d1} WHERE time >= {t0} AND time <= {t1} "
            f"AND root.{scale}.g_0.{d0}.{_s(scale, 'a')} > -5 "
            f"AND root.{scale}.g_0.{d1}.{_s(scale, 'c')} > -5;")


def q4(scale: str, rng: random.Random) -> str:
    """计数聚合（双设备全程）。"""
    d0, d1 = _pair(rng)
    t0, t1 = _t0t1(rng)
    return (f"SELECT count({_s(scale, 'a')}), count({_s(scale, 'c')}) "
            f"FROM root.{scale}.g_0.{d0}, root.{scale}.g_0.{d1} "
            f"WHERE time >= {t0} AND time <= {t1};")


def q5(scale: str, rng: random.Random) -> str:
    """求和聚合（双设备全程）。"""
    d0, d1 = _pair(rng)
    t0, t1 = _t0t1(rng)
    return (f"SELECT sum({_s(scale, 'b')}), sum({_s(scale, 'd')}) "
            f"FROM root.{scale}.g_0.{d0}, root.{scale}.g_0.{d1} "
            f"WHERE time >= {t0} AND time <= {t1};")


def q6(scale: str, rng: random.Random) -> str:
    """计数 + 非时间谓词（双设备全程）。"""
    d0, d1 = _pair(rng)
    t0, t1 = _t0t1(rng)
    return (f"SELECT count({_s(scale, 'a')}), count({_s(scale, 'c')}) "
            f"FROM root.{scale}.g_0.{d0}, root.{scale}.g_0.{d1} "
            f"WHERE time >= {t0} AND time <= {t1} "
            f"AND root.{scale}.g_0.{d0}.{_s(scale, 'a')} > -5 "
            f"AND root.{scale}.g_0.{d1}.{_s(scale, 'c')} > -5;")


def q7(scale: str, rng: random.Random) -> str:
    """窗口计数（双设备全程 20s 窗）。"""
    d0, d1 = _pair(rng)
    return (f"SELECT count({_s(scale, 'a')}), count({_s(scale, 'c')}) "
            f"FROM root.{scale}.g_0.{d0}, root.{scale}.g_0.{d1} "
            f"GROUP BY ([{T0}, {T1}), 20000ms);")


def q8(scale: str, rng: random.Random) -> str:
    """LAST（全设备）。"""
    return f"SELECT LAST {_s(scale, 'a')}, {_s(scale, 'c')} FROM root.{scale}.g_0.*;"


def q9(scale: str, rng: random.Random) -> str:
    """范围 + ORDER BY（双设备全程）。"""
    d0, d1 = _pair(rng)
    t0, t1 = _t0t1(rng)
    return (f"SELECT {_s(scale, 'a')}, {_s(scale, 'c')} FROM root.{scale}.g_0.{d0}, "
            f"root.{scale}.g_0.{d1} WHERE time >= {t0} AND time <= {t1} "
            f"ORDER BY time DESC;")


def q10(scale: str, rng: random.Random) -> str:
    """范围 + 谓词（传感器变体）。"""
    d0, d1 = _pair(rng)
    t0, t1 = _t0t1(rng)
    return (f"SELECT {_s(scale, 'b')}, {_s(scale, 'd')} FROM root.{scale}.g_0.{d0}, "
            f"root.{scale}.g_0.{d1} WHERE time >= {t0} AND time <= {t1} "
            f"AND root.{scale}.g_0.{d0}.{_s(scale, 'b')} > -4 "
            f"AND root.{scale}.g_0.{d1}.{_s(scale, 'd')} > -4;")


def q11(scale: str, rng: random.Random) -> str:
    """窗口求和（双设备全程 10s 窗）。"""
    d0, d1 = _pair(rng)
    return (f"SELECT sum({_s(scale, 'b')}), sum({_s(scale, 'd')}) "
            f"FROM root.{scale}.g_0.{d0}, root.{scale}.g_0.{d1} "
            f"GROUP BY ([{T0}, {T1}), 10000ms);")


def q12(scale: str, rng: random.Random) -> str:
    """通配宽扫描（全设备全程）。"""
    t0, t1 = _t0t1(rng)
    return (f"SELECT {_s(scale, 'a')} FROM root.{scale}.g_0.* "
            f"WHERE time >= {t0} AND time <= {t1};")


GENERATORS = {f"q{i}": globals()[f"q{i}"] for i in range(1, 13)}


def generate_file(scale: str, qname: str) -> str:
    rng = random.Random(SEED)
    gen = GENERATORS[qname]
    lines = [f"-- {qname.upper()} generated {N} queries using s_333~s_532（重查询改写：全数据域/全设备）"]
    for _ in range(N):
        lines.append(gen(scale, rng))
    return "\n".join(lines) + "\n"


def main() -> int:
    for scale in SCALES:
        for i in range(1, 13):
            qname = f"q{i}"
            path = os.path.join(ROOT, "query", f"queries_{scale}", f"{qname}.sql")
            with open(path, "w", encoding="utf-8") as f:
                f.write(generate_file(scale, qname))
            print(f"重写 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
