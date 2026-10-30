#!/usr/bin/env python3
"""生成全部实验配置文件（H3：消除配置漂移，单一事实来源）。

按 规模 × 控制流/LLM 组合生成（2×2 + 固定策略对照）：
  规模：dbs（small）/ dbm（medium）/ dbl（large）
  控制流：pipeline（固定流水线）/ agent（观察驱动闭环）
  LLM：off（provider=none）/ on（共享 LLM，决策每条 1 次）

  配置矩阵：
    config.{scale}.pipeline.json      P0：pipeline + 无 LLM
    config.{scale}.pipeline-llm.json  P1：pipeline + LLM
    config.{scale}.json               A0：agent + 无 LLM
    config.{scale}.llm.json           A1：agent + LLM（主实验）
    config.{scale}.fixed.json         固定策略对照（Baseline 2）

另生成 config.local-llm.json（单库小样本 A1 模式）。
运行：python3 scripts/gen_configs.py（在项目根目录）
生成后请自行核对；改动任一默认参数只需改本脚本再重新生成。
"""

from __future__ import annotations

import copy
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
    # 评估策略更新：不再逐条配对双执行——每条查询只执行优化后的语句；
    # baseline（原始语句）由 native 模式单独跑，跨轮对比。
    "measure_baseline": False,
}

# LLM 共享片段（OpenAI 兼容协议接本地 vLLM Qwen3-8B）
# decision_mode="llm_primary"：LLM 参与策略选择（特征化 prompt + 输出缓存 +
# 合法性校验，确定性为兜底）——OD 决策每条查询 1 次调用，同质查询命中缓存；
# agents.llm_shared=true 时 QA 按 on_gap、EM 按 on_anomaly 共享同一客户端；
# KM 经验总结按查询形状收口（同一形状只调用一次）。
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
        "decision_mode": "llm_primary",
        "sample_rate": 1,
        "async_enhance": False,
        "async_workers": 2,
        "cache_file": f"data/llm_cache_{scale}.json",
    }


FIXED_STRATEGY = {
    "alignment": "time_aligned",
    "fetch_size": "fetch_default",
}

# 四 Agent 运行时配置（与 config.example.json / agent-runtime.md 一致）。
# 2×2 对照通过 agents.control 切换控制流：
#   control="pipeline"：固定流水线（单次决策/执行、固定 top-K，无 replan/recall）
#   control="agent"   ：观察驱动闭环（degraded/failed 重规划、空证据 recall）
# LLM 维度由 llm 节 + agents.llm_shared 控制；无 LLM 模式不写 llm 节。
AGENT_DEFAULTS = {
    "query_analysis": {
        "llm_policy": "on_gap",              # on_gap | always | never
        "confidence_threshold": 0.5,
        "deadline_ms": 10000,
    },
    "optimization_decision": {
        "max_attempts": 2,
        "deadline_ms": 30000,
        "replan_on": ["degraded", "failed"],
        "recall_on_empty_evidence": True,
        "recall_max_records": None,
    },
    "execution_monitoring": {
        # on_anomaly | on_anomaly_or_degraded | always | never
        # 默认 on_anomaly：只在真实异常/失败时解读；degraded 由确定性链路
        # 记录，不再逐条触发 LLM（on_anomaly_or_degraded 保留为消融）。
        "llm_policy": "on_anomaly",
        "deadline_ms": 5000,
    },
    "knowledge_memory": {
        "min_strong": 1,
        "max_rounds": 2,
        "expand_factor": 2,
        "deadline_ms": 2000,
    },
}


def agents_section(enabled: bool, llm_shared: bool = False,
                   control: str = "agent") -> dict:
    """生成 agents 配置节；每个配置里显式出现，作为可发现的开关。

    llm_shared=true：同一个 LLM 客户端按各 Agent 的 llm_policy 共享给
    QA/EM/KM（B：LLM 常驻）；false：仅 OD 使用 LLM（旧行为，便于消融）。
    control：pipeline（固定控制流）/ agent（观察驱动闭环）。
    """
    section = {
        "enabled": bool(enabled),
        "llm_shared": bool(llm_shared),
        "control": control,
    }
    section.update(copy.deepcopy(AGENT_DEFAULTS))
    return section


# mode → (配置后缀, 控制流, 是否启用 LLM, 结果目录前缀, 知识库名)
MODE_SPECS = {
    "pipeline": ("pipeline", "pipeline", False,
                 "pipeline_{scale}", "knowledge_pipeline"),
    "pipeline-llm": ("pipeline-llm", "pipeline", True,
                     "pipeline_llm_{scale}", "knowledge_pipeline_llm"),
    "agent": ("", "agent", False, "agent_{scale}", "knowledge_agent"),
    "llm": ("llm", "agent", True, "{scale}_llm", "knowledge_llm"),
    "fixed": ("fixed", "agent", False, "fixed_{scale}", "knowledge_fixed"),
}


def base_config(scale: str, mode: str) -> dict:
    # 持久化层（D8b 第五步实测结论）：sqlite 增量 INSERT——写入 291× 于整库 JSON
    # 快照；加载 2s/51k（一次性）；事务崩溃安全。
    # 统一知识库（设计更新）：跨规模共享（dbs/dbm/dbl 共用同一库——不同数据集的
    # 经验互相迁移，无需每个数据集重新学习）；跨模式隔离（2×2 各单元与 fixed
    # 各自独立——避免一种控制流/LLM 配置的执行记录污染另一种的历史证据）。
    _, control, use_llm, result_prefix, kb = MODE_SPECS[mode]
    cfg = {
        "knowledge_store": f"data/{kb}.db",
        "knowledge_backend": "sqlite",
        "results_dir": f"results/round_{result_prefix.format(scale=scale)}",
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
    # fixed 模式跳过决策/知识检索，不需要 agent 运行时；其余四种组合启用
    # 四 Agent 组件，仅通过 control/llm_shared 切换控制流与 LLM 维度。
    enabled = mode != "fixed"
    cfg["agents"] = agents_section(
        enabled=enabled,
        llm_shared=(enabled and use_llm),
        control=control,
    )
    if use_llm:
        cfg["llm"] = llm_section(scale)
    if mode == "fixed":
        # 固定策略对照不走计划生成，无需设备清单
        cfg["fixed_strategy"] = dict(FIXED_STRATEGY)
    else:
        # 通配 FROM 的设备分区展开清单；由 scripts/gen_device_inventory.py
        # 生成并做完整性校验；文件缺失时自动降级（不生成设备分区计划）。
        cfg["device_inventory_file"] = "data/device_inventory.json"
    return cfg


def config_name(scale: str, mode: str) -> str:
    suffix = MODE_SPECS[mode][0]
    return f"config.{scale}.{suffix}.json" if suffix else f"config.{scale}.json"


def main() -> None:
    for scale in SCALES:
        for mode in MODE_SPECS:
            fname = config_name(scale, mode)
            path = os.path.join(ROOT, fname)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(base_config(scale, mode), f, ensure_ascii=False, indent=2)
            print(f"生成 {fname}")
    # local-llm：dbs 规模的 A1 配置，独立知识库与结果目录
    local = base_config("dbs", "llm")
    local["knowledge_store"] = "data/knowledge_local.db"
    local["results_dir"] = "results/round_local_llm"
    with open(os.path.join(ROOT, "config.local-llm.json"), "w", encoding="utf-8") as f:
        json.dump(local, f, ensure_ascii=False, indent=2)
    print("生成 config.local-llm.json")


if __name__ == "__main__":
    main()
