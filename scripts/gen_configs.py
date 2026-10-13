#!/usr/bin/env python3
"""生成全部实验配置文件（H3：消除配置漂移，单一事实来源）。

按 规模 × 模式 组合生成：
  规模：dbs（small）/ dbm（medium）/ dbl（large）
  模式：基础（agent 确定性）/ llm（异步 LLM 增强）/ fixed（固定策略对照）

另生成 config.local-llm.json（单库小样本 LLM 模式）。
运行：python3 scripts/gen_configs.py（在项目根目录）
生成后请自行核对；改动任一默认参数只需改本脚本再重新生成。
"""

from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCALES = {"dbs": "small", "dbm": "medium", "dbl": "large"}

EXECUTOR = {
    "type": "iotdb",
    "host": "127.0.0.1",
    "port": 6667,
    "user": "root",
    "password": "root",
    "fetch_size": 1024,
    "measure_baseline": True,
}

# LLM 共享片段（OpenAI 兼容协议接本地 vLLM Qwen3-8B）
def llm_section(scale: str) -> dict:
    return {
        "provider": "openai",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": "EMPTY",
        "model": "/opt/models/Qwen3-8B",
        "timeout_s": 180,
        "max_tokens": 1024,
        "log_file": "data/llm_log.jsonl",
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        "preference_bonus": False,
        "sample_rate": 10,
        "async_enhance": True,
        "async_workers": 2,
        "cache_file": f"data/llm_cache_{scale}.json",
    }


FIXED_STRATEGY = {
    "time_pruning": "full_scan",
    "filter_order": "time_first",
    "aggregation_placement": "final_level_aggregation",
}


def base_config(scale: str, mode: str) -> dict:
    cfg = {
        "knowledge_store": f"data/knowledge_{mode_prefix(scale, mode)}.json",
        "results_dir": f"results/round_{mode_prefix(scale, mode)}",
        "executor": dict(EXECUTOR),
        "database_state": {"data_scale_level": SCALES[scale]},
        "system_state": {},
        "candidate_strategies": None,
        "baseline_strategy": None,
        "thresholds": {},
        "reference_time": None,
        "knowledge_save_interval": 500,
        "async_drain_timeout": 0,
    }
    if mode == "llm":
        cfg["llm"] = llm_section(scale)
    if mode == "fixed":
        cfg["fixed_strategy"] = dict(FIXED_STRATEGY)
    return cfg


def mode_prefix(scale: str, mode: str) -> str:
    return {"base": scale, "llm": f"{scale}_llm", "fixed": f"fixed_{scale}"}[mode]


def main() -> None:
    for scale in SCALES:
        for mode, fname in (("base", f"config.{scale}.json"),
                            ("llm", f"config.{scale}.llm.json"),
                            ("fixed", f"config.{scale}.fixed.json")):
            path = os.path.join(ROOT, fname)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(base_config(scale, mode), f, ensure_ascii=False, indent=2)
            print(f"生成 {fname}")
    # local-llm：dbs 规模的 LLM 配置，独立知识库与结果目录
    local = base_config("dbs", "llm")
    local["knowledge_store"] = "data/knowledge_local.json"
    local["results_dir"] = "results/round_local_llm"
    with open(os.path.join(ROOT, "config.local-llm.json"), "w", encoding="utf-8") as f:
        json.dump(local, f, ensure_ascii=False, indent=2)
    print("生成 config.local-llm.json")


if __name__ == "__main__":
    main()
