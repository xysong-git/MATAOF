#!/usr/bin/env python3
"""SQL 语义杠杆差异实测（native 直发，无任何优化系统）。

目的：验证"同一逻辑查询的不同写法"在 IoTDB 上的执行差异是否真实存在——
这是策略空间重设计（数据库无关的 SQL 语义杠杆）的实证基础。

方法：每组杠杆准备若干写法变体，随机交错各跑 N 遍（交错 A/B 抵消缓存漂移），
统计 avg/P50/P95 与结果行数。纯客户端墙钟（与实验口径一致）。

用法：python3 scripts/bench_sql_levers.py [--scale dbs|dbm|dbl] [--reps 3]
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.executors.iotdb import IoTDBExecutor  # noqa: E402


# 每个杠杆的变体：名称 → SQL 模板（{scale} 填 root.dbs.g_0 / root.dbm.g_0 / root.dbl.g_0）
LEVERS = {
    # 1) 多设备对齐方式：默认时间对齐 vs ALIGN BY DEVICE（执行路径真实不同）
    "align": {
        "默认时间对齐": ("SELECT s_0 FROM {db}.* WHERE time >= 1640966400000 "
                     "AND time <= 1640970000000", True),
        "ALIGN BY DEVICE": ("SELECT s_0 FROM {db}.* WHERE time >= 1640966400000 "
                        "AND time <= 1640970000000 ALIGN BY DEVICE", True),
    },
    # 2) 窗口聚合：显式 session 窗口 vs 全局窗口（两阶段聚合 vs 单遍，执行计划不同）
    #    注：s_0~s_100 为 BOOLEAN，聚合需数值传感器（查询集同款 s_333/s_334）
    "groupby": {
        "session 窗口": ("SELECT avg(s_333) FROM {db}.d_0 WHERE time >= 1640966400000 "
                     "AND time <= 1644566405000 GROUP BY ([1640966400000, 1644566405000), 1h)",
                     True),
        "全局窗口": ("SELECT avg(s_333) FROM {db}.d_0 WHERE time >= 1640966400000 "
                 "AND time <= 1644566405000 GROUP BY TIME(1h)", True),
    },
    # 3) 滑窗 vs 固定窗
    "sliding": {
        "固定 1h 窗": ("SELECT avg(s_333) FROM {db}.d_0 WHERE time >= 1640966400000 "
                   "AND time <= 1644566405000 GROUP BY TIME(1h)", True),
        "滑窗 1h/30m": ("SELECT avg(s_333) FROM {db}.d_0 WHERE time >= 1640966400000 "
                    "AND time <= 1644566405000 GROUP BY TIME(1h, 30m)", True),
    },
    # 4) NULL 过滤：结果体量与传输差异（注意：结果语义不同——去掉空值行）
    "nulls": {
        "普通": ("SELECT avg(s_333) FROM {db}.d_0 WHERE time >= 1640966400000 "
              "AND time <= 1644566405000 GROUP BY TIME(1h)", True),
        "WITHOUT NULL": ("SELECT avg(s_333) FROM {db}.d_0 WHERE time >= 1640966400000 "
                     "AND time <= 1644566405000 GROUP BY TIME(1h) WITHOUT NULL", True),
    },
    # 5) last 通道 vs 普通点查（最近点查询）
    "last": {
        "普通点查": ("SELECT s_0 FROM {db}.d_0 WHERE time >= 1640966400000 "
                 "AND time <= 1640970000000", True),
        "LAST 查询": ("SELECT LAST s_0 FROM {db}.d_0", True),
    },
    # 6) 谓词结构：BETWEEN vs 显式范围（优化器归一化验证——预期无差异的对照组）
    "predicate_form": {
        "显式范围": ("SELECT s_0 FROM {db}.d_0 WHERE time >= 1640966400000 "
                 "AND time <= 1640970000000", True),
        "BETWEEN 写法": ("SELECT s_0 FROM {db}.d_0 WHERE time BETWEEN 1640966400000 "
                     "AND 1640970000000", True),
    },
}


def run_once(executor, sql: str, name: str):
    r = executor.execute(sql, name, {})
    return r.metrics.get("response_time_ms"), r.execution_status


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scale", choices=["dbs", "dbm", "dbl"], default="dbs")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--levers", default="",
                   help="只测指定杠杆（逗号分隔，默认全部）")
    args = p.parse_args(argv)

    db = f"root.{args.scale}.g_0"
    want = {x.strip() for x in args.levers.split(",") if x.strip()}
    executor = IoTDBExecutor(measure_baseline=False)
    try:
        for lever, variants in LEVERS.items():
            if want and lever not in want:
                continue
            results = {name: [] for name in variants}
            rows = {name: None for name in variants}
            plan = [name for name in variants for _ in range(args.reps)]
            random.Random(42).shuffle(plan)      # 交错随机：抵消缓存漂移
            for name in plan:
                sql = variants[name][0].format(db=db)
                lat, status = run_once(executor, sql, f"{lever}:{name}")
                if status == "success" and isinstance(lat, (int, float)):
                    results[name].append(lat)
                else:
                    print(f"  ! {lever}/{name} 执行失败: {status}")
            print(f"== {lever}（{db}）==")
            for name, lats in results.items():
                if not lats:
                    print(f"  {name}: 无成功样本")
                    continue
                s = sorted(lats)
                print(f"  {name:<16} avg {sum(s)/len(s):7.2f}ms | "
                      f"P50 {statistics.median(s):7.2f} | P95 {s[int(len(s)*0.95)-1]:7.2f} | "
                      f"样本 {len(s)}")
    finally:
        executor.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
