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
import random
import threading
import time
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


def sample_items(items: list, limit: Optional[int], mode: str,
                 rng: random.Random, seed: int) -> tuple[list, Optional[dict]]:
    """按限制从条目列表取样。

    - limit 为 None/0/≥len → 全量，返回 (items, None)；
    - mode="seq"     → 取前 limit 条；
    - mode="random"  → 用 rng（种子 seed）随机抽 limit 条，保持文件内原有顺序
      （同种子 → 同抽样结果，实验可复现）。
    返回 (取样后的列表, 采样说明 dict 或 None)。
    """
    if not limit or limit <= 0 or limit >= len(items):
        return items, None
    if mode == "random":
        idx = sorted(rng.sample(range(len(items)), min(int(limit), len(items))))
        info = {"mode": "random", "limit": int(limit), "total": len(items),
                "seed": seed}
        return [items[i] for i in idx], info
    info = {"mode": "seq", "limit": int(limit), "total": len(items)}
    return items[: int(limit)], info


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
    run_id: Optional[str] = None                # 本轮运行标识（自动生成；保证文件名唯一）
    per_file_limit: Optional[int] = None        # 每个 SQL 文件最多取多少条语句（None → 全量）
    sample_mode: str = "seq"                    # 采样方式：seq（顺序前 N 条）| random（随机抽样，种子可复现）
    sample_seed: int = 42                       # random 采样种子（同种子 → 同抽样结果）
    knowledge_max_records: int = 100            # 检索/证据 top-K（控制证据规模与每查询开销）
    eval_prior_strength: float = 0.10           # D1：评价表先验加分强度（有界 ±strength）
    eval_min_count: int = 3                     # D1：评价表先验所需最小样本数
    fixed_strategy: Optional[dict] = None       # D3：固定策略对照（非空 → 跳过决策 Agent）
    knowledge_save_interval: int = 1            # B5：知识库批量落盘间隔（大库实验建议 20~50）


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
        self.km = KnowledgeMemoryAgent(store_path=config.knowledge_store, llm=self.llm,
                                       save_interval=config.knowledge_save_interval)
        self.executor: Executor = build_executor(config.executor)
        # 每次运行生成唯一 run_id：自动 query_id 的前缀（同名文件不覆盖）
        self.run_id = config.run_id or time.strftime("%Y%m%d-%H%M%S")
        # LLM 增强采样：sample_rate=N 表示每 N 条查询启用一次 LLM（实验采样）
        sr = (config.llm or {}).get("sample_rate")
        self._llm_every = int(sr) if isinstance(sr, (int, float)) and not isinstance(sr, bool) and sr >= 1 else 1
        self._rng = random.Random(config.sample_seed)
        self._seq = 0
        self._seq_lock = threading.Lock()   # B2 并行下 query_id 序号分配线程安全

        # B4：异步增强模式（确定性链路零 LLM 调用，后台补写 llm_analysis）
        self._llm_async = bool((config.llm or {}).get("async_enhance")) and self.llm is not None
        if self._llm_async and (config.llm or {}).get("preference_bonus"):
            # LLM 参与评分必须同步，强制回退并提示
            self._llm_async = False
            print("[runner] 注意: preference_bonus 开启，async_enhance 强制回退为同步模式",
                  flush=True)
        self._llm_workers = max(1, int((config.llm or {}).get("async_workers") or 2))
        self._llm_executor = None
        self._llm_futures: list = []
        self._llm_futures_lock = threading.Lock()
        self._trace_paths: dict = {}   # query_id → 实际落盘路径（异步回写用）

    def _llm_for_query(self) -> Optional[object]:
        """按采样率决定本条查询是否启用同步 LLM；未采样 → None（确定性快路径）。

        B4 异步模式下同步路径永远不用 LLM（后台统一补写），采样逻辑被绕过。
        """
        if self._llm_async:
            return None
        if self._llm_every <= 1:
            return self.llm
        return self.llm if (self._seq % self._llm_every) == 0 else None

    # ------------------------------------------------------------------
    # 单查询闭环
    # ------------------------------------------------------------------
    def run_query(self, query: str, query_id: str = "", source: str = "",
                  sampling: Optional[dict] = None) -> dict:
        """执行一次完整闭环，返回追踪记录（也写入 results_dir）。

        query_id 缺省时自动生成 f"{run_id}-{seq:04d}"（每次运行唯一，不覆盖历史结果）。
        """
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        if not query_id:
            query_id = f"{self.run_id}-{seq:04d}"

        llm = self._llm_for_query()   # 采样控制；未采样 → 确定性快路径

        # 1. 分析（LLM 增强可选；不可用自动回退确定性路径）
        analysis = analyze(query, query_id=query_id, llm=llm)
        # 2. 知识检索（历史证据；runner 链路关闭检索解读 LLM——决策只消费结构化记录）
        #    fixed 模式（Baseline 2）不使用历史，跳过检索
        if self.config.fixed_strategy:
            km_result = {"matched_count": 0, "knowledge_confidence": 0.0,
                         "matched_records_for_decision": [],
                         "strategy_evaluations": {}}
        else:
            km_result = self.km.retrieve(
                analysis, database_state=self.config.database_state,
                system_state=self.config.system_state, query_id=query_id,
                llm_summary=False,
                max_records=self.config.knowledge_max_records,
            )
        # 3. 决策（LLM 增强可选；bonus 开关来自配置 llm 节）
        decision_input = {
            "query_id": query_id,
            "query_analysis": analysis,
            "database_state": self.config.database_state,
            "system_state": self.config.system_state,
            "historical_records": km_result.get("matched_records_for_decision") or [],
            "candidate_strategies": self.config.candidate_strategies,
            "baseline_strategy": self.config.baseline_strategy,
            "reference_time": self.config.reference_time,
            "llm_preference_bonus": bool((self.config.llm or {}).get("preference_bonus")),
            # D1：策略评价表（反馈驱动的动态权重，来自 KM 检索输出）
            "strategy_evaluations": km_result.get("strategy_evaluations"),
            "eval_prior_strength": self.config.eval_prior_strength,
            "eval_min_count": self.config.eval_min_count,
        }
        if self.config.fixed_strategy:
            decision = self._fixed_decision(analysis, query_id)
        else:
            decision = decide(decision_input, llm=llm)
        # 4. 执行（真实执行层；NullExecutor 时不产生任何指标）
        execution: ExecutionResult = self.executor.execute(
            query, query_id, decision.get("selected_strategy") or {}
        )
        # 5. 监控（事实采集，确定性）
        monitoring_input = {
            "query_id": query_id,
            "strategy_id": (decision.get("selected_strategy") or {}).get("strategy_id") or "",
            "execution_status": execution.execution_status,
            "failure_reason": execution.failure_reason,
            "metrics": execution.metrics,
            "baseline_metrics": execution.baseline_metrics,
            "thresholds": self.config.thresholds or None,
        }
        feedback = monitor(monitoring_input, llm=None)
        # B1：合并 LLM 调用（监控解读 + KM 经验总结一次完成，4→3 次/条）
        experience_note: Optional[str] = None
        if llm is not None:
            from mataof.agents.execution_monitoring.llm_enhance import (
                build_post_execution_llm,
            )
            combined = build_post_execution_llm(llm, monitoring_input, feedback)
            feedback["llm_analysis"] = {
                "available": (combined["performance_summary"] is not None
                              or combined["anomaly_summary"] is not None),
                "performance_summary": combined["performance_summary"],
                "anomaly_summary": combined["anomaly_summary"],
                "notes": combined["notes"],
            }
            experience_note = combined["experience_note"]
        # 6. 知识更新（use_llm=False：经验总结已在合并调用中生成）
        knowledge_update = self.km.update({
            "query_id": query_id,
            "query": query,
            "query_analysis": analysis,
            "decision": decision,
            "monitoring": feedback,
            "database_state": self.config.database_state,
            "system_state": self.config.system_state,
            "timestamp": execution.timestamp,
        }, experience_note=experience_note, use_llm=False)

        trace = {
            "query_id": query_id,
            "query": query,
            "source": source,          # 来源文件（run_file 传入；手动查询为空串）
            "sampling": sampling,      # 每文件采样说明（无采样为 None）
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
        trace_path = self._write_trace(trace)
        if trace_path:
            with self._seq_lock:
                self._trace_paths[query_id] = trace_path
        # B4：异步增强——同步链路已完成，LLM 解读交由后台补写（不阻塞主链路）
        if self._llm_async:
            self._enqueue_llm_backfill(trace, query, decision_input, monitoring_input)
        return trace

    # ------------------------------------------------------------------
    # B4：异步 LLM 增强（后台补写 llm_analysis，不阻塞确定性链路）
    # ------------------------------------------------------------------
    def _enqueue_llm_backfill(self, trace: dict, query: str,
                              decision_input: dict, monitoring_input: dict) -> None:
        if self._llm_executor is None:
            from concurrent.futures import ThreadPoolExecutor
            self._llm_executor = ThreadPoolExecutor(
                max_workers=self._llm_workers, thread_name_prefix="llm-backfill")
        fut = self._llm_executor.submit(
            self._backfill_llm, trace, query, decision_input, monitoring_input)
        with self._llm_futures_lock:
            self._llm_futures.append(fut)

    def _backfill_llm(self, trace: dict, query: str,
                      decision_input: dict, monitoring_input: dict) -> None:
        """后台补写：QA 摘要 + 决策解读 + 合并（监控解读+经验总结）。任何失败只记录。"""
        try:
            from mataof.agents.query_analysis.llm_enhance import (
                build_llm_analysis as qa_build,
            )
            trace["analysis"]["llm_analysis"] = qa_build(query, trace["analysis"], self.llm)

            from mataof.agents.optimization_decision.context import normalize_input
            from mataof.agents.optimization_decision.llm_enhance import (
                build_llm_analysis as dec_build,
            )
            candidates = normalize_input(decision_input).candidates
            trace["decision"]["llm_analysis"] = dec_build(
                self.llm, decision_input, trace["decision"], candidates)

            from mataof.agents.execution_monitoring.llm_enhance import (
                build_post_execution_llm,
            )
            combined = build_post_execution_llm(self.llm, monitoring_input,
                                                trace["monitoring"])
            trace["monitoring"]["llm_analysis"] = {
                "available": (combined["performance_summary"] is not None
                              or combined["anomaly_summary"] is not None),
                "performance_summary": combined["performance_summary"],
                "anomaly_summary": combined["anomaly_summary"],
                "notes": combined["notes"] + ["异步生成（B4）"],
            }
            # 经验总结只补写 trace，不写入知识库记录（append-only 原则）
            if combined["experience_note"]:
                trace["knowledge_update"]["llm_analysis"] = {
                    "available": True,
                    "experience_note": combined["experience_note"],
                    "notes": ["异步生成；未写入知识库记录（append-only 原则）"],
                }
        except Exception as exc:  # 后台失败绝不拖垮主链路
            trace.setdefault("llm_backfill_notes", []).append(
                f"异步增强失败：{type(exc).__name__}")
        finally:
            self._rewrite_trace_file(trace)

    def _rewrite_trace_file(self, trace: dict) -> None:
        """回写原 trace 文件（异步补写结果；路径来自 _write_trace 的记录）。"""
        with self._seq_lock:
            path = self._trace_paths.get(trace["query_id"])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(trace, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def wait_llm_tasks(self, timeout_s: Optional[float] = None) -> int:
        """等待全部异步增强任务完成（批次结束/需要完整 llm_analysis 时调用）。"""
        with self._llm_futures_lock:
            futures = list(self._llm_futures)
        for fut in futures:
            try:
                fut.result(timeout=timeout_s)
            except Exception:
                pass
        with self._llm_futures_lock:
            self._llm_futures = [f for f in self._llm_futures if not f.done()]
        return len(futures)

    # ------------------------------------------------------------------
    # 批量运行
    # ------------------------------------------------------------------
    def run_statements(self, statements: list, source: str = "",
                       sampling: Optional[dict] = None, workers: int = 1) -> list:
        """运行一组语句；source 为来源文件/名称，sampling 为采样说明（写入追踪）。

        workers>1 时用线程池并行处理（B2）：各查询的 LLM 调用可并发（vLLM
        continuous batching），知识库/执行层已做线程安全处理；结果按输入顺序返回。
        批次结束后 flush 知识库（批量落盘模式下保证数据持久化）。
        """
        workers = max(1, int(workers))
        if workers == 1:
            traces = [self.run_query(s, source=source, sampling=sampling)
                      for s in statements]
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as pool:
                traces = list(pool.map(
                    lambda s: self.run_query(s, source=source, sampling=sampling),
                    statements,
                ))
        self.wait_llm_tasks()   # B4：批次结束排空异步增强任务
        self.km.store.flush()
        return traces

    def run_file(self, path: str, per_file_limit: Optional[int] = None,
                 sample_mode: Optional[str] = None,
                 sample_seed: Optional[int] = None,
                 workers: int = 1) -> list:
        """运行查询文件：.sql 按分号拆分语句；.jsonl 每行 {"query_id", "query"}。

        按配置（或参数覆盖）做每文件采样：per_file_limit 条、seq/random 模式、
        随机种子 sample_seed（同种子可复现）。采样说明写入每条追踪与汇总日志。
        workers：并行处理查询数（B2，默认 1 串行）。
        """
        source = os.path.basename(path)
        limit = per_file_limit if per_file_limit is not None else self.config.per_file_limit
        mode = sample_mode if sample_mode is not None else self.config.sample_mode
        seed = sample_seed if sample_seed is not None else self.config.sample_seed
        rng = random.Random(seed)   # 每文件独立随机流（跨文件同种子同种子序列）

        traces: list = []
        if path.endswith(".jsonl"):
            items: list = []
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    items.append(json.loads(line))
            picked, sampling = sample_items(items, limit, mode, rng, seed)
            for item in picked:
                traces.append(self.run_query(
                    str(item.get("query") or ""), str(item.get("query_id") or ""),
                    source=source, sampling=sampling,
                ))
            self.wait_llm_tasks()
            self.km.store.flush()
            return traces
        with open(path, encoding="utf-8") as f:
            text = f.read()
        statements, sampling = sample_items(split_sql_statements(text), limit, mode, rng, seed)
        return self.run_statements(statements, source=source, sampling=sampling,
                                   workers=workers)

    # ------------------------------------------------------------------
    # D3：固定策略对照（Baseline 2，跳过决策 Agent）
    # ------------------------------------------------------------------
    def _fixed_decision(self, analysis: dict, query_id: str) -> dict:
        """构造固定策略的决策输出（决策链路其余部分复用，保证口径一致）。"""
        from mataof.schemas import optimization_decision_output_template
        from mataof.agents.optimization_decision.catalog import (
            DIMENSIONS, NOT_APPLICABLE, STRATEGY_CATALOG,
        )
        out = optimization_decision_output_template()
        out["query_id"] = query_id
        features = analysis.get("query_features") or {}
        t = features.get("time") or {}
        agg = features.get("aggregation") or {}
        counts = (features.get("filter") or {}).get("type_counts") or {}
        non_time = sum(v for k, v in counts.items() if k != "time")
        gbd = agg.get("group_by_details") or {}
        applicable = {
            "time_pruning": bool(t.get("has_time_filter")) or gbd.get("type") == "time_window",
            "filter_order": non_time >= 2,
            "aggregation_placement": bool(agg.get("has_aggregation")),
        }
        parts = []
        for dim in DIMENSIONS:
            fixed = (self.config.fixed_strategy or {}).get(dim)
            if applicable[dim] and fixed in STRATEGY_CATALOG[dim]:
                out["decision"][dim] = {
                    "strategy": fixed,
                    "reason": f"固定策略对照（Baseline 2）：{fixed}",
                    "confidence": 1.0,
                    "equivalent_sql": None,
                }
                parts.append(f"{dim}={fixed}")
            else:
                out["decision"][dim] = {
                    "strategy": NOT_APPLICABLE,
                    "reason": "维度不适用或未配置固定策略",
                    "confidence": 0.0,
                    "equivalent_sql": None,
                }
                parts.append(f"{dim}={NOT_APPLICABLE}")
        out["selected_strategy"]["strategy_id"] = "|".join(parts)
        out["selected_strategy"]["strategy_parameters"] = {}
        out["selected_strategy"]["equivalent_sql"] = None
        out["decision_status"] = "fixed_strategy"
        out["notes"] = ["fixed_strategy 模式：跳过决策 Agent，执行固定策略组合（Baseline 2 对照）"]
        out["evidence"]["query_features"] = ["fixed_strategy 模式不使用决策证据"]
        out["evidence"]["historical_records"] = [
            {"record_id": None,
             "summary": {"considered": 0, "used": 0,
                         "below_similarity_threshold": 0, "invalid": 0}}]
        out["overall_confidence"] = 1.0
        return out

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
    def _write_trace(self, trace: dict) -> Optional[str]:
        if not self.config.results_dir:
            return None
        os.makedirs(self.config.results_dir, exist_ok=True)
        query_id = trace["query_id"]
        # 完整追踪记录（防同名覆盖：已存在则追加序号，绝不覆盖历史结果）
        base = os.path.join(self.config.results_dir, query_id)
        path, n = f"{base}.json", 1
        while os.path.exists(path):
            n += 1
            path = f"{base}-{n}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(trace, f, ensure_ascii=False, indent=2)
        # 汇总行（追加，实验分析用）
        decision = trace["decision"]
        mon = trace["monitoring"]
        summary = {
            "run_id": self.run_id,
            "query_id": query_id,
            "source": trace.get("source") or "",
            "sampling": trace.get("sampling"),
            "query_type": trace["analysis"].get("query_type"),
            "strategy_id": decision["selected_strategy"]["strategy_id"],
            "decision_status": decision["decision_status"],
            "execution_status": mon["execution_status"],
            "performance_assessment": mon["performance_assessment"],
            "anomaly_count": len(mon["anomalies"]),
            "response_time_ms": mon["metrics"]["response_time_ms"],
            "baseline_response_time_ms": (
                trace["execution"].get("baseline_metrics") or {}
            ).get("response_time_ms"),
            "cpu_utilization": mon["metrics"].get("cpu_utilization"),
            "cpu_utilization_peak": mon["metrics"].get("cpu_utilization_peak"),
            "memory_utilization": mon["metrics"].get("memory_utilization"),
            "memory_utilization_peak": mon["metrics"].get("memory_utilization_peak"),
            "knowledge_confidence": trace["knowledge_retrieval"]["knowledge_confidence"],
            "stored": trace["knowledge_update"]["stored"],
        }
        with open(os.path.join(self.config.results_dir, "run_log.jsonl"),
                  "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        return path

    def append_batch_row(self, total_time_s: float, total_queries: int) -> None:
        """把本批次墙钟总用时以 batch 行追加进 run_log（最终汇总据此算总用时/吞吐）。"""
        if not self.config.results_dir:
            return
        row = {"batch": True, "run_id": self.run_id, "query_id": None,
               "batch_total_time_s": round(total_time_s, 2),
               "batch_queries": int(total_queries)}
        with open(os.path.join(self.config.results_dir, "run_log.jsonl"),
                  "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


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
        per_file_limit=data.get("per_file_limit"),
        sample_mode=str(data.get("sample_mode") or "seq"),
        sample_seed=int(data.get("sample_seed") or 42),
        knowledge_max_records=int(data.get("knowledge_max_records") or 100),
        eval_prior_strength=float(data.get("eval_prior_strength") or 0.10),
        eval_min_count=int(data.get("eval_min_count") or 3),
        fixed_strategy=data.get("fixed_strategy"),
        knowledge_save_interval=int(data.get("knowledge_save_interval") or 1),
    )
