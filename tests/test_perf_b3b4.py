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


class SlowLLM(CountingLLM):
    def chat(self, messages, max_tokens=2048, timeout_s=60.0):
        time.sleep(0.3)
        return super().chat(messages, max_tokens=max_tokens, timeout_s=timeout_s)


def test_wait_llm_tasks_timeout_zero_no_wait(tmp_path):
    llm = SlowLLM()
    runner = _runner(tmp_path, llm)
    traces = runner.run_statements([NARROW_Q] * 3, source="t.sql",
                                   drain_llm=False)   # 不排空
    t0 = time.time()
    pending = runner.wait_llm_tasks(timeout_s=0.0)     # 不等待
    assert time.time() - t0 < 0.5
    assert pending > 0                                # 任务仍在后台
    runner.wait_llm_tasks()                           # 最终排空（API 语义不变）
    for t in traces:
        assert t["analysis"]["llm_analysis"]["available"] is True


def test_wait_llm_tasks_timeout_positive(tmp_path):
    llm = SlowLLM()
    runner = _runner(tmp_path, llm)
    runner.run_statements([NARROW_Q] * 3, source="t.sql", drain_llm=False)
    pending = runner.wait_llm_tasks(timeout_s=5.0)    # 足够全部完成
    assert pending == 0


def test_stop_llm_executor_abandons_pending(tmp_path):
    """进程退出不被未完成补写阻塞：stop 后线程池关闭、pending 被放弃。

    确定性产物（已落盘的 trace 主体）不受影响；未完成补写保持未完成语义。
    """
    llm = SlowLLM()
    runner = _runner(tmp_path, llm)
    traces = runner.run_statements([NARROW_Q] * 3, source="t.sql",
                                   drain_llm=False)
    runner.stop_llm_executor()                        # 立即返回，不等待
    assert runner._llm_executor is None
    # 已落盘 trace 文件仍完整可读（原子回写不产生半写文件）
    for t in traces:
        path = runner._trace_paths[t["query_id"]]
        with open(path, encoding="utf-8") as f:
            saved = json.loads(f.read())
        assert saved["query_id"] == t["query_id"]


def test_decision_mode_llm_primary_wiring(tmp_path, monkeypatch):
    """decision_mode="llm_primary"：强制同步 + sample_rate 强制 1 + 每条查询
    恰好 1 次 LLM 调用（决策）；QA 解读关闭；决策带 llm_selected 标注。"""
    import mataof.runner as runner_mod

    class DecideLLM:
        name = "fake"
        model = "fake"

        def __init__(self):
            self.calls = 0

        def chat(self, messages, max_tokens=2048, timeout_s=60.0):
            self.calls += 1
            return json.dumps({"decisions": {
                "time_pruning": "full_scan", "filter_order": None,
                "aggregation_placement": None},
                "reasons": {"time_pruning": "保守选择全表扫描"}}, ensure_ascii=False)

    fake = DecideLLM()
    monkeypatch.setattr(runner_mod, "build_llm_client", lambda cfg: fake)
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path),
        llm={"provider": "none", "decision_mode": "llm_primary",
             "async_enhance": True, "sample_rate": 10},
        run_id="dm",
    ))
    assert runner._llm_decide is True
    assert runner._llm_async is False          # 决策必须同步：强制回退
    assert runner._llm_every == 1              # 采样强制 1（每条都要决策）

    trace = runner.run_query(NARROW_Q, query_id="dm1")
    assert fake.calls == 1                     # 每条查询恰好 1 次（决策）
    assert trace["decision"]["decision"]["time_pruning"]["strategy"] == "full_scan"
    assert "llm_selected" in trace["decision"]["decision"]["time_pruning"]["reason"]
    assert any("llm_selected" in n for n in trace["decision"]["notes"])
    assert trace["analysis"]["llm_analysis"]["available"] is False   # QA 解读关闭


def test_process_exits_despite_llm_backlog(tmp_path):
    """回归（子进程实测）：大量未完成补写积压时进程能及时退出。

    背景：ThreadPoolExecutor 的 atexit 钩子会 join 全部 worker——若排队任务
    未被取消，积压数小时的补写会让进程在汇总打印后"卡住"。此处积压 ≈7s，
    断言子进程 4s 内退出（无修复时需 ~7s+ 排空）。
    """
    import subprocess
    script = """
import json
import time
from mataof.runner import PipelineRunner, RunnerConfig

NARROW_Q = ("SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 "
            "AND time <= 1640970000000")

class SlowLLM:
    name = "fake"
    model = "fake"
    def chat(self, messages, max_tokens=2048, timeout_s=60.0):
        time.sleep(0.8)          # 单条补写 ≈ 3×0.8s；6 条积压 ≈ 7.2s
        return "{{}}"

runner = PipelineRunner(RunnerConfig(
    results_dir={results!r},
    llm={{"provider": "none", "async_enhance": True, "async_workers": 2}},
    run_id="exit-test",
))
runner.llm = SlowLLM()
runner._llm_async = True
runner.run_statements([NARROW_Q] * 6, source="t.sql", drain_llm=False)
# 不调用 stop：退出路径由 atexit 兜底（模拟 Ctrl+C/正常退出）
""".format(results=str(tmp_path / "results"))
    t0 = time.time()
    r = subprocess.run([sys.executable, "-c", script], capture_output=True,
                       text=True, timeout=30, cwd=os.path.dirname(
                           os.path.dirname(os.path.abspath(__file__))))
    elapsed = time.time() - t0
    assert r.returncode == 0, r.stderr
    assert elapsed < 4.0, f"进程退出耗时 {elapsed:.1f}s：排队补写未被取消"
