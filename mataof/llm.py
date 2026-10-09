"""共享 LLM 层：统一客户端抽象（API / 本地双后端）+ 确定性兜底。

设计约束（与系统核心原则一致）：
- LLM 只做"增强"，不做"事实生产"：Agent 在 LLM 不可用、超时、输出非法时
  一律回退到确定性规则路径（chat 返回 None）；
- 零新依赖：OpenAI 兼容协议用 stdlib urllib 直接发 REST 请求；
- 可追踪：每次调用写入 JSONL 日志（时间/延迟/状态/用量；完整内容默认不落盘）；
- 调用是尽力而为的：任何异常都被捕获并返回 None，绝不让 LLM 故障拖垮决策链路。

配置（配置文件 "llm" 节）：
    {"provider": "none"}                                  # 禁用（默认）
    {"provider": "openai", "base_url": "...",             # OpenAI 兼容 API
     "api_key": "..." 或环境变量 LLM_API_KEY, "model": "..."}
    {"provider": "local", "url": "http://127.0.0.1:8000/generate",
     "model": "qwen2.5-3b"}                               # 本地推理服务（MAPO 式 FastAPI）
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from typing import Any, Callable, Optional

# 调用日志（JSONL，追加）；None 表示不落盘
_DEFAULT_LOG = None


class LLMClient:
    """LLM 客户端抽象。chat 返回文本；任何失败返回 None。"""

    name: str = "abstract"

    def chat(self, messages: list[dict], max_tokens: int = 2048,
             timeout_s: float = 60.0) -> Optional[str]:
        raise NotImplementedError


def _log_call(log_file: Optional[str], entry: dict) -> None:
    if not log_file:
        return
    try:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _is_local_host(url: str) -> bool:
    """本地/回环地址（本地 vLLM 等推理服务不应绕经系统代理）。"""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")
    except Exception:
        return False


def _post_json(url: str, payload: dict, headers: dict,
               timeout_s: float) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    if _is_local_host(url):
        # 本地推理服务直连，绕过系统代理（http_proxy 等环境变量）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()   # 远程 API 遵循系统代理配置
    with opener.open(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return resp.status, body


class OpenAICompatClient(LLMClient):
    """OpenAI 兼容 Chat Completions 客户端（DeepSeek/OpenAI/自建端点通用）。

    _post 可注入用于测试。
    """

    name = "openai"

    def __init__(self, base_url: str, model: str, api_key: str,
                 timeout_s: float = 60.0, max_tokens: int = 2048,
                 log_file: Optional[str] = None,
                 extra_body: Optional[dict] = None,
                 _post: Optional[Callable[..., tuple[int, str]]] = None):
        self.base_url = str(base_url).rstrip("/")
        self.model = str(model)
        self.api_key = api_key
        self.timeout_s = float(timeout_s)
        self.default_max_tokens = int(max_tokens)
        self.log_file = log_file
        self.extra_body = extra_body or {}
        self._post = _post or _post_json

    def chat(self, messages: list[dict], max_tokens: int = 0,
             timeout_s: float = 0.0) -> Optional[str]:
        t0 = time.time()
        entry = {
            "ts": int(t0 * 1000), "provider": "openai", "model": self.model,
            "messages": len(messages),
            "max_tokens": max_tokens or self.default_max_tokens,
        }
        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens or self.default_max_tokens,
                "stream": False,
            }
            payload.update(self.extra_body)   # 透传 vLLM 等实现的扩展参数
            status, body = self._post(
                f"{self.base_url}/chat/completions",
                payload,
                {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                timeout_s or self.timeout_s,
            )
            entry.update({"status": status, "latency_ms": round((time.time() - t0) * 1000)})
            if status != 200:
                entry["error"] = body[:200]
                _log_call(self.log_file, entry)
                return None
            data = json.loads(body)
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content")
            if not isinstance(text, str) or not text.strip():
                entry["error"] = "空响应"
                _log_call(self.log_file, entry)
                return None
            entry["response_chars"] = len(text)
            _log_call(self.log_file, entry)
            return text.strip()
        except Exception as exc:  # 网络/超时/解析——一律降级 None
            entry.update({"error": f"{type(exc).__name__}: {str(exc)[:200]}",
                          "latency_ms": round((time.time() - t0) * 1000)})
            _log_call(self.log_file, entry)
            return None


class LocalHTTPClient(LLMClient):
    """本地推理服务客户端（MAPO 式 FastAPI：POST {"messages", "max_tokens"} → {"text"}）。"""

    name = "local"

    def __init__(self, url: str, model: str = "local", timeout_s: float = 120.0,
                 max_tokens: int = 2048, log_file: Optional[str] = None,
                 _post: Optional[Callable[..., tuple[int, str]]] = None):
        self.url = str(url)
        self.model = str(model)
        self.timeout_s = float(timeout_s)
        self.default_max_tokens = int(max_tokens)
        self.log_file = log_file
        self._post = _post or _post_json

    def chat(self, messages: list[dict], max_tokens: int = 0,
             timeout_s: float = 0.0) -> Optional[str]:
        t0 = time.time()
        entry = {"ts": int(t0 * 1000), "provider": "local", "model": self.model,
                 "messages": len(messages), "max_tokens": max_tokens or self.default_max_tokens}
        try:
            status, body = self._post(
                self.url,
                {"messages": messages, "max_tokens": max_tokens or self.default_max_tokens},
                {"Content-Type": "application/json"},
                timeout_s or self.timeout_s,
            )
            entry.update({"status": status, "latency_ms": round((time.time() - t0) * 1000)})
            if status != 200:
                entry["error"] = body[:200]
                _log_call(self.log_file, entry)
                return None
            text = json.loads(body).get("text")
            if not isinstance(text, str) or not text.strip():
                entry["error"] = "空响应"
                _log_call(self.log_file, entry)
                return None
            entry["response_chars"] = len(text)
            _log_call(self.log_file, entry)
            return text.strip()
        except Exception as exc:
            entry.update({"error": f"{type(exc).__name__}: {str(exc)[:200]}",
                          "latency_ms": round((time.time() - t0) * 1000)})
            _log_call(self.log_file, entry)
            return None


def hash_messages(messages: list) -> str:
    """对消息序列做稳定哈希（缓存键）。"""
    return hashlib.sha256(
        json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class CachedLLMClient(LLMClient):
    """LLM 输出缓存包装（B3）：同 prompt（含模型名）→ 直接返回缓存响应。

    - 持久化写穿（cache_file JSON），线程安全；容量上限（默认 100k 条）；
    - 缓存只缓存原始响应文本——各 Agent 的解析/校验照常执行，
      编造检测等安全机制不受影响；
    - 命中时不产生底层调用（LLM 开销趋零），统计在 log_file 中标记 cached。
    """

    name = "cached"

    def __init__(self, inner: LLMClient, cache_file: str,
                 max_entries: int = 100_000, log_file: Optional[str] = None):
        self._inner = inner
        self.cache_file = cache_file
        self.max_entries = int(max_entries)
        self.log_file = log_file
        self._lock = threading.Lock()
        self._entries: dict = {}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                    self._entries = data["entries"]
        except (OSError, json.JSONDecodeError):
            self._entries = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.cache_file) or ".", exist_ok=True)
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"entries": self._entries}, f, ensure_ascii=False,
                          separators=(",", ":"))
            os.replace(tmp, self.cache_file)
        except OSError:
            pass

    def chat(self, messages: list, max_tokens: int = 0,
             timeout_s: float = 0.0) -> Optional[str]:
        key = f"{getattr(self._inner, 'model', self._inner.name)}::{hash_messages(messages)}"
        with self._lock:
            if key in self._entries:
                text = self._entries[key]
                _log_call(self.log_file, {
                    "ts": int(time.time() * 1000), "provider": "cached",
                    "model": getattr(self._inner, "model", ""),
                    "messages": len(messages), "status": "cache_hit",
                })
                return text
        text = self._inner.chat(messages, max_tokens=max_tokens, timeout_s=timeout_s)
        if text is not None:
            with self._lock:
                if len(self._entries) >= self.max_entries:
                    # 容量保护：清掉最早一半（缓存是易失优化，不承担正确性）
                    for k in list(self._entries.keys())[: self.max_entries // 2]:
                        del self._entries[k]
                self._entries[key] = text
                self._save()
        return text


def build_llm_client(spec: Optional[dict]) -> Optional[LLMClient]:
    """按配置构造 LLM 客户端；provider=none/无配置/缺密钥 → None（安全降级）。

    OpenAI 兼容 API 的 api_key 支持配置文件或环境变量 LLM_API_KEY。
    cache_file 非空时自动包一层 CachedLLMClient（B3）。
    """
    if not isinstance(spec, dict):
        return None
    provider = str(spec.get("provider") or "none")
    log_file = spec.get("log_file") or _DEFAULT_LOG
    timeout_s = float(spec.get("timeout_s") or 60.0)
    max_tokens = int(spec.get("max_tokens") or 2048)
    cache_file = spec.get("cache_file")

    client: Optional[LLMClient] = None
    if provider in ("none", ""):
        return None
    if provider == "openai":
        base_url = spec.get("base_url")
        model = spec.get("model")
        api_key = spec.get("api_key") or os.environ.get("LLM_API_KEY") or ""
        if not base_url or not model or not api_key:
            return None   # 缺配置 → 静默降级（规则路径照常）
        client = OpenAICompatClient(base_url=base_url, model=model, api_key=api_key,
                                    timeout_s=timeout_s, max_tokens=max_tokens,
                                    log_file=log_file,
                                    extra_body=spec.get("extra_body")
                                               if isinstance(spec.get("extra_body"), dict)
                                               else None)
    elif provider == "local":
        url = spec.get("url")
        if url:
            client = LocalHTTPClient(url=url, model=str(spec.get("model") or "local"),
                                     timeout_s=timeout_s, max_tokens=max_tokens,
                                     log_file=log_file)
    if client is not None and isinstance(cache_file, str) and cache_file.strip():
        client = CachedLLMClient(client, cache_file=cache_file.strip(), log_file=log_file)
    return client
