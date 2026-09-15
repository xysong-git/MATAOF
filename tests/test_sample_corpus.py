"""在 MAPO 前序工作的真实 IoTDB 查询语料上做鲁棒性验证。

要求（对应"无虚构、可验证"原则）：
- 任何输入都不抛异常；
- 输出始终符合 schema 契约；
- 解析失败的查询必须降级为 unknown + 低置信度，不得崩溃、不得猜测。
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.query_analysis import analyze  # noqa: E402

# MAPO 样例目录（前序工作，仅作测试语料，不属于 MATAOF 代码）
MAPO_SAMPLES = "/opt/program-code/MAPO/MAPO"
SAMPLE_DIRS = [
    "sample_queries_iotdb",
    "sample_queries_iotdbs",
    "sample_queries_iotdbm",
    "sample_queries_iotdbl",
    "sample_queries_iotdb800",
]
STATEMENTS_PER_FILE = 50  # 每文件最多验证的语句数（全量语料数万条，测试保持快速）


def _iter_statements():
    for d in SAMPLE_DIRS:
        root = os.path.join(MAPO_SAMPLES, d)
        if not os.path.isdir(root):
            continue
        for fname in sorted(os.listdir(root)):
            if not fname.endswith(".sql"):
                continue
            with open(os.path.join(root, fname), encoding="utf-8", errors="replace") as f:
                text = f.read()
            statements = [s.strip() for s in text.split(";") if s.strip() and not s.strip().startswith("--")]
            for s in statements[:STATEMENTS_PER_FILE]:
                yield fname, s


def test_corpus_robustness():
    counts = Counter()
    n = 0
    parse_failures = []
    for fname, sql in _iter_statements():
        n += 1
        r = analyze(sql, query_id=f"{fname}#{n}")
        json.dumps(r)                       # 必须可序列化
        assert 0.0 <= r["analysis_confidence"] <= 1.0
        assert "query_features" in r and "optimization_relevant_features" in r
        counts[r["query_type"]] += 1
        if any("解析失败" in u for u in r["unknown_features"]):
            parse_failures.append((fname, sql[:120]))
            assert r["query_type"] == "unknown"
            assert r["analysis_confidence"] == 0.1
    assert n > 0, "未找到样例语料（MAPO 样例目录缺失？）"
    print(f"\n语料验证完成：共 {n} 条语句（每文件 ≤{STATEMENTS_PER_FILE} 条）")
    print("类型分布：", dict(counts))
    print(f"解析失败数：{len(parse_failures)}（全部优雅降级为 unknown）")
    # 关键类型必须在语料中出现（确保测试语料真实覆盖了研究场景）
    assert counts["point_query"] > 0
    assert counts["range_query"] > 0
    assert counts["predicate_filter_query"] > 0
    assert counts["window_aggregation_query"] > 0
    assert counts["complex_composite_query"] > 0
