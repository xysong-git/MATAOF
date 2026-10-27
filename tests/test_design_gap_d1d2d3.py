"""D1（策略评价表动态权重）/ D2（特征向量化）/ D3（固定策略对照）的单元测试。

运行：pytest tests/test_design_gap_d1d2d3.py -v
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.query_analysis import analyze  # noqa: E402
from mataof.agents.query_analysis.vectorize import (  # noqa: E402
    FEATURE_VECTOR_DIM,
    FEATURE_VECTOR_DIMS,
    encode_features,
)
from mataof.agents.optimization_decision import decide  # noqa: E402
from mataof.agents.optimization_decision.scoring import _evaluation_prior  # noqa: E402
from mataof.agents.knowledge_memory import KnowledgeMemoryAgent  # noqa: E402
from mataof.runner import PipelineRunner, RunnerConfig  # noqa: E402

NARROW_Q = ("SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 "
            "AND time <= 1640970000000")
MULTI_Q = ("SELECT s_0 FROM root.test.d_0, root.test.d_1 WHERE time >= 1640966405000 "
           "AND time <= 1640970000000")
AGG_Q = ("SELECT AVG(s_0) FROM root.test.d_0 "
         "GROUP BY ([1640966405000, 1640976405000), 1h)")


# ---------------------------------------------------------------------------
# D2：特征向量化
# ---------------------------------------------------------------------------

def test_feature_vector_contract():
    r = analyze(NARROW_Q, query_id="v1")
    fv = r["feature_vector"]
    assert len(fv["dims"]) == FEATURE_VECTOR_DIM
    assert len(fv["values"]) == FEATURE_VECTOR_DIM
    assert fv["dims"] == FEATURE_VECTOR_DIMS
    assert all(isinstance(v, float) and 0.0 <= v <= 1.0 for v in fv["values"])


def test_feature_vector_onehot_and_unknown_encoding():
    r = analyze(NARROW_Q, query_id="v1")
    fv = r["feature_vector"]
    idx = {d: i for i, d in enumerate(fv["dims"])}
    assert fv["values"][idx["query_type=range_query"]] == 1.0
    assert fv["values"][idx["range_level=narrow"]] == 1.0
    assert fv["values"][idx["has_time_filter"]] == 1.0
    # 无聚合 → 聚合相关维度为 0（unknown 位，不编造）
    assert fv["values"][idx["has_aggregation"]] == 0.0
    assert fv["values"][idx["aggregation_count"]] == 0.0
    # 跨度 3595000ms → log10 归一化 > 0
    assert fv["values"][idx["time_span_log"]] > 0.0


def test_feature_vector_unknown_query_zero():
    r = analyze("SELECT s1 FROM root.sg1.d1", query_id="u")   # 类型 unknown、无时间过滤
    fv = r["feature_vector"]
    idx = {d: i for i, d in enumerate(fv["dims"])}
    assert fv["values"][idx["query_type=unknown"]] == 1.0
    assert fv["values"][idx["time_span_log"]] == 0.0


def test_feature_vector_deterministic():
    r1 = analyze(AGG_Q, query_id="a")
    r2 = analyze(AGG_Q, query_id="b")
    assert r1["feature_vector"]["values"] == r2["feature_vector"]["values"]


# ---------------------------------------------------------------------------
# D1：策略评价表与动态权重
# ---------------------------------------------------------------------------

def _ingest(km, query_id="q1", q=NARROW_Q, metrics=None, baseline=None):
    analysis = analyze(q, query_id=query_id)
    decision = {"selected_strategy": {
        "strategy_id": "time_pruning=partition_pruning|filter_order=not_applicable|aggregation_placement=not_applicable",
        "strategy_parameters": {}},
        "decision": {"time_pruning": {"strategy": "partition_pruning"},
                     "filter_order": {"strategy": "not_applicable"},
                     "aggregation_placement": {"strategy": "not_applicable"}}}
    from mataof.agents.execution_monitoring import monitor
    feedback = monitor({
        "query_id": query_id, "strategy_id": decision["selected_strategy"]["strategy_id"],
        "execution_status": "success",
        "metrics": metrics or {"response_time_ms": 10.0},
        "baseline_metrics": baseline or {"response_time_ms": 50.0},
    })
    return km.update({"query_id": query_id, "query": q, "query_analysis": analysis,
                      "decision": decision, "monitoring": feedback})


def test_evaluation_table_accumulates():
    km = KnowledgeMemoryAgent()
    r = _ingest(km, query_id="q1")
    key = "range_query|time_pruning|partition_pruning"
    evals = km.store.evaluations()
    assert evals[key]["count"] == 1 and evals[key]["success"] == 1
    assert evals[key]["success_ratio"] == 1.0
    # update 输出包含评价表增量
    assert key in r["strategy_evaluation_updates"]


def test_evaluation_table_persists(tmp_path):
    path = str(tmp_path / "k.json")
    km1 = KnowledgeMemoryAgent(store_path=path)
    _ingest(km1, query_id="q1")
    km2 = KnowledgeMemoryAgent(store_path=path)
    assert km2.store.evaluations()["range_query|time_pruning|partition_pruning"]["count"] == 1


def test_retrieve_outputs_strategy_evaluations():
    km = KnowledgeMemoryAgent()
    _ingest(km, query_id="q1")
    r = km.retrieve(analyze(NARROW_Q, query_id="new"))
    assert "range_query|time_pruning|partition_pruning" in r["strategy_evaluations"]


def test_evaluation_prior_function():
    evals = {"range_query|time_pruning|partition_pruning":
             {"count": 5, "success": 5, "success_ratio": 1.0,
              "avg_latency_ms": 10.0}}
    class _Ctx:
        query_type = "range_query"
    labels = []
    assert _evaluation_prior(_Ctx(), evals, "time_pruning", "partition_pruning",
                             labels, strength=0.10, min_count=3) == 0.10
    # 全部失败 → 负先验
    evals["range_query|time_pruning|partition_pruning"] = {
        "count": 5, "success": 0, "success_ratio": 0.0, "avg_latency_ms": 90.0}
    assert _evaluation_prior(_Ctx(), evals, "time_pruning", "partition_pruning",
                             [], strength=0.10, min_count=3) == -0.10
    # 样本不足 → 无先验（证据不足不虚增）
    evals["range_query|time_pruning|partition_pruning"]["count"] = 2
    assert _evaluation_prior(_Ctx(), evals, "time_pruning", "partition_pruning",
                             [], strength=0.10, min_count=3) == 0.0


def test_decision_uses_evaluation_prior():
    # 评价表先验（D1）：同维度两策略的成功率差异 → 先验支撑选择
    evals = {"range_query|alignment|align_by_device":
             {"count": 10, "success": 9, "success_ratio": 0.9, "avg_latency_ms": 10.0},
             "range_query|alignment|time_aligned":
             {"count": 10, "success": 1, "success_ratio": 0.1, "avg_latency_ms": 80.0}}
    analysis = analyze(MULTI_Q, query_id="q1")
    r = decide({"query_analysis": analysis, "strategy_evaluations": evals})
    assert r["decision"]["alignment"]["strategy"] == "align_by_device"
    assert "策略评价表先验" in r["decision"]["alignment"]["reason"]


def test_evaluation_prior_cannot_override_requirements():
    # 需求不满足的候选（单设备查询选对齐策略）即使先验极好也不可选
    evals = {"range_query|alignment|align_by_device":
             {"count": 50, "success": 50, "success_ratio": 1.0, "avg_latency_ms": 5.0}}
    analysis = analyze(NARROW_Q, query_id="q1")
    r = decide({"query_analysis": analysis, "strategy_evaluations": evals})
    assert r["decision"]["alignment"]["strategy"] == "not_applicable"


# ---------------------------------------------------------------------------
# D3：固定策略对照（Baseline 2）
# ---------------------------------------------------------------------------

def test_fixed_strategy_mode(tmp_path):
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path),
        fixed_strategy={"alignment": "time_aligned", "fetch_size": "fetch_default"},
    ))
    t = runner.run_query(NARROW_Q, query_id="f1")
    d = t["decision"]
    assert d["decision_status"] == "fixed_strategy"
    assert d["decision"]["alignment"]["strategy"] == "not_applicable"  # 单设备
    assert d["decision"]["fetch_size"]["strategy"] == "fetch_default"
    assert d["selected_strategy"]["strategy_id"] == \
        "alignment=not_applicable|fetch_size=fetch_default"
    # 不使用历史与决策证据
    assert t["knowledge_retrieval"]["matched_count"] == 0
    # 执行/监控/入库链路照常（口径一致）
    assert "execution_status" in t["monitoring"]
    assert t["knowledge_update"]["stored"] is True


def test_fixed_strategy_applies_to_multi_device(tmp_path):
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path),
        fixed_strategy={"alignment": "time_aligned", "fetch_size": "fetch_default"},
    ))
    t = runner.run_query(MULTI_Q, query_id="f2")
    d = t["decision"]
    assert d["decision"]["alignment"]["strategy"] == "time_aligned"
    assert d["decision_status"] == "fixed_strategy"
