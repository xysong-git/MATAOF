"""Knowledge Memory Agent（知识记忆智能体）。

核心职责：
    负责历史查询、查询特征、优化策略、数据库状态、系统状态以及实际执行结果的
    结构化存储、检索、关联与更新，为 Optimization Decision Agent 提供历史决策依据。
    职责不是直接决定优化策略。

严格限制（严格遵守）：
    - 禁止编造历史查询、编造执行结果；
    - 禁止修改真实历史数据（存储为 append-only，无修改/删除接口）；
    - 禁止删除与当前决策不一致的历史记录；
    - 不把单次实验结果当作绝对规律——历史样本保留，评价随累计结果更新；
    - 不直接决定当前优化策略（检索结果仅作为决策 Agent 的证据输入）。

用法：
    km = KnowledgeMemoryAgent(store_path="data/knowledge_store.json")  # 可持久化
    # 更新（Execution Monitoring Agent 反馈 → 知识入库）
    km.update({"query_analysis": ..., "decision": ..., "monitoring": ..., "timestamp": ...})
    # 检索（新查询 → 历史证据）
    result = km.retrieve(query_analysis, database_state=..., system_state=...)
    # 供决策 Agent 使用
    decide({"query_analysis": ..., "historical_records": result["matched_records_for_decision"]})
"""

from __future__ import annotations

import time
from typing import Optional

from mataof.runtime import (
    Act,
    AgentContext,
    AgentRuntime,
    Finish,
    Goal,
    ToolRegistry,
)
from mataof.agents.knowledge_memory.store import MemoryStore
from mataof.agents.knowledge_memory.record import assemble_record, experience_key
from mataof.agents.knowledge_memory.retrieval import retrieve as _retrieve
from mataof.agents.knowledge_memory.llm_enhance import (
    build_retrieve_llm_analysis,
    build_update_llm_analysis,
    request_experience_note,
)

# 策略优先级变化判定阈值（累计成功率变化超过该值才报告方向）
PRIORITY_CHANGE_DELTA = 0.1

_AGENT_DEFAULTS = {
    "enabled": False,
    "min_strong": 1,          # 强匹配条数达到该值即认为证据充分
    "max_rounds": 2,          # 最多检索轮数（含首轮）
    "expand_factor": 2,       # 每轮 top-K 扩大倍数
    "max_records_cap": None,  # top-K 上限（None → 首轮 K × 倍数，或 200）
    "deadline_ms": 2000.0,
}


def _normalize_agent_config(raw: object,
                            initial_max_records: Optional[int]) -> dict:
    """校验/钳制检索 Agent 配置；cap 至少覆盖首轮 K。"""
    cfg = dict(_AGENT_DEFAULTS)
    if isinstance(raw, dict):
        if "enabled" in raw:
            cfg["enabled"] = bool(raw["enabled"])
        value = raw.get("min_strong")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            cfg["min_strong"] = value
        value = raw.get("max_rounds")
        if isinstance(value, int) and not isinstance(value, bool):
            cfg["max_rounds"] = max(1, min(4, value))
        value = raw.get("expand_factor")
        if isinstance(value, int) and not isinstance(value, bool):
            cfg["expand_factor"] = max(2, min(8, value))
        value = raw.get("max_records_cap")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            cfg["max_records_cap"] = value
        value = raw.get("deadline_ms")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            cfg["deadline_ms"] = max(1.0, float(value))

    initial = int(initial_max_records) if initial_max_records else None
    cap = cfg["max_records_cap"]
    if cap is None:
        cap = (initial * cfg["expand_factor"]) if initial else 200
    if initial is not None:
        cap = max(int(cap), initial)
    cfg["max_records_cap"] = int(cap)
    return cfg


class KnowledgeMemoryAgent:
    """知识记忆智能体。"""

    name = "knowledge_memory"
    description = "历史查询/策略/执行结果的结构化存储、检索、关联与更新。"

    def __init__(self, store_path: Optional[str] = None,
                 llm: Optional[object] = None, save_interval: int = 1,
                 backend: str = "json"):
        """store_path=None 时仅内存保存；给定路径时按 backend 持久化。

        backend：持久化层实现（持久化层改造实验）——
          "json"   整库 JSON 快照（现状，默认）；
          "jsonl"  追加式 JSON Lines + 元数据小文件（写入 O(新增)）；
          "sqlite" SQLite 增量 INSERT（事务提交，写入 O(新增)）。
        三者同协议、同内存结构、同检索语义（等价性由测试保证）。

        llm：可选 LLMClient（混合增强模式）——只生成 llm_analysis 附加节与
        经验总结（llm_note），不影响检索与入库的确定性核心；失败自动兜底。
        save_interval：批量落盘间隔（B5，默认 1=每次保存；大库实验建议 20~50）。
        """
        if backend == "jsonl":
            from mataof.agents.knowledge_memory.jsonl_store import JSONLStore
            self.store = JSONLStore(store_path, save_interval=save_interval)
        elif backend == "sqlite":
            from mataof.agents.knowledge_memory.sqlite_store import SQLiteStore
            self.store = SQLiteStore(store_path, save_interval=save_interval)
        else:
            self.store = MemoryStore(store_path, save_interval=save_interval)
        self.llm = llm
        # 形状级经验总结缓存（方案一）：同一查询形状只调用一次 LLM，
        # 后续执行复用；键见 record.experience_key（不含精确 time_span）。
        self._experience_notes: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def retrieve(self, query_analysis: dict, database_state: Optional[dict] = None,
                 system_state: Optional[dict] = None, query_id: str = "",
                 llm_summary: bool = True,
                 max_records: Optional[int] = None,
                 summary: bool = True,
                 timing_out: Optional[dict] = None) -> dict:
        """检索与当前查询上下文相关的历史知识（三层匹配 + 汇总 + 模式）。

        llm_summary=False 时跳过 LLM 解读（如 runner 链路：决策只消费结构化记录，
        每条查询省一次 LLM 调用）；无匹配记录时同样跳过（无内容可解读）。
        max_records 为返回记录上限（top-K；None 不截断，API 默认）。
        summary=False 为决策路径（runner 使用）：跳过全匹配集聚合
        （historical_summary 置空、summary_skipped=True），检索成本与库大小
        近乎解耦；决策消费字段与完整路径逐字段一致（等价性由测试保证）。
        timing_out：可选 dict，写入细粒度检索计时（不影响返回值）。
        """
        result = _retrieve(self.store, query_analysis, database_state, system_state,
                           query_id, max_records=max_records, summary=summary,
                           timing_out=timing_out)
        if not llm_summary:
            result["llm_analysis"] = {
                "available": False, "semantic_summary": None,
                "notes": ["llm_summary=false，跳过 LLM 解读"],
            }
        elif not result.get("matched_records"):
            result["llm_analysis"] = {
                "available": False, "semantic_summary": None,
                "notes": ["无匹配历史记录，跳过 LLM 解读"],
            }
        else:
            result["llm_analysis"] = build_retrieve_llm_analysis(
                self.llm, result, query_analysis)
        return result

    # ------------------------------------------------------------------
    # Agent 闭环：检索 → 观察证据充分性 →（不足时）扩大 top-K → 终止
    # ------------------------------------------------------------------
    def run_retrieve(self, query_analysis: dict,
                     database_state: Optional[dict] = None,
                     system_state: Optional[dict] = None, query_id: str = "",
                     llm_summary: bool = True,
                     max_records: Optional[int] = None,
                     summary: bool = True,
                     agent_config: Optional[dict] = None) -> dict:
        """自适应检索：只在"强证据不足且仍有未展开记录"时扩大 top-K。

        与 `retrieve()` 的区别是**检索规模由观察决定**：首轮使用调用方的
        top-K；若强匹配少于 `min_strong` 且返回值显示还有更多匹配记录被截断，
        则以 `expand_factor` 扩大 top-K 再检索；强证据充足、无更多记录、
        达到轮数/上限/截止时间时终止。每轮使用 llm_summary=False 避免重复
        调用，最终只生成一次 LLM 解读。

        返回 {"retrieval": <retrieve 输出>, "agent": <检索元数据>}。
        """
        initial_k = (int(max_records)
                     if max_records is not None and int(max_records) > 0 else None)
        cfg = _normalize_agent_config(agent_config, initial_k)
        state = {
            "query_analysis": query_analysis,
            "database_state": database_state,
            "system_state": system_state,
            "query_id": query_id,
            "llm_summary": bool(llm_summary),
            "summary": bool(summary),
            "cfg": cfg,
            "initial_k": initial_k,
            "current_k": initial_k,
            "rounds": 0,
            "round_log": [],
            "retrieval_ms": [],
            "last": None,
            "terminal_reason": None,
            "llm_summary_used": False,
            "llm_skip_reason": None,
            "error": None,
        }

        tools = ToolRegistry()

        def retrieve_round(max_records: Optional[int]) -> dict:
            timing: dict = {}
            result = self.retrieve(
                query_analysis,
                database_state=database_state,
                system_state=system_state,
                query_id=query_id,
                llm_summary=False,   # 每轮不调用；最终统一一次
                max_records=max_records,
                summary=summary,
                timing_out=timing,
            )
            return {"retrieval": result, "timing": timing}

        def llm_summarize() -> dict:
            return build_retrieve_llm_analysis(
                self.llm, state["last"] or {}, query_analysis)

        tools.register(
            "retrieve_round", retrieve_round,
            description="按给定 top-K 检索历史知识（三层匹配 + 匹配计数）",
            parameters={"required": ["max_records"],
                        "properties": {"max_records": {"type": ["integer", "null"]}}},
            side_effect="read", cost="low",
        )
        tools.register(
            "llm_summarize", llm_summarize,
            description="对最终检索结果生成一次 LLM 解读（只写 llm_analysis）",
            parameters={"required": [], "properties": {}},
            side_effect="none", cost="high",
        )

        runtime = AgentRuntime(self.name, tools, self._agent_policy)
        run_result = runtime.run(
            Goal(name="retrieve_knowledge",
                 max_steps=int(cfg["max_rounds"]) + 2,
                 deadline_ms=cfg["deadline_ms"]),
            AgentContext(data=state),
        )
        payload = run_result.result
        if not isinstance(payload, dict):
            payload = self._agent_retrieve_payload(state)
        meta = payload.setdefault("agent", {})
        meta.update({
            "steps": run_result.steps,
            "terminated_by": run_result.terminated_by,
            "elapsed_ms": round(run_result.elapsed_ms, 3),
            "trace": run_result.trace,
        })
        return payload

    def _agent_policy(self, ctx: AgentContext, tools: ToolRegistry):
        state = ctx.data
        obs = ctx.last_observation

        if obs is None:
            return Act("retrieve_round",
                       {"max_records": state["initial_k"]},
                       reason="首轮检索（调用方 top-K）")

        if obs.tool == "retrieve_round":
            if not obs.ok:
                state["error"] = obs.error
                state["last"] = self._empty_retrieval(state, obs.error)
                state["terminal_reason"] = "retrieve_failed"
                state["llm_skip_reason"] = "retrieve_failed"
                return self._finish(state, "retrieve_failed")

            value = obs.value if isinstance(obs.value, dict) else {}
            result = value.get("retrieval")
            if not isinstance(result, dict):
                state["error"] = "检索工具未返回结果"
                state["last"] = self._empty_retrieval(
                    state, "检索工具未返回结果")
                state["terminal_reason"] = "retrieve_failed"
                state["llm_skip_reason"] = "retrieve_failed"
                return self._finish(state, "retrieve_failed")

            state["last"] = result
            state["rounds"] += 1
            timing = value.get("timing") or {}
            if isinstance(timing.get("total_ms"), (int, float)):
                state["retrieval_ms"].append(timing["total_ms"])
            counts = result.get("match_counts") or {}
            state["round_log"].append({
                "round": state["rounds"],
                "max_records": state["current_k"],
                "strong": counts.get("strong"),
                "moderate": counts.get("moderate"),
                "weak": counts.get("weak"),
                "truncated": result.get("truncated"),
                "knowledge_confidence": result.get("knowledge_confidence"),
            })
            return self._decide_next(state, result)

        if obs.tool == "llm_summarize":
            result = state["last"] or {}
            if obs.ok and isinstance(obs.value, dict):
                result["llm_analysis"] = obs.value
                state["llm_summary_used"] = True
                return self._finish(state, "llm_summarized")
            result["llm_analysis"] = {
                "available": False, "semantic_summary": None,
                "notes": [f"LLM 解读工具失败：{obs.error}"],
            }
            state["llm_skip_reason"] = "llm_tool_failed"
            return self._finish(state, "llm_failed")

        return self._finish(state, "unexpected_tool")

    def _decide_next(self, state: dict, result: dict):
        """根据本轮观察决定继续扩大 top-K 还是终止。"""
        cfg = state["cfg"]
        counts = result.get("match_counts") or {}
        matched = sum(int(counts.get(k) or 0)
                      for k in ("strong", "moderate", "weak"))
        # 充分性按**实际返回给决策的证据**衡量：match_counts 是全局匹配计数，
        # 但 top-K 截断后决策只能看到 listed 记录；全局强匹配很多而 K 很小时，
        # 不应据此宣称证据充分。
        listed_strong = sum(
            1 for m in result.get("matched_records") or []
            if m.get("match_level") == "strong_match")

        if listed_strong >= int(cfg["min_strong"]):
            return self._finish_or_llm(state, "sufficient")
        if matched == 0:
            return self._finish_or_llm(state, "no_match")
        if not result.get("truncated"):
            # 所有匹配记录都已返回：扩大 K 不会产生新证据。
            return self._finish_or_llm(state, "exhausted")
        if state["rounds"] >= int(cfg["max_rounds"]):
            return self._finish_or_llm(state, "max_rounds")

        current = state["current_k"]
        if current is None:
            return self._finish_or_llm(state, "exhausted")
        next_k = min(int(cfg["max_records_cap"]),
                     int(current) * int(cfg["expand_factor"]))
        if next_k <= int(current):
            return self._finish_or_llm(state, "cap")

        state["current_k"] = next_k
        return Act(
            "retrieve_round", {"max_records": next_k},
            reason=(f"返回的强匹配 {listed_strong} < {cfg['min_strong']} 且仍有"
                    f"未展开记录 → top-K {current}→{next_k}"),
        )

    def _finish_or_llm(self, state: dict, reason: str):
        """终止前决定是否做一次 LLM 解读（无内容/未启用/已禁用则跳过）。"""
        state["terminal_reason"] = reason
        result = state["last"] or {}
        if not state["llm_summary"]:
            result["llm_analysis"] = self._no_llm_section(
                "llm_summary=false，跳过 LLM 解读")
            state["llm_skip_reason"] = "llm_summary_disabled"
            return self._finish(state, reason)
        if not result.get("matched_records"):
            result["llm_analysis"] = self._no_llm_section(
                "无匹配历史记录，跳过 LLM 解读")
            state["llm_skip_reason"] = "no_match"
            return self._finish(state, reason)
        if self.llm is None:
            result["llm_analysis"] = build_retrieve_llm_analysis(
                None, result, state["query_analysis"])
            state["llm_skip_reason"] = "llm_unavailable"
            return self._finish(state, reason)
        return Act("llm_summarize", {}, reason="对最终检索结果生成一次 LLM 解读")

    def _finish(self, state: dict, reason: str):
        return Finish(result=self._agent_retrieve_payload(state), reason=reason)

    @staticmethod
    def _no_llm_section(reason: str) -> dict:
        return {"available": False, "semantic_summary": None, "notes": [reason]}

    def _empty_retrieval(self, state: dict, error: Optional[str]) -> dict:
        """检索失败时的诚实兜底：不声称"无匹配"，所有证据字段置 null。"""
        return {
            "query_id": state.get("query_id") or "",
            "matched_records": [],
            "historical_summary": {
                "successful_strategies": [],
                "failed_strategies": [],
                "strategy_statistics": {},
            },
            "knowledge_confidence": None,
            "matched_records_for_decision": [],
            "match_counts": {"strong": None, "moderate": None, "weak": None},
            "match_executions": {"strong": None, "moderate": None, "weak": None},
            "truncated": None,
            "strategy_evaluations": {},
            "summary_skipped": not state.get("summary", True),
            "retrieval_ok": False,
            "retrieval_error": error,
            "llm_analysis": self._no_llm_section(
                "检索失败，未生成 LLM 解读"),
        }

    @staticmethod
    def _agent_retrieve_payload(state: dict) -> dict:
        cfg = state.get("cfg") or _AGENT_DEFAULTS
        result = state.get("last")
        if not isinstance(result, dict):
            result = {
                "query_id": state.get("query_id") or "",
                "matched_records": [],
                "historical_summary": {
                    "successful_strategies": [], "failed_strategies": [],
                    "strategy_statistics": {},
                },
                "knowledge_confidence": None,
                "matched_records_for_decision": [],
                "match_counts": {"strong": None, "moderate": None, "weak": None},
                "match_executions": {"strong": None, "moderate": None, "weak": None},
                "truncated": None,
                "strategy_evaluations": {},
                "summary_skipped": not state.get("summary", True),
                "retrieval_ok": False,
                "retrieval_error": state.get("error"),
                "llm_analysis": KnowledgeMemoryAgent._no_llm_section(
                    "检索未完成，未生成 LLM 解读"),
            }
        timings = [round(float(v), 3) for v in state.get("retrieval_ms") or []]
        return {
            "retrieval": result,
            "agent": {
                "enabled": True,
                "runtime": "mataof.runtime",
                "policy": "adaptive_topk",
                "min_strong": int(cfg.get("min_strong") or 0),
                "max_rounds": int(cfg.get("max_rounds") or 1),
                "expand_factor": int(cfg.get("expand_factor") or 2),
                "max_records_cap": int(cfg.get("max_records_cap") or 0),
                "initial_max_records": state.get("initial_k"),
                "final_max_records": state.get("current_k"),
                "rounds": int(state.get("rounds") or 0),
                "topk_rounds": list(state.get("round_log") or []),
                "retrieval_termination": state.get("terminal_reason"),
                "retrieval_ms": timings,
                "retrieval_ms_total": round(sum(timings), 3),
                "llm_summary": bool(state.get("llm_summary")),
                "llm_summary_used": bool(state.get("llm_summary_used")),
                "llm_skip_reason": state.get("llm_skip_reason"),
                "error": state.get("error"),
            },
        }

    # ------------------------------------------------------------------
    # 更新（反馈入库）
    # ------------------------------------------------------------------
    def update(self, feedback_input: dict,
               experience_note: Optional[str] = None,
               use_llm: bool = True,
               llm: Optional[object] = None) -> dict:
        """把一次完整执行（分析 + 决策 + 监控反馈）关联为历史记录并入库。

        experience_note：预生成的经验总结（旧合并路径：runner 已用一次
        LLM 调用生成，此处直接使用，不再调 LLM）。
        use_llm=True（B/方案一）：按查询形状生成经验总结——同一形状只在
        首次执行时调用一次 LLM，后续复用（形状键见 record.experience_key），
        不再逐执行生成。
        use_llm=False：跳过本方法的 LLM 调用（llm_shared=false 旧路径）。
        llm：本次查询实际可用的 LLM 客户端（None → 回退 self.llm）；
        采样未命中时传 use_llm=False，避免绕过采样控制。
        返回规格输出（update_type / record_id / stored / strategy_effect /
        knowledge_update），notes 为扩展说明。
        """
        notes: list[str] = []
        record, rejected = assemble_record(feedback_input, notes)
        if record is None:
            return {
                "update_type": rejected or "rejected_invalid",
                "record_id": "",
                "stored": False,
                "strategy_effect": "",
                "knowledge_update": {
                    "success_record": False,
                    "failure_record": False,
                    "strategy_priority_change": None,
                },
                "notes": notes,
                "llm_analysis": {"available": False, "experience_note": None,
                                 "notes": ["LLM 未启用或记录未入库"]},
            }

        # LLM 经验总结（llm_generated 元数据；失败 → 不写入，事实记录不变）
        # 方案一：按查询形状收口——同一形状首次执行生成一次，后续复用；
        # 旧合并路径（experience_note 已给出）直接使用，不重复调用。
        effect = record["execution_result"]
        key = experience_key(feedback_input)
        reused = False
        llm_ms = 0.0
        llm_notes: list[str] = []
        note_llm = llm if llm is not None else self.llm
        if experience_note is None and use_llm:
            cached = self._experience_notes.get(key)
            if cached is not None:
                experience_note = cached
                reused = True
                llm_notes.append("同形状经验总结复用（形状键命中，未重复调用 LLM）")
            else:
                t_llm = time.perf_counter()
                experience_note = request_experience_note(
                    note_llm, feedback_input,
                    {"stored": True, "strategy_effect": effect}, llm_notes)
                llm_ms = round((time.perf_counter() - t_llm) * 1000.0, 3)
                if experience_note is not None:
                    self._experience_notes[key] = experience_note
        elif experience_note is None:
            llm_notes.append("runner 合并路径：本次未生成 LLM 经验总结（未采样或调用失败）")
        if experience_note is not None:
            record["llm_note"] = experience_note

        # 累计统计（入库前 vs 入库后）——历史样本保留，评价随累计更新
        sid = record["strategy"]["strategy_id"]
        before = self.store.statistics().get(sid, {})
        record_id = self.store.add(record)
        after = self.store.statistics().get(sid, {})
        notes.extend(self._post_store_notes(record_id))
        # 策略评价表更新（D1：反馈驱动的动态权重，派生聚合）
        eval_updates = self.store.record_evaluation(record)

        failure_record = (
            effect in ("degraded", "failed")
            or record["execution"]["status"] in ("failed", "timeout")
            or any(a.get("severity") == "critical" for a in record["anomalies"])
        )
        success_record = effect == "improved"

        priority_change = None
        b_ratio = before.get("success_ratio") if before else None
        a_ratio = after.get("success_ratio") if after else None
        # 无方向明确样本（或全为 unchanged）时 success_ratio 为 None
        # （证据不足/中性结果不计入分母）→ 不计算变化
        if (sid and isinstance(b_ratio, (int, float)) and not isinstance(b_ratio, bool)
                and isinstance(a_ratio, (int, float)) and not isinstance(a_ratio, bool)):
            delta = a_ratio - b_ratio
            if delta >= PRIORITY_CHANGE_DELTA:
                priority_change = "increased"
            elif delta <= -PRIORITY_CHANGE_DELTA:
                priority_change = "decreased"
            if priority_change:
                notes.append(
                    f"策略 {sid} 累计成功率 {b_ratio} → {a_ratio}，"
                    f"优先级变化：{priority_change}"
                )

        llm_analysis = build_update_llm_analysis(experience_note, llm_notes)
        llm_analysis["experience_key"] = key
        llm_analysis["note_reused"] = reused
        llm_analysis["llm_ms"] = llm_ms
        return {
            "update_type": "new_record",
            "record_id": record_id,
            "stored": True,
            "strategy_effect": effect,
            "knowledge_update": {
                "success_record": success_record,
                "failure_record": failure_record,
                "strategy_priority_change": priority_change,
            },
            "notes": notes,
            "llm_analysis": llm_analysis,
            "strategy_evaluation_updates": eval_updates,   # 扩展：评价表增量
        }

    def _post_store_notes(self, record_id: str) -> list:
        return [f"历史记录已追加存储：{record_id}（append-only，不修改/删除既有记录）"]

    # ------------------------------------------------------------------
    # 全库统计
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return {
            "total_records": self.store.count(),
            "strategy_statistics": self.store.statistics(),
        }


# 默认单例（内存存储）
default_agent = KnowledgeMemoryAgent()


def retrieve(query_analysis: dict, database_state: Optional[dict] = None,
             system_state: Optional[dict] = None, query_id: str = "") -> dict:
    """Knowledge Memory Agent 检索的便捷入口。"""
    return default_agent.retrieve(query_analysis, database_state, system_state, query_id)


def update(feedback_input: dict) -> dict:
    """Knowledge Memory Agent 更新的便捷入口。"""
    return default_agent.update(feedback_input)
