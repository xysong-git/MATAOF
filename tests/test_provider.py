"""候选提供方（provider.py）单元测试。

覆盖：align_by_device 只在引擎可接受（FROM 含一级通配）时提供并携带 SQL；
显式设备列表不提供该候选（引擎会拒绝 ALIGN BY DEVICE——实测 701）；
fetch_large 携带执行参数；外部配置候选优先。
运行：pytest tests/test_provider.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.agents.query_analysis import analyze  # noqa: E402
from mataof.agents.optimization_decision.provider import build_candidates  # noqa: E402


def _names(candidates, dim):
    return [e["strategy"] for e in candidates[dim]]


def test_align_by_device_not_offered_for_explicit_device_list():
    # 显式设备路径列表：引擎拒绝 ALIGN BY DEVICE（实测 701）→ 只提供 time_aligned
    q = ("SELECT s_0 FROM root.test.d_0, root.test.d_1 "
         "WHERE time >= 1 AND time <= 2")
    an = analyze(q, query_id="x")
    c = build_candidates(q, an)
    assert _names(c, "alignment") == ["time_aligned"]


def test_align_by_device_offered_for_wildcard_path():
    # 一级通配路径：引擎接受 ALIGN BY DEVICE → 提供且携带改写 SQL
    q = "SELECT s_0 FROM root.test.g_0.* WHERE time >= 1 AND time <= 2"
    an = analyze(q, query_id="y")
    c = build_candidates(q, an)
    assert "align_by_device" in _names(c, "alignment")
    ab = next(e for e in c["alignment"] if e["strategy"] == "align_by_device")
    assert ab["equivalent_sql"] == q + " ALIGN BY DEVICE"


def test_single_device_no_align_candidate_sql():
    # 单设备：time_aligned 不携带 SQL（数据库默认）
    q = "SELECT s_0 FROM root.test.d_0 WHERE time >= 1 AND time <= 2"
    an = analyze(q, query_id="z")
    c = build_candidates(q, an)
    assert _names(c, "alignment") == ["time_aligned"]
    assert c["alignment"][0]["equivalent_sql"] is None


def test_fetch_large_carries_parameter():
    q = "SELECT s_0 FROM root.test.d_0 WHERE time >= 1 AND time <= 2"
    an = analyze(q, query_id="f")
    c = build_candidates(q, an)
    large = next(e for e in c["fetch_size"] if e["strategy"] == "fetch_large")
    assert large["parameters"] == {"fetch_size": 10000}


def test_configured_candidates_take_precedence():
    configured = {"alignment": [], "fetch_size": [{"strategy": "fetch_default"}]}
    q = "SELECT s_0 FROM root.test.d_0 WHERE time >= 1 AND time <= 2"
    an = analyze(q, query_id="p")
    assert build_candidates(q, an, configured) is configured
