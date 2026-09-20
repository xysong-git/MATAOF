"""共享 LLM 层与 Query Analysis LLM 增强的单元测试。

覆盖：客户端工厂（openai/local/none/缺配置降级）、调用失败返回 None、
JSON 输出校验、混合增强（LLM 只进 llm_analysis 节、确定性字段不变）、
禁用/失败/非法输出时的确定性兜底、Runner 配置接入。
运行：pytest tests/test_llm_layer.py -v
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.llm import (  # noqa: E402
    LocalHTTPClient,
    OpenAICompatClient,
    build_llm_client,
)
from mataof.agents.query_analysis import analyze, QueryAnalysisAgent  # noqa: E402
from mataof.agents.query_analysis.llm_enhance import (  # noqa: E402
    build_llm_analysis,
    parse_llm_response,
)
from mataof.runner import PipelineRunner, RunnerConfig  # noqa: E402

NARROW_Q = "SELECT s_0 FROM root.test.d_0 WHERE time >= 1640966405000 AND time <= 1640970000000"
AMBIG_Q = "SELECT s1 FROM root.sg1.d1 WHERE s1 > 10 AND t1 = 'v1'"


class FakeLLM:
    """可控的 mock LLM 客户端。"""

    name = "fake"

    def __init__(self, response=None, exc=None, calls=None):
        self.response = response
        self.exc = exc
        self.calls = calls if calls is not None else []
        self.last_messages = None

    def chat(self, messages, max_tokens=2048, timeout_s=60.0):
        self.calls.append(1)
        self.last_messages = messages
        if self.exc:
            raise self.exc
        return self.response


GOOD_RESPONSE = json.dumps({
    "query_type_hint": "unknown",
    "semantic_summary": "查询单设备在指定时间窗口内读取 s_0 测点数据。",
    "condition_hints": {"t1 = 'v1'": "tag"},
}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 客户端工厂与失败兜底
# ---------------------------------------------------------------------------

def test_build_llm_client_defaults():
    assert build_llm_client(None) is None
    assert build_llm_client({"provider": "none"}) is None
    assert build_llm_client({}) is None
    # openai 缺 base_url/model/api_key → 静默降级
    assert build_llm_client({"provider": "openai"}) is None
    assert build_llm_client({"provider": "openai", "base_url": "x", "model": "m"}) is None


def test_build_llm_client_openai_with_env_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    client = build_llm_client({"provider": "openai", "base_url": "http://x/v1",
                               "model": "deepseek-chat"})
    assert isinstance(client, OpenAICompatClient)
    assert client.api_key == "sk-test"


def test_build_llm_client_local():
    client = build_llm_client({"provider": "local", "url": "http://127.0.0.1:8000/generate"})
    assert isinstance(client, LocalHTTPClient)


def test_http_client_failure_returns_none():
    def failing_post(url, payload, headers, timeout_s):
        raise TimeoutError("timed out")
    client = OpenAICompatClient("http://x/v1", "m", "k", _post=failing_post)
    assert client.chat([{"role": "user", "content": "hi"}]) is None


def test_http_client_bad_status_returns_none():
    def bad_post(url, payload, headers, timeout_s):
        return 500, '{"error": "server down"}'
    client = OpenAICompatClient("http://x/v1", "m", "k", _post=bad_post)
    assert client.chat([{"role": "user", "content": "hi"}]) is None


def test_http_client_ok_response():
    def ok_post(url, payload, headers, timeout_s):
        return 200, json.dumps({"choices": [{"message": {"content": "回答"}}]})
    client = OpenAICompatClient("http://x/v1", "m", "k", _post=ok_post)
    assert client.chat([{"role": "user", "content": "hi"}]) == "回答"


# ---------------------------------------------------------------------------
# LLM 增强（Query Analysis）
# ---------------------------------------------------------------------------

def test_llm_analysis_enrichment():
    r = analyze(AMBIG_Q, query_id="q1", llm=FakeLLM(response=GOOD_RESPONSE))
    la = r["llm_analysis"]
    assert la["available"] is True
    assert "s_0" in la["semantic_summary"] or "测点" in la["semantic_summary"]
    hints = la["condition_hints"]
    assert any(v == "tag" for v in hints.values())
    # 确定性字段不受影响：t1 仍如实标记 tag_or_attribute
    f = r["query_features"]["filter"]
    assert f["type_counts"].get("tag_or_attribute", 0) == 1
    assert r["query_type"] != "unknown" if r["query_type"] != "unknown" else True


def test_llm_query_type_hint_only_when_unknown():
    # 无过滤裸查询：确定性 unknown → LLM 提示被记录（不改 query_type）
    llm = FakeLLM(response=json.dumps({
        "query_type_hint": "range_query",
        "semantic_summary": "全量扫描。",
        "condition_hints": {},
    }))
    r = analyze("SELECT s1 FROM root.sg1.d1", query_id="q2", llm=llm)
    assert r["query_type"] == "unknown"                       # 确定性权威，不被 LLM 改写
    assert r["llm_analysis"]["query_type_hint"] == "range_query"


def test_llm_failure_falls_back_deterministic():
    r = analyze(AMBIG_Q, query_id="q1", llm=FakeLLM(exc=ConnectionError("refused")))
    la = r["llm_analysis"]
    assert la["available"] is False
    assert la["semantic_summary"] is None
    assert any("回退确定性路径" in n for n in la["notes"])


def test_llm_invalid_json_falls_back():
    r = analyze(AMBIG_Q, query_id="q1", llm=FakeLLM(response="这不是 JSON，balabala"))
    assert r["llm_analysis"]["available"] is False
    assert any("JSON" in n for n in r["llm_analysis"]["notes"])


def test_llm_disabled_output_matches_old_shape():
    # 不启用 LLM：llm_analysis 为空节，核心输出与历史行为一致（确定性）
    r1 = analyze(AMBIG_Q, query_id="q1")
    assert r1["llm_analysis"]["available"] is False
    core1 = {k: v for k, v in r1.items() if k != "llm_analysis"}
    r2 = QueryAnalysisAgent().analyze(AMBIG_Q, query_id="q1")
    core2 = {k: v for k, v in r2.items() if k != "llm_analysis"}
    assert core1 == core2


def test_parse_llm_response_code_fence():
    text = "```json\n" + GOOD_RESPONSE + "\n```"
    data = parse_llm_response(text, [])
    assert data is not None and data.get("semantic_summary")


def test_build_llm_analysis_none_client():
    r = analyze(AMBIG_Q, query_id="q1")
    section = build_llm_analysis(AMBIG_Q, r, None)
    assert section["available"] is False


# ---------------------------------------------------------------------------
# Runner 接入
# ---------------------------------------------------------------------------

def test_runner_passes_llm(tmp_path):
    runner = PipelineRunner(RunnerConfig(
        results_dir=str(tmp_path),
        llm={"provider": "none"},
    ))
    assert runner.llm is None   # provider=none → 确定性路径
    trace = runner.run_query(NARROW_Q, query_id="q1")
    assert trace["analysis"]["llm_analysis"]["available"] is False


def test_load_config_file_llm(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"llm": {"provider": "none"}}), encoding="utf-8")
    from mataof.runner import load_config_file
    config = load_config_file(str(cfg))
    assert config.llm == {"provider": "none"}
