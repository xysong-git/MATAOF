"""PipelineRunner：MATAOF 系统的正式运行入口（闭环编排）。

闭环：Query → Analysis → Knowledge Retrieve → Decision → Execute（执行层）
      → Monitor → Knowledge Update → 追踪落盘。

追踪（可追溯性要求）：每次运行把完整决策链写入
    <results_dir>/<query_id>.json      # 完整追踪记录
    <results_dir>/run_log.jsonl        # 汇总行（追加，实验分析用）

配置（RunnerConfig / JSON 配置文件）：
    knowledge_store      知识库存放路径（None → 仅内存）
    results_dir          追踪记录输出目录（None → 不落盘）
    executor             执行层配置（null / file / 自定义对象）
    database_state       决策上下文（静态；真实环境可按查询动态更新）
    system_state         同上
    candidate_strategies / baseline_strategy / thresholds   决策与监控参数
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from mataof.agents.query_analysis import analyze
from mataof.agents.optimization_decision import decide
from mataof.agents.execution_monitoring import monitor
from mataof.agents.knowledge_memory import KnowledgeMemoryAgent
from mataof.executors import build_executor
from mataof.executors.base import ExecutionResult, Executor
from mataof.llm import build_llm_client


def split_sql_statements(text: str) -> list[str]:
    """按分号拆分语句，去掉 `--` 行注释与空行。

    查询集文件的注释是标注，不是可执行 SQL（IoTDB 不接受前导 `--`），
    拆分时一律去除，保证交给分析与执行的文本是干净的语句。
    """
    out: list[str] = []
    for raw in text.split(";"):
        lines = [l for l in raw.splitlines()
                 if l.strip() and not l.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            out.append(stmt)
    return out


@dataclass
class RunnerConfig:
    """PipelineRunner 配置。所有字段可缺省（安全默认）。"""

    knowledge_store: Optional[str] = None       # 知识库文件路径
    results_dir: Optional[str] = None           # 追踪输出目录
    executor: Any = None                        # null / file / 自定义对象
    database_state: dict = field(default_factory=dict)
    system_state: dict = field(default_factory=dict)
    candidate_strategies: Any = None
    baseline_strategy: Any = None
    thresholds: dict = field(default_factory=dict)
    reference_time: Optional[int] = None        # 历史时效衰减参考时间
    llm: Optional[dict] = None                  # LLM 配置（mataof/llm.py；None → 不启用）


class PipelineRunner:
    """正式运行入口：四 Agent 闭环 + 执行层对接 + 追踪落盘。

    用法：
        runner = PipelineRunner(RunnerConfig(
            knowledge_store="data/knowledge.json",
            results_dir="results",
            executor={"type": "file", "results_file": "execution_results.json"},
            database_state={...},
        ))
        trace = runner.run_query("SELECT ...", query_id="q1")
        traces = runner.run_file("queries.sql")
    """

    def __init__(self, config: RunnerConfig):
        self.config = config
        self.llm = build_llm_client(config.llm)   # None → 确定性路径（兜底）
        self.km = KnowledgeMemoryAgent(store_path=config.knowledge_store, llm=self.llm)
        self.executor: Executor = build_executor(config.executor)
        self._seq = 0

    # ------------------------------------------------------------------
    # 单查询闭环
    # ------------------------------------------------------------------
    def run_query(self, query: str, query_id: str = "") -> dict:
        """执行一次完整闭环，返回追踪记录（也写入 results_dir）。"""
        self._seq += 1
        if not query_id:
            query_id = f"query-{self._seq:04d}"

        # 1. 分析（LLM 增强可选；不可用自动回退确定性路径）
        analysis = analyze(query, query_id=query_id, llm=self.llm)
        # 2. 知识检索（历史证据）
        km_result = self.km.retrieve(
            analysis, database_state=self.config.database_state,
            system_state=self.config.system_state, query_id=query_id,
        )
        # 3. 决策（LLM 增强可选；bonus 开关来自配置 llm 节）
        decision = decide({
            "query_id": query_id,
            "query_analysis": analysis,
            "database_state": self.config.database_state,
            "system_state": self.config.system_state,
            "historical_records": km_result.get("matched_records_for_decision") or [],
            "candidate_strategies": self.config.candidate_strategies,
            "baseline_strategy": self.config.baseline_strategy,
            "reference_time": self.config.reference_time,
            "llm_preference_bonus": bool((self.config.llm or {}).get("preference_bonus")),
        }, llm=self.llm)
        # 4. 执行（真实执行层；NullExecutor 时不产生任何指标）
        execution: ExecutionResult = self.executor.execute(
            query, query_id, decision.get("selected_strategy") or {}
        )
        # 5. 监控（事实采集；LLM 增强可选）
        feedback = monitor({
            "query_id": query_id,
            "strategy_id": (decision.get("selected_strategy") or {}).get("strategy_id") or "",
            "execution_status": execution.execution_status,
            "failure_reason": execution.failure_reason,
            "metrics": execution.metrics,
            "baseline_metrics": execution.baseline_metrics,
            "thresholds": self.config.thresholds or None,
        }, llm=self.llm)
        # 6. 知识更新
        knowledge_update = self.km.update({
            "query_id": query_id,
            "query": query,
            "query_analysis": analysis,
            "decision": decision,
            "monitoring": feedback,
            "database_state": self.config.database_state,
            "system_state": self.config.system_state,
            "timestamp": execution.timestamp,
        })

        trace = {
            "query_id": query_id,
            "query": query,
            "analysis": analysis,
            "knowledge_retrieval": {
                "matched_count": len(km_result.get("matched_records") or []),
                "knowledge_confidence": km_result.get("knowledge_confidence"),
            },
            "decision": decision,
            "execution": {
                "execution_status": execution.execution_status,
                "failure_reason": execution.failure_reason,
                "metrics": execution.metrics,
                "baseline_metrics": execution.baseline_metrics,
                "timestamp": execution.timestamp,
            },
            "monitoring": feedback,
            "knowledge_update": knowledge_update,
        }
        self._write_trace(trace)
        return trace

    # ------------------------------------------------------------------
    # 批量运行
    # ------------------------------------------------------------------
    def run_file(self, path: str) -> list:
        """运行查询文件：.sql 按分号拆分语句；.jsonl 每行 {"query_id", "query"}。"""
        traces: list = []
        if path.endswith(".jsonl"):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    item = json.loads(line)
                    traces.append(self.run_query(
                        str(item.get("query") or ""), str(item.get("query_id") or "")
                    ))
            return traces
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for stmt in split_sql_statements(text):
            traces.append(self.run_query(stmt))
        return traces

    # ------------------------------------------------------------------
    # 知识库统计
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return self.km.stats()

    def retrieve(self, query: str, query_id: str = "") -> dict:
        """只做检索（不执行、不入库），用于检查历史知识。"""
        analysis = analyze(query, query_id=query_id)
        return self.km.retrieve(
            analysis, database_state=self.config.database_state,
            system_state=self.config.system_state, query_id=query_id,
        )

    # ------------------------------------------------------------------
    # 追踪落盘
    # ------------------------------------------------------------------
    def _write_trace(self, trace: dict) -> None:
        if not self.config.results_dir:
            return
        os.makedirs(self.config.results_dir, exist_ok=True)
        query_id = trace["query_id"]
        # 完整追踪记录
        with open(os.path.join(self.config.results_dir, f"{query_id}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(trace, f, ensure_ascii=False, indent=2)
        # 汇总行（追加，实验分析用）
        decision = trace["decision"]
        mon = trace["monitoring"]
        summary = {
            "query_id": query_id,
            "query_type": trace["analysis"].get("query_type"),
            "strategy_id": decision["selected_strategy"]["strategy_id"],
            "decision_status": decision["decision_status"],
            "execution_status": mon["execution_status"],
            "performance_assessment": mon["performance_assessment"],
            "anomaly_count": len(mon["anomalies"]),
            "response_time_ms": mon["metrics"]["response_time_ms"],
            "knowledge_confidence": trace["knowledge_retrieval"]["knowledge_confidence"],
            "stored": trace["knowledge_update"]["stored"],
        }
        with open(os.path.join(self.config.results_dir, "run_log.jsonl"),
                  "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")


def load_config_file(path: str) -> RunnerConfig:
    """从 JSON 配置文件加载 RunnerConfig。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return RunnerConfig(
        knowledge_store=data.get("knowledge_store"),
        results_dir=data.get("results_dir"),
        executor=data.get("executor"),
        database_state=data.get("database_state") or {},
        system_state=data.get("system_state") or {},
        candidate_strategies=data.get("candidate_strategies"),
        baseline_strategy=data.get("baseline_strategy"),
        thresholds=data.get("thresholds") or {},
        reference_time=data.get("reference_time"),
        llm=data.get("llm"),
    )
