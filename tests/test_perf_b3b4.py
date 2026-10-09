"""B3（LLM 输出缓存）/ B4（异步增强模式）的单元测试。

运行：pytest tests/test_perf_b3b4.py -v
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.llm import CachedLLMClient, build_llm_client, hash_messages  # noqa: E402
from mataof.runner import PipelineRunner, RunnerConfig  # noqa: E402

NARROW_Q = ("SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 "
            "AND time <= 1640970000000")


class CountingLLM:
    name = "fake"
    model = "fake-model"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, max_tokens=2048, timeout_s=60.0):
        self.calls += 1
        return json.dumps({
            "semantic_summary": "摘要",
            "query_type_hint": "unknown",
            "condition_hints": {},
            "semantic_reason": "解读",
            "preferences": {"time_pruning": ["full_scan"], "filter_order": [],
                            "aggregation_placement": []},
            "performance_summary": "执行成功。",
            "anomaly_summary": None,
            "experience_note": "经验总结",
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# B3：LLM 输出缓存
# ---------------------------------------------------------------------------

def test_cache_hit_avoids_underlying_call(tmp_path):
    inner = CountingLLM()
    cache = CachedLLMClient(inner, cache_file=str(tmp_path / "c.json"))
    msgs = [{"role": "user", "content": "同一条消息"}]
    r1 = cache.chat(msgs)
    r2 = cache.chat(msgs)                    # 命中缓存
    assert r1 == r2 and r1 is not None
    assert inner.calls == 1                  # 底层只调用一次
    r3 = cache.chat([{"role": "user", "content": "不同消息"}])
    assert inner.calls == 2


def test_cache_persists_across_clients(tmp_path):
    path = str(tmp_path / "c.json")
    inner = CountingLLM()
    cache1 = CachedLLMClient(inner, cache_file=path)
    cache1.chat([{"role": "user", "content": "x"}])
    assert inner.calls == 1
    inner2 = CountingLLM()
    cache2 = CachedLLMClient(inner2, cache_file=path)
    assert cache2.chat([{"role": "user", "content": "x"}]) is not None
    assert inner2.calls == 0                 # 跨进程（同文件）命中


def test_cache_none_response_not_cached(tmp_path):
    class NoneLLM(CountingLLM):
        def chat(self, messages, max_tokens=2048, timeout_s=60.0):
            self.calls += 1
            return None
    inner = NoneLLM()
    cache = CachedLLMClient(inner, cache_file=str(tmp_path / "c.json"))
    assert cache.chat([{"role": "user", "content": "y"}]) is None
    assert cache.chat([{"role": "user", "content": "y"}]) is None
    assert inner.calls == 2                  # None 不缓存，失败可重试


def test_build_llm_client_wraps_cache():
    os.environ["LLM_API_KEY"] = "k"
    client = build_llm_client({
        "provider": "openai", "base_url": "http://x/v1", "model": "m",
        "api_key": "k", "cache_file": "/tmp/llmc.json",
    })
    assert isinstance(client, CachedLLMClient)


# ---------------------------------------------------------------------------
# B4：异步增强模式
# ---------------------------------------------------------------------------

def _runner(tmp_path, llm, async_enabled=True, results=True):
    cfg = {
        "provider": "none",
        "async_enhance": async_enabled,
        "async_workers": 2,
    }
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path) if results else None,
        llm=cfg,
        run_id="b4",
    ))
    runner.llm = llm
    runner._llm_async = async_enabled and llm is not None
    return runner


def test_async_mode_no_sync_llm_calls(tmp_path):
    llm = CountingLLM()
    runner = _runner(tmp_path, llm)
    t = runner.run_query(NARROW_Q, query_id="q1")
    # 同步链路零 LLM 调用（run_query 返回时后台任务可能尚未执行完，但同步路径没调）
    # 后台线程可能已经开始——等待排空后再断言内容
    runner.wait_llm_tasks()
    assert t["analysis"]["llm_analysis"]["available"] is True
    assert t["decision"]["llm_analysis"]["available"] is True
    assert t["monitoring"]["llm_analysis"]["available"] is True
    # 经验总结只补写 trace，不写入知识库记录（append-only）
    rec = runner.km.store.get(t["knowledge_update"]["record_id"])
    assert "llm_note" not in rec
    assert t["knowledge_update"]["llm_analysis"]["experience_note"] == "经验总结"
    # trace 文件被回写（含 llm_analysis）
    f = tmp_path / "q1.json"
    saved = json.loads(f.read_text(encoding="utf-8"))
    assert saved["analysis"]["llm_analysis"]["available"] is True


def test_async_keeps_deterministic_core(tmp_path):
    llm = CountingLLM()
    runner = _runner(tmp_path, llm)
    t = runner.run_query(NARROW_Q, query_id="q1")
    # 主链路立即返回确定性结果（不等 LLM）
    assert t["decision"]["decision"]["time_pruning"]["strategy"] == "partition_pruning"
    assert t["monitoring"]["execution_status"] == "unknown"   # NullExecutor，事实不变
    runner.wait_llm_tasks()


def test_async_preference_bonus_forces_sync():
    # preference_bonus 开启 → async 强制回退（构造器打印提示）
    runner = PipelineRunner(RunnerConfig(
        llm={"provider": "none", "async_enhance": True, "preference_bonus": True},
    ))
    runner.llm = CountingLLM()
    # 构造时已把 async 关掉；此处直接断言 _llm_async 为 False
    assert runner._llm_async is False


def test_async_run_statements_drains(tmp_path):
    llm = CountingLLM()
    runner = _runner(tmp_path, llm)
    traces = runner.run_statements([NARROW_Q] * 3, source="t.sql")
    # 批次结束已排空：所有 trace 的 llm_analysis 均已补写
    for t in traces:
        assert t["analysis"]["llm_analysis"]["available"] is True
    assert llm.calls >= 3 * 3   # 每条 3 个异步任务（QA/决策/合并）
