"""Query Analysis Agent 使用示例。

运行：python3 examples/analyze_example.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.query_analysis import analyze  # noqa: E402

EXAMPLES = [
    ("point", "SELECT s_1666 FROM root.db800.g_0.d_0 WHERE time = 1640966450000"),
    ("range", "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000"),
    (
        "predicate_filter",
        "SELECT s_1666, s_1766 FROM root.db800.g_0.d_0, root.db800.g_0.d_1 "
        "WHERE time >= 1640966400000 AND time <= 1640966650000 "
        "AND root.db800.g_0.d_0.s_1666 > -5 AND root.db800.g_0.d_0.s_1766 > -5",
    ),
    ("window_aggregation", "SELECT AVG(s_0) FROM root.test.d_0 GROUP BY ([1640966405000, 1640976405000), 1h)"),
    ("sliding_window", "SELECT AVG(s1) FROM root.sg1.d1 GROUP BY ([0, 4102444800000), 1h, 30m)"),
    ("complex_union", "SELECT s1 FROM root.sg1.d1 WHERE time >= 1 AND time <= 2 "
                      "UNION SELECT s1 FROM root.sg1.d2 WHERE time >= 3 AND time <= 4"),
    ("complex_subquery", "SELECT s1 FROM root.sg1.d1 WHERE time IN "
                         "(SELECT time FROM root.sg1.d1 WHERE s1 > 100)"),
    ("no_filter", "SELECT s1 FROM root.sg1.d1"),
]

# 带数据库统计信息的示例（选择率 + 扫描估计）
DB_STATE = {
    "statistics": {"selectivity": {"s1": 0.05, "s2": 0.4}},
    "scan_estimate": {"time_range": [1640966400000, 1640966700000], "estimated_points": 30000},
}


def main() -> None:
    for label, sql in EXAMPLES:
        result = analyze(sql, query_id=label)
        print(f"===== {label} =====")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print()

    print("===== with database_state =====")
    result = analyze(
        "SELECT s1, s2 FROM root.sg1.d1 "
        "WHERE time >= 1640966400000 AND time <= 1640966700000 AND s1 > 10 AND s2 < 50",
        query_id="with_db",
        database_state=DB_STATE,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
