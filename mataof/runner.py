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

import hashlib
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from mataof.agents.query_analysis import analyze
from mataof.agents.query_analysis import run_agent as run_query_analysis_agent
from mataof.agents.optimization_decision import decide, run_agent
from mataof.agents.execution_monitoring import (
    llm_interpretation_needed,
    monitor,
)
from mataof.agents.execution_monitoring import run_agent as run_monitoring_agent
from mataof.agents.knowledge_memory import KnowledgeMemoryAgent
from mataof.executors import build_executor
from mataof.executors.base import ExecutionResult, Executor
from mataof.llm import build_llm_client
from mataof.runtime import AgentBus, AgentMessage
from mataof.similarity import (baseline_sample_adequate, build_history_evidence,
                               compact_features, same_shape_evidence)


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


def load_device_inventory(data: Any = None,
                          path: Optional[str] = None) -> Any:
    """设备清单加载（通配 FROM 展开用）；无效输入 → None（计划生成安全降级）。

    契约：list[str]（全局清单）或 dict[str, list[str]]（通配模式 → 清单）。
    文件读取失败不抛异常——设备分区计划不生成，其余计划照常工作。
    """
    inv = data
    if inv is None and path:
        try:
            with open(path, encoding="utf-8") as f:
                inv = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[runner] 注意：设备清单读取失败（{path}）：{exc}；"
                  f"通配查询不生成设备分区计划", flush=True)
            return None
    if inv is None:
        return None
    if isinstance(inv, dict):
        if all(isinstance(v, list) and all(isinstance(x, str) for x in v)
               for v in inv.values()):
            return {str(k): list(v) for k, v in inv.items()}
    elif isinstance(inv, list) and all(isinstance(x, str) for x in inv):
        return list(inv)
    print("[runner] 注意：设备清单格式无效（需 list[str] 或 dict[str, list[str]]）；"
          "通配查询不生成设备分区计划", flush=True)
    return None


def validate_device_inventory(inventory: Any,
                              count_devices: Any = None) -> tuple[Any, list[str]]:
    """用数据库真实设备数校验通配清单；不完整/无法校验的模式剔除。

    设备分区按清单展开子查询，清单缺设备会在合并结果里静默丢列
    （verify_merged 只按计划自身校验，发现不了），因此使用前必须核对
    COUNT DEVICES <pattern> 与清单长度：
    - 非 dict（全局 list）无法按模式核对 → 原样返回并说明；
    - 某个模式数量不一致/查询失败 → 仅剔除该模式（其余模式继续可用）；
    - 全部剔除 → None（设备/混合分区计划不生成，其余计划照常）。
    """
    notes: list[str] = []
    if not isinstance(inventory, dict):
        if inventory is not None:
            notes.append("清单不是 dict[pattern, list] 形式，无法按模式核对设备数；保持原样")
        return inventory, notes
    if not callable(count_devices):
        notes.append("执行器不支持设备数校验；保持原样")
        return inventory, notes
    kept: dict = {}
    for pattern, devices in inventory.items():
        try:
            n = count_devices(pattern)
        except Exception as exc:
            notes.append(f"{pattern}：校验查询失败（{type(exc).__name__}: "
                         f"{str(exc)[:160]}）；剔除该清单")
            continue
        if n is None:
            notes.append(f"{pattern}：数据库未返回设备数；剔除该清单")
            continue
        try:
            n = int(n)
        except (TypeError, ValueError):
            notes.append(f"{pattern}：设备数不是整数（{n!r}）；剔除该清单")
            continue
        if n != len(devices):
            notes.append(f"{pattern}：清单 {len(devices)} 台 ≠ 数据库 {n} 台；"
                         f"剔除该清单（避免设备分区丢数据）")
            continue
        kept[pattern] = devices
    if inventory and not kept:
        notes.append("所有通配模式均未通过校验；本次运行不生成设备/混合分区计划")
    return (kept or None), notes


_BASELINE_PLAN_ID = "baseline_single_df"

# 四个 Agent 的配置名（与 agents 配置节同名）
_AGENT_NAMES = ("query_analysis", "optimization_decision",
                "execution_monitoring", "knowledge_memory")


def resolve_agents_config(agents: Any, legacy_agent: Any = None) -> dict:
    """解析四 Agent 的启用状态与配置。

    - `agents.enabled` 是全局默认值；某个 Agent 自己声明 enabled 时优先生效
      （便于单 Agent 消融）；
    - 兼容旧配置 `agent`：等价于 `agents.optimization_decision`；
      两者的字段合并，新配置同名键优先；
    - 返回每个 Agent 的配置副本，保证含 `enabled` 布尔键。
    """
    raw = agents if isinstance(agents, dict) else {}
    default_enabled = bool(raw.get("enabled", False))
    legacy = legacy_agent if isinstance(legacy_agent, dict) else {}
    out: dict = {}
    for name in _AGENT_NAMES:
        section = raw.get(name)
        section = dict(section) if isinstance(section, dict) else {}
        if name == "optimization_decision":
            section = {**legacy, **section}
        if "enabled" not in section:
            section["enabled"] = default_enabled
        else:
            section["enabled"] = bool(section["enabled"])
        out[name] = section
    return out

# baseline 样本充分性口径见 mataof.similarity.baseline_sample_adequate
# （与决策成本模型共用）：少于 3 次或不足同计划证据的 10% → 不充分，
# 改用同计划（同查询形状 + 同执行计划）的实测历史。


def _plan_reference(ev: Any, plan_id: str, source: str,
                    scope: str = "similar_records") -> Optional[dict]:
    """从历史证据中取某计划的加权实测延迟；无有效样本 → None。"""
    lat = ev.weighted_latency("plan", plan_id)
    count = ev.count_for("plan", plan_id)
    if lat is None or lat <= 0 or count <= 0:
        return None
    return {
        "response_time_ms": round(float(lat), 3),
        "source": source,
        "plan_id": plan_id,
        "count": count,
        "scope": scope,
    }


def build_historical_reference(decision: Any, historical_records: Any,
                               reference_time: Optional[int] = None,
                               query_analysis: Optional[dict] = None,
                               database_state: Optional[dict] = None
                               ) -> Optional[dict]:
    """构造执行监控的历史参照（只取历史实测延迟，不使用成本先验）。

    优先使用 baseline 计划历史（衡量相对原查询的优化收益），但 baseline
    样本可能因选择性执行而稀少、偏冷启动。规则：
    1. baseline 样本充分（≥3 次且 ≥ 同查询形状全部计划证据的 10%）
       → baseline 历史，无论所选计划是什么；
    2. baseline 不充分/缺失，且所选计划有同计划历史
       → 同计划历史（同查询形状、同计划，口径一致且样本更密）；
    3. 否则 → 任意相似计划的加权实测延迟（典型表现）；
    4. 无任何实测延迟 → None（保持 insufficient_evidence，绝不编造参照值）。

    提供 query_analysis 时，若"同查询形状 + 同数据规模"的实测历史充分
    （≥3 次），参照只在该子集内构造——近邻形状（q3/q10 测点不同、
    q7/q11 窗口不同）的延迟不参与，避免参照被近邻形状稀释。

    返回字段：response_time_ms / source / plan_id / count / scope
    （source、scope 仅供追踪审计）。
    """
    dec = decision if isinstance(decision, dict) else {}
    ps = dec.get("plan_selection")
    ps = ps if isinstance(ps, dict) else {}
    baseline_plan_id = str(ps.get("baseline_plan_id") or _BASELINE_PLAN_ID)
    ss = dec.get("selected_strategy")
    plan = (ss or {}).get("execution_plan") if isinstance(ss, dict) else None
    selected_plan_id = plan.get("plan_id") if isinstance(plan, dict) else None

    records = historical_records if isinstance(historical_records, list) else []
    current_compact = (compact_features(query_analysis)
                       if isinstance(query_analysis, dict) else {})
    ev = None
    if records:
        # 与决策 Agent 使用同一套相似度/时效权重（记录自带预算相似度时直接复用）。
        ev = build_history_evidence(records, current_compact, database_state or {},
                                    {}, reference_time)
        if not ev.used:
            ev = None

    scope = "similar_records"
    if ev is not None and current_compact:
        same_ev = same_shape_evidence(ev, current_compact,
                                      database_state or {})
        if same_ev is not None:
            ev = same_ev
            scope = "same_shape"

    # baseline 参照：优先决策成本模型中的历史基线值（与决策预测同源），
    # 否则取相似历史中 baseline 计划的加权实测延迟。
    baseline_ref = None
    single_ms = ps.get("single_ms")
    if (ps.get("single_source") == "history_baseline"
            and isinstance(single_ms, (int, float))
            and not isinstance(single_ms, bool) and single_ms > 0):
        count = 0
        for e in ps.get("evaluations") or []:
            if isinstance(e, dict) and e.get("plan_id") == baseline_plan_id:
                count = int(e.get("history_count") or 0)
                break
        if count <= 0 and ev is not None:
            count = ev.count_for("plan", baseline_plan_id)
        baseline_ref = {
            "response_time_ms": round(float(single_ms), 3),
            "source": "history_baseline",
            "plan_id": baseline_plan_id,
            "count": count,
            "scope": scope,
        }
    elif ev is not None:
        baseline_ref = _plan_reference(ev, baseline_plan_id,
                                       "history_baseline_records", scope)

    same_ref = None
    if (ev is not None and selected_plan_id
            and str(selected_plan_id) != baseline_plan_id):
        same_ref = _plan_reference(ev, str(selected_plan_id),
                                   "history_same_plan", scope)

    # baseline 样本是否充分按"同查询形状全部计划证据"衡量（与决策成本模型
    # 同一口径），而不是只看同计划条数：baseline 是安全动作，样本稀少时
    # 无论所选计划是什么，都不能拿冷启动值当参照。
    total = ev.total_count("plan") if ev is not None else 0
    b_count = int(baseline_ref.get("count") or 0) if baseline_ref else 0
    baseline_ok = (baseline_ref is not None
                   and baseline_sample_adequate(b_count, total))
    if same_ref is not None:
        if baseline_ref is None:
            return same_ref
        return baseline_ref if baseline_ok else same_ref
    if baseline_ok:
        return baseline_ref
    if ev is None:
        return None

    items: list[tuple[float, float]] = []
    count = 0
    for u in ev.used:
        fb = u["record"].get("execution_feedback") or {}
        lat = fb.get("latency_ms")
        if (isinstance(lat, (int, float)) and not isinstance(lat, bool)
                and lat >= 0):
            items.append((u["weight"] * u["count"], float(lat)))
            count += u["count"]
    total_w = sum(w for w, _ in items)
    if not items or total_w <= 0:
        return None
    weighted = sum(w * lat for w, lat in items) / total_w
    return {
        "response_time_ms": round(weighted, 3),
        "source": "history_matched",
        "plan_id": None,
        "count": count,
        "scope": scope,
    }


@dataclass
class RunnerConfig:
    """PipelineRunner 配置。所有字段可缺省（安全默认）。"""

    knowledge_store: Optional[str] = None       # 知识库文件路径
    knowledge_backend: str = "json"             # 持久化层：json（整库快照）/ jsonl（追加式）/ sqlite（增量 INSERT）
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
    async_drain_timeout: float = 0.0            # B4：异步补写排空超时（0=不等待；>0 最多等 N 秒）
    # 执行计划（决策 Agent 内置能力）：Agent 按查询编译可执行计划并做
    # 成本/收益选择，把选中计划交给执行器。这里只配置事实输入与上限。
    device_inventory: Any = None                # 通配设备展开清单（list[str] 或 dict[pattern, list]）
    device_inventory_file: Optional[str] = None # 从 JSON 文件加载设备清单（device_inventory 优先）
    planning: dict = field(default_factory=dict)  # 决策 Agent 计划生成配置（enabled 等）
    # Agent 闭环（默认关闭）：决策 → 执行 → 观察 →（退化时）重规划。
    # {"enabled": true, "max_attempts": 2, "deadline_ms": 30000,
    #  "replan_on": ["degraded", "failed"]}
    # 兼容旧配置；等价于 agents.optimization_decision。
    agent: dict = field(default_factory=dict)
    # 四 Agent 运行时配置（默认全部关闭）：
    # {"enabled": false, "query_analysis": {...}, "optimization_decision": {...},
    #  "execution_monitoring": {...}, "knowledge_memory": {...}}
    agents: dict = field(default_factory=dict)
    plan_max_parallelism: int = 8               # 计划子查询最大并行度
    plan_min_saving_ms: Optional[float] = None  # 计划净收益门槛（缺省 → 成本模型默认）
    plan_min_saving_ratio: Optional[float] = None


class _BackfillFuture:
    """简化 Future：Event 通知 + result(timeout)/done（与 wait_llm_tasks 兼容）。"""

    def __init__(self):
        self._done = threading.Event()
        self._result = None
        self._exc = None

    def _set_result(self, r):
        self._result = r
        self._done.set()

    def _set_exception(self, e):
        self._exc = e
        self._done.set()

    def done(self) -> bool:
        return self._done.is_set()

    def result(self, timeout=None):
        if not self._done.wait(timeout):
            raise TimeoutError
        if self._exc is not None:
            raise self._exc
        return self._result


class _DaemonBackfillPool:
    """daemon 线程工作池：进程退出立即终止，不被未完成补写阻塞。

    ThreadPoolExecutor 的 worker 线程非 daemon：解释器退出时
    threading._shutdown 会先 join 全部非 daemon 线程（早于任何 atexit 钩子），
    大轮次下积压数小时的补写会让进程在汇总打印后"卡住"，cancel_futures 也
    无法生效。本池用 daemon 线程：退出即弃，未完成补写保持"未完成"语义
    （llm_analysis 不入库、确定性产物不受影响；重跑同查询集可命中 LLM 缓存）。
    """

    def __init__(self, workers: int, prefix: str = "llm-backfill"):
        import queue
        self._q: queue.Queue = queue.Queue()
        self._threads = [
            threading.Thread(target=self._work, name=f"{prefix}-{i}", daemon=True)
            for i in range(workers)]
        for t in self._threads:
            t.start()

    def _work(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            fn, fut = item
            try:
                fut._set_result(fn())
            except Exception as exc:   # 后台失败绝不拖垮主链路
                fut._set_exception(exc)

    def submit(self, fn):
        fut = _BackfillFuture()
        self._q.put((fn, fut))
        return fut

    def shutdown(self, wait: bool = False, cancel_futures: bool = False) -> None:
        pass   # daemon 线程退出即终止（接口与 ThreadPoolExecutor 对齐）


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
                                       save_interval=config.knowledge_save_interval,
                                       backend=config.knowledge_backend)
        self.executor: Executor = build_executor(config.executor)
        # Agent 配置（默认关闭 → 保持原确定性流水线；不影响既有实验）。
        # agents 是新的四 Agent 配置；旧 agent 节映射到 optimization_decision。
        self.agents_config = resolve_agents_config(config.agents, config.agent)
        self.agent_config = dict(self.agents_config["optimization_decision"])
        self._agent_enabled = bool(self.agent_config.get("enabled"))
        self._qa_agent_enabled = bool(
            self.agents_config["query_analysis"].get("enabled"))
        self._em_agent_enabled = bool(
            self.agents_config["execution_monitoring"].get("enabled"))
        self._km_agent_enabled = bool(
            self.agents_config["knowledge_memory"].get("enabled"))
        # 只要任一 Agent 启用，主链路就走消息总线（各端点按配置选择 Agent/
        # 确定性实现）；全部关闭时保持旧的直接调用路径。
        self._bus_enabled = bool(
            self._agent_enabled or self._qa_agent_enabled
            or self._em_agent_enabled or self._km_agent_enabled)
        # B（LLM 常驻）：同一 LLM 客户端按各 Agent 的 llm_policy 共享给
        # QA/EM/KM；默认关闭，保持旧路径（llm_primary 只给 OD）。
        agents_raw = config.agents if isinstance(config.agents, dict) else {}
        self._agents_llm_shared = bool(agents_raw.get("llm_shared", False))
        # 控制流（2×2 对照的“控制流”维度）：
        # - "agent"（默认）：观察驱动闭环——OD 可 replan、空证据可 recall、
        #   KM 可按观察扩 top-K；
        # - "pipeline"：固定流水线——单次决策/执行、固定 top-K 检索，
        #   不做 replan/recall/自适应扩 K；组件与 LLM 调用点保持一致。
        control = str(agents_raw.get("control") or "agent").strip().lower()
        if control not in ("agent", "pipeline"):
            print(f"[runner] 注意: agents.control={control!r} 非法，回退 agent",
                  flush=True)
            control = "agent"
        self._agents_control = control
        self._pipeline_mode = control == "pipeline"
        # 计划生成设备清单（通配 FROM 展开；None → 设备/混合分区计划不生成）
        self.device_inventory = load_device_inventory(
            config.device_inventory, config.device_inventory_file)
        # 完整性校验：清单缺设备会让设备分区静默丢列（verify_merged 发现不了）
        self.device_inventory, inventory_notes = validate_device_inventory(
            self.device_inventory, getattr(self.executor, "count_devices", None))
        for note in inventory_notes:
            print(f"[runner] 设备清单：{note}", flush=True)
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
        # LLM 参与决策（decision_mode="llm_primary"）：每条查询都要等 LLM 决策，
        # 必须同步且不可采样（缓存承担同质查询的加速）
        self._llm_decide = bool((config.llm or {}).get("decision_mode") == "llm_primary") \
            and self.llm is not None
        if self._llm_decide:
            if self._llm_async:
                self._llm_async = False
                print("[runner] 注意: decision_mode=llm_primary 开启，LLM 参与决策必须"
                      "同步，async_enhance 强制回退为同步模式", flush=True)
            if self._llm_every > 1:
                self._llm_every = 1
                print("[runner] 注意: decision_mode=llm_primary 下 sample_rate 强制为 1"
                      "（每条查询都需 LLM 决策；同质查询命中输出缓存）", flush=True)
        self._llm_workers = max(1, int((config.llm or {}).get("async_workers") or 2))
        self._llm_executor = None
        self._llm_futures: list = []
        self._llm_futures_lock = threading.Lock()
        self._trace_paths: dict = {}   # query_id → 实际落盘路径（异步回写用）

    def _llm_for_query(self) -> Optional[object]:
        """按采样率决定本条查询是否启用同步 LLM；未采样 → None（确定性快路径）。

        B4 异步模式下同步路径永远不用 LLM（后台统一补写）；采样逻辑同样适用于
        异步补写（只有被采样的查询才入队补写，控制后台队列规模）。
        """
        if self._llm_async:
            return None
        if self._llm_every <= 1:
            return self.llm
        return self.llm if (self._seq % self._llm_every) == 0 else None

    def _llm_sampled(self, seq: int) -> bool:
        """本条查询是否命中 LLM 采样（异步补写据此决定是否入队）。"""
        if self._llm_every <= 1:
            return True
        return seq % self._llm_every == 0

    def _post_llm_policy(self) -> str:
        """执行后 LLM 的触发策略（与 EM Agent 的 llm_policy 同口径）。"""
        section = self.agents_config.get("execution_monitoring") or {}
        policy = section.get("llm_policy") if isinstance(section, dict) else None
        return policy if isinstance(policy, str) and policy else "on_anomaly"

    def _control_flow_label(self) -> str:
        """本次运行的控制流口径（写入 trace/run_log，供 2×2 实验聚合）。"""
        if self.config.fixed_strategy:
            return "fixed_strategy"
        if self._pipeline_mode:
            return "pipeline"
        if self._bus_enabled:
            return "agent"
        return "legacy"

    def _should_post_llm(self, feedback: dict) -> bool:
        """执行后监控解读是否触发。

        旧路径（llm_shared=false）保持原语义：llm_post 非空即调用；
        B（llm_shared=true）按 EM 的 llm_policy 触发（默认仅异常/失败）。
        """
        if not self._agents_llm_shared:
            return True
        return llm_interpretation_needed(feedback, self._post_llm_policy())

    def _post_llm_skipped(self) -> dict:
        """共享 LLM 模式下未触发执行后调用时的诚实占位（不伪造解读）。"""
        policy = self._post_llm_policy()
        if policy == "never":
            reason = "llm_policy=never，跳过执行后监控 LLM 解读"
        elif policy == "on_anomaly":
            reason = "无异常且未失败，按 llm_policy=on_anomaly 跳过执行后监控 LLM 解读"
        else:
            reason = (f"无异常且未退化，按 llm_policy={policy} "
                      "跳过执行后监控 LLM 解读")
        return {
            "available": False,
            "performance_summary": None,
            "anomaly_summary": None,
            "notes": [reason],
        }

    # ------------------------------------------------------------------
    # Agent 端点（消息总线）
    # ------------------------------------------------------------------
    def _analyze_query(self, query: str, query_id: str,
                       llm_qa: Optional[object] = None
                       ) -> tuple[dict, Optional[dict]]:
        """分析入口：QA Agent 或确定性实现，返回 (analysis, Agent 元数据)。"""
        if self._qa_agent_enabled:
            out = run_query_analysis_agent(
                query, query_id=query_id,
                database_state=self.config.database_state,
                llm=llm_qa,
                agent_config=self.agents_config["query_analysis"],
            )
            return out["analysis"], out.get("agent")
        return analyze(query, query_id=query_id, llm=llm_qa), None

    def _retrieve_knowledge(self, analysis: dict, query_id: str,
                            max_records: Optional[int] = None,
                            fixed_topk: bool = False
                            ) -> tuple[dict, Optional[dict]]:
        """检索入口：KM Agent 或确定性实现，返回 (retrieval, Agent 元数据)。

        fixed_topk=True（pipeline 控制流）：强制单轮检索，不按观察扩 top-K；
        仍复用 KM Agent 的单步运行器，保证与 agent 模式的组件/LLM 调用点一致。
        """
        if self._km_agent_enabled:
            agent_config = dict(self.agents_config["knowledge_memory"])
            if fixed_topk:
                agent_config["max_rounds"] = 1
            out = self.km.run_retrieve(
                analysis,
                database_state=self.config.database_state,
                system_state=self.config.system_state, query_id=query_id,
                llm_summary=False,
                max_records=max_records,
                summary=False,
                agent_config=agent_config,
            )
            return out["retrieval"], out.get("agent")
        return self.km.retrieve(
            analysis, database_state=self.config.database_state,
            system_state=self.config.system_state, query_id=query_id,
            llm_summary=False, max_records=max_records, summary=False,
        ), None

    def _recall_max_records(self) -> int:
        """OD 补充证据请求的 top-K：必须比首轮更宽，否则没有补充意义。"""
        initial = int(self.config.knowledge_max_records or 100)
        configured = self.agent_config.get("recall_max_records")
        if (isinstance(configured, int) and not isinstance(configured, bool)
                and configured > 0):
            value = configured
        else:
            value = max(200, initial * 2)
        return max(int(value), initial + 1)

    @staticmethod
    def _bus_request(bus: AgentBus, sender: str, recipient: str,
                     intent: str, payload: Optional[dict] = None
                     ) -> tuple[Optional[dict], Optional[str]]:
        """发送一次请求-响应；失败返回 (None, 错误文本)，由调用方决定兜底。"""
        reply = bus.send(AgentMessage(
            sender=sender, recipient=recipient, intent=intent,
            payload=payload or {},
        ))
        if reply.intent == "error":
            return None, reply.meta.get("error") or "未知总线错误"
        return reply.payload or {}, None

    def _make_bus(self, ctx: dict) -> AgentBus:
        """为单条查询构建消息总线端点（消息只携带小型参数，查询上下文由
        ctx 捕获；大对象不重复写入消息 trace）。"""
        bus = AgentBus()

        def handle_analyze(message: AgentMessage) -> dict:
            analysis, meta = self._analyze_query(
                ctx["query"], ctx["query_id"], ctx.get("llm_qa"))
            ctx["analysis"] = analysis
            return {"analysis": analysis, "agent": meta}

        def handle_knowledge(message: AgentMessage) -> dict:
            analysis = ctx.get("analysis")
            if not isinstance(analysis, dict):
                raise ValueError("知识检索前缺少 query_analysis")
            if message.intent in ("retrieve", "recall"):
                result, meta = self._retrieve_knowledge(
                    analysis, ctx["query_id"],
                    max_records=message.payload.get("max_records"),
                    fixed_topk=(self._pipeline_mode
                                and message.intent == "retrieve"))
                return {"retrieval": result, "agent": meta}
            if message.intent == "remember":
                feedback_input = ctx.get("feedback_input")
                if not isinstance(feedback_input, dict):
                    raise ValueError("知识入库前缺少 feedback_input")
                llm_km = ctx.get("llm_km")
                return {"update": self.km.update(
                    feedback_input,
                    experience_note=ctx.get("experience_note"),
                    use_llm=llm_km is not None,
                    llm=llm_km,
                )}
            raise ValueError(
                f"知识记忆 Agent 不支持 intent={message.intent!r}")

        def handle_monitor(message: AgentMessage) -> dict:
            monitoring_input = message.payload.get("monitoring_input")
            if not isinstance(monitoring_input, dict):
                raise ValueError("监控请求缺少 monitoring_input")
            external = bool(message.payload.get("external_llm", False))
            monitoring, meta = self._monitor_impl(
                monitoring_input,
                llm=None if external else ctx.get("llm"),
                external_llm=external,
            )
            return {"monitoring": monitoring, "agent": meta}

        bus.register("query_analysis", handle_analyze)
        bus.register("knowledge_memory", handle_knowledge)
        bus.register("execution_monitoring", handle_monitor)
        return bus

    # ------------------------------------------------------------------
    # 执行 + 监控（非 Agent 路径与 Agent 执行工具共用）
    # ------------------------------------------------------------------
    def _monitor_impl(self, monitoring_input: dict,
                      llm: Optional[object] = None,
                      external_llm: bool = False
                      ) -> tuple[dict, Optional[dict]]:
        """监控实现：统一确定性路径与 Agent 路径。"""
        if self._em_agent_enabled:
            out = run_monitoring_agent(
                monitoring_input, llm=llm,
                agent_config=self.agents_config["execution_monitoring"],
                external_llm=external_llm,
            )
            return out["monitoring"], out.get("agent")
        return monitor(monitoring_input, llm=llm), None

    def _monitor(self, monitoring_input: dict, llm: Optional[object] = None,
                 external_llm: bool = False, bus: Optional[AgentBus] = None,
                 sender: str = "runner") -> tuple[dict, Optional[dict]]:
        """监控入口：有总线时向监控 Agent 发请求；失败回退直接实现。"""
        if bus is None:
            return self._monitor_impl(monitoring_input, llm=llm,
                                      external_llm=external_llm)
        payload, error = self._bus_request(
            bus, sender, "execution_monitoring", "monitor",
            {"monitoring_input": monitoring_input,
             "external_llm": bool(external_llm)},
        )
        if error is None and isinstance(payload.get("monitoring"), dict):
            return payload["monitoring"], payload.get("agent")
        # 总线失败：错误已写入 bus.trace，这里回退到直接实现保证查询继续
        return self._monitor_impl(monitoring_input, llm=llm,
                                  external_llm=external_llm)

    def _execute_and_monitor(self, decision: dict, query: str, query_id: str,
                             analysis: dict, historical_records: Any,
                             bus: Optional[AgentBus] = None,
                             monitor_sender: str = "runner") -> dict:
        """执行一次决策并生成监控反馈；返回可序列化的执行事实与计时。"""
        t_exec = time.perf_counter()
        execution: ExecutionResult = self.executor.execute(
            query, query_id, decision.get("selected_strategy") or {}
        )
        execution_ms = (time.perf_counter() - t_exec) * 1000.0

        t_mon = time.perf_counter()
        # 历史参照 = 相似历史中的实测延迟（baseline 计划优先）：已见过的查询
        # 必须能判断 improved/unchanged/degraded；全新查询才允许证据不足。
        reference = build_historical_reference(
            decision, historical_records, self.config.reference_time,
            query_analysis=analysis,
            database_state=self.config.database_state,
        )
        monitoring_input = {
            "query_id": query_id,
            "strategy_id": (decision.get("selected_strategy") or {}).get("strategy_id") or "",
            "execution_status": execution.execution_status,
            "failure_reason": execution.failure_reason,
            "metrics": execution.metrics,
            "baseline_metrics": execution.baseline_metrics,
            "historical_reference": reference,
            "thresholds": self.config.thresholds or None,
        }
        # 监控 Agent 的 LLM 解读由 runner 的合并调用统一管理（不重复调用）
        feedback, monitoring_agent = self._monitor(
            monitoring_input, llm=None, external_llm=True,
            bus=bus, sender=monitor_sender)
        monitoring_ms = (time.perf_counter() - t_mon) * 1000.0
        return {
            "agent": monitoring_agent,
            "execution": {
                "execution_status": execution.execution_status,
                "failure_reason": execution.failure_reason,
                "metrics": execution.metrics,
                "baseline_metrics": execution.baseline_metrics,
                "timestamp": execution.timestamp,
            },
            "monitoring": feedback,
            "monitoring_reference": reference,
            "monitoring_input": monitoring_input,
            "execution_ms": round(execution_ms, 3),
            "monitoring_ms": round(monitoring_ms, 3),
        }

    def _agent_execute_tool(self, decision: dict, query: str, query_id: str,
                            analysis: dict, historical_records: Any,
                            bus: Optional[AgentBus] = None) -> dict:
        """Agent 的执行工具：异常也转成事实性的失败结果，不中断控制环。"""
        try:
            return self._execute_and_monitor(
                decision, query, query_id, analysis, historical_records,
                bus=bus, monitor_sender="optimization_decision")
        except Exception as exc:   # 执行层异常 → failed（真实错误文本，不编造）
            reason = f"{type(exc).__name__}: {exc}"
            monitoring_input = {
                "query_id": query_id,
                "strategy_id": (decision.get("selected_strategy") or {}).get("strategy_id") or "",
                "execution_status": "failed",
                "failure_reason": reason,
                "metrics": {},
                "baseline_metrics": None,
                "historical_reference": None,
                "thresholds": self.config.thresholds or None,
            }
            feedback, monitoring_agent = self._monitor(
                monitoring_input, llm=None, external_llm=True,
                bus=bus, sender="optimization_decision")
            return {
                "agent": monitoring_agent,
                "execution": {
                    "execution_status": "failed",
                    "failure_reason": reason,
                    "metrics": {},
                    "baseline_metrics": None,
                    "timestamp": None,
                },
                "monitoring": feedback,
                "monitoring_reference": None,
                "monitoring_input": monitoring_input,
                "execution_ms": 0.0,
                "monitoring_ms": 0.0,
            }

    # ------------------------------------------------------------------
    # 单查询闭环
    # ------------------------------------------------------------------
    def run_query(self, query: str, query_id: str = "", source: str = "",
                  sampling: Optional[dict] = None) -> dict:
        """执行一次完整闭环，返回追踪记录（也写入 results_dir）。

        query_id 缺省时自动生成 f"{run_id}-{seq:04d}"（每次运行唯一，不覆盖历史结果）。

        计时（D8）：全链路 perf_counter 墙钟，写入 trace["overhead"]——
        优化机制各行为成本（分析/检索/决策/监控/入库分项 + 合计）与端到端
        （end_to_end_ms，请求进入 → 查询结果就绪）。纯 runner 计时，不碰 Agent 输出。
        """
        t_entry = time.perf_counter()
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        if not query_id:
            query_id = f"{self.run_id}-{seq:04d}"

        llm = self._llm_for_query()   # 采样控制；未采样 → 确定性快路径
        # LLM 路由：
        # - 默认（llm_shared=false）：旧行为——llm_primary 只给 OD，其余模式
        #   的 llm 照常用于解读增强；
        # - llm_shared=true（B/方案一）：同一客户端按各 Agent 的 llm_policy
        #   共享；QA 仅在启用时接收；执行后监控解读由 EM 的 llm_policy 门控
        #   （默认仅异常/失败）；KM 经验总结按查询形状单独生成（同一形状
        #   只调用一次，见 KnowledgeMemoryAgent.update）。
        if self._agents_llm_shared:
            llm_qa = llm if self._qa_agent_enabled else None
            llm_post = llm if self._em_agent_enabled else None
            llm_km = llm if self._km_agent_enabled else None
        else:
            llm_qa = None if self._llm_decide else llm
            llm_post = llm_qa
            llm_km = None

        # 单查询消息总线：任一 Agent 启用时，主链路通过总线调用各 Agent 端点。
        bus_ctx = {
            "query": query, "query_id": query_id,
            "llm": llm, "llm_qa": llm_qa, "llm_km": llm_km,
            "analysis": None, "feedback_input": None, "experience_note": None,
        }
        bus = self._make_bus(bus_ctx) if self._bus_enabled else None

        # 1. 分析（LLM 增强可选；不可用自动回退确定性路径）
        qa_agent_meta = None
        analysis = None
        if bus is not None:
            payload, error = self._bus_request(
                bus, "runner", "query_analysis", "analyze",
                {"query_id": query_id})
            if error is None:
                analysis = payload.get("analysis")
                qa_agent_meta = payload.get("agent")
            if not isinstance(analysis, dict):
                # 总线失败或载荷非法：直接走分析实现，保证查询继续
                analysis, qa_agent_meta = self._analyze_query(
                    query, query_id, llm_qa)
        else:
            analysis, qa_agent_meta = self._analyze_query(
                query, query_id, llm_qa)
        bus_ctx["analysis"] = analysis
        t_analysis = time.perf_counter()
        # 2. 知识检索（历史证据；runner 链路关闭检索解读 LLM——决策只消费结构化记录）
        #    fixed 模式（Baseline 2）不使用历史，跳过检索
        km_agent_meta = None
        if self.config.fixed_strategy:
            km_result = {"matched_count": 0, "knowledge_confidence": 0.0,
                         "matched_records_for_decision": [],
                         "strategy_evaluations": {}}
        elif bus is not None:
            payload, error = self._bus_request(
                bus, "runner", "knowledge_memory", "retrieve",
                {"query_id": query_id,
                 "max_records": self.config.knowledge_max_records})
            if error is None:
                km_result = payload.get("retrieval")
                km_agent_meta = payload.get("agent")
            if not isinstance(km_result, dict):
                km_result, km_agent_meta = self._retrieve_knowledge(
                    analysis, query_id, self.config.knowledge_max_records,
                    fixed_topk=self._pipeline_mode)
        else:
            km_result, km_agent_meta = self._retrieve_knowledge(
                analysis, query_id, self.config.knowledge_max_records,
                fixed_topk=self._pipeline_mode)
        t_retrieval = time.perf_counter()
        # 3. 决策（LLM 增强可选；候选策略/可执行计划由决策 Agent 按查询生成）
        planning = dict(self.config.planning or {})
        planning.setdefault("enabled", True)
        planning.setdefault("max_parallelism", self.config.plan_max_parallelism)
        planning.setdefault("min_saving_ms", self.config.plan_min_saving_ms)
        planning.setdefault("min_saving_ratio", self.config.plan_min_saving_ratio)
        decision_input = {
            "query_id": query_id,
            "query": query,
            "query_analysis": analysis,
            "database_state": self.config.database_state,
            "system_state": self.config.system_state,
            "historical_records": km_result.get("matched_records_for_decision") or [],
            # 非空外部配置覆盖 Agent 内置候选生成；缺失 → Agent 按查询生成
            "candidate_strategies": self.config.candidate_strategies,
            "baseline_strategy": self.config.baseline_strategy,
            "reference_time": self.config.reference_time,
            "llm_preference_bonus": bool((self.config.llm or {}).get("preference_bonus")),
            # D1：策略评价表（反馈驱动的动态权重，来自 KM 检索输出）
            "strategy_evaluations": km_result.get("strategy_evaluations"),
            "eval_prior_strength": self.config.eval_prior_strength,
            "eval_min_count": self.config.eval_min_count,
            # 计划生成是决策 Agent 的固有步骤；这里只提供事实输入与执行上限
            "device_inventory": self.device_inventory,
            "planning": planning,
        }
        agent_meta = None
        em_agent_meta = None
        if self.config.fixed_strategy:
            decision = self._fixed_decision(analysis, query_id)
            t_decision = time.perf_counter()
            execution_info = self._execute_and_monitor(
                decision, query, query_id, analysis,
                km_result.get("matched_records_for_decision"),
                bus=bus,
            )
            em_agent_meta = execution_info.get("agent")
            t_end = time.perf_counter()
        elif self._agent_enabled and not self._pipeline_mode:
            # Agent 闭环：决策 → 执行 → 观察 →（退化时）重规划 → 终止。
            historical_records = km_result.get("matched_records_for_decision") or []

            def recall_evidence() -> dict:
                """OD → KM：证据为空时请求一次更宽的历史检索。"""
                payload, error = self._bus_request(
                    bus, "optimization_decision", "knowledge_memory",
                    "recall",
                    {"query_id": query_id,
                     "max_records": self._recall_max_records(),
                     "reason": "empty_evidence"})
                if error is not None:
                    return {"ok": False, "error": error,
                            "historical_records": []}
                retrieval = payload.get("retrieval") or {}
                meta = payload.get("agent")
                return {
                    "ok": True,
                    "historical_records":
                        retrieval.get("matched_records_for_decision") or [],
                    "retrieval": {
                        "matched_count":
                            len(retrieval.get("matched_records") or []),
                        "usable_count":
                            len(retrieval.get(
                                "matched_records_for_decision") or []),
                        "knowledge_confidence":
                            retrieval.get("knowledge_confidence"),
                        "truncated": retrieval.get("truncated"),
                    },
                    # 补充检索也是一次 Agent 运行，摘要写入 evidence_recall
                    "agent": ({
                        "steps": meta.get("steps"),
                        "terminated_by": meta.get("terminated_by"),
                        "retrieval_termination":
                            meta.get("retrieval_termination"),
                        "final_max_records": meta.get("final_max_records"),
                        "retrieval_ms_total": meta.get("retrieval_ms_total"),
                    } if isinstance(meta, dict) else None),
                }

            agent_out = run_agent(
                decision_input,
                execute=lambda decision: self._agent_execute_tool(
                    decision, query, query_id, analysis, historical_records,
                    bus=bus),
                recall=recall_evidence if bus is not None else None,
                agent_config=self.agent_config,
                llm=llm, llm_decide=self._llm_decide,
            )
            decision = agent_out.get("decision") or {}
            agent_meta = agent_out.get("agent") or {}
            em_agent_meta = agent_out.get("monitoring_agent")
            execution_info = {
                "execution": agent_out.get("execution") or {},
                "monitoring": agent_out.get("monitoring") or {},
                "monitoring_reference": agent_out.get("monitoring_reference"),
                "monitoring_input": agent_out.get("monitoring_input") or {},
            }
            t_decision = time.perf_counter()
            t_end = t_decision
        else:
            # pipeline 控制流（或未启用 OD Agent）：单次决策 + 单次执行，
            # 不进入观察驱动闭环；LLM 调用点与 agent 模式保持一致。
            decision = decide(decision_input, llm=llm, llm_decide=self._llm_decide)
            t_decision = time.perf_counter()
            execution_info = self._execute_and_monitor(
                decision, query, query_id, analysis,
                km_result.get("matched_records_for_decision"),
                bus=bus,
            )
            em_agent_meta = execution_info.get("agent")
            t_end = time.perf_counter()

        # 4/5. 执行与监控结果（两种路径统一为可序列化字典）
        execution = execution_info.get("execution") or {}
        feedback = execution_info.get("monitoring") or {}
        historical_reference = execution_info.get("monitoring_reference")
        monitoring_input = execution_info.get("monitoring_input") or {}
        t_monitoring = t_end
        # 最后一次执行完成的时刻（扣除最终监控耗时；用于端到端计时）
        if agent_meta is not None:
            final_mon_ms = float(agent_meta.get("final_monitoring_ms") or 0.0)
        else:
            final_mon_ms = float(execution_info.get("monitoring_ms") or 0.0)
        t_execution = t_end - final_mon_ms / 1000.0
        # 执行后 LLM：
        # - llm_shared=false：旧语义，llm_post 非空即调用（合并产出监控解读 +
        #   KM 经验总结，llm_primary 下 llm_post 为 None）；
        # - llm_shared=true（方案一）：llm_post 只做监控解读，由 EM 的
        #   llm_policy 门控（默认仅异常/失败）；KM 经验总结由 KM Agent 按
        #   查询形状单独生成（同形状只调用一次）；未触发时写诚实占位。
        experience_note: Optional[str] = None
        if llm_post is not None and self._should_post_llm(feedback):
            from mataof.agents.execution_monitoring.llm_enhance import (
                build_post_execution_llm,
            )
            combined = build_post_execution_llm(
                llm_post, monitoring_input, feedback,
                include_experience_note=not self._agents_llm_shared)
            feedback["llm_analysis"] = {
                "available": (combined["performance_summary"] is not None
                              or combined["anomaly_summary"] is not None),
                "performance_summary": combined["performance_summary"],
                "anomaly_summary": combined["anomaly_summary"],
                "notes": combined["notes"],
            }
            if not self._agents_llm_shared:
                experience_note = combined["experience_note"]
        elif self._agents_llm_shared and llm_post is not None:
            feedback["llm_analysis"] = self._post_llm_skipped()
        elif self._agents_llm_shared:
            reason = ("EM Agent 未启用，跳过执行后监控 LLM 解读"
                      if llm_km is not None
                      else "EM/KM Agent 均未启用，跳过执行后 LLM 调用")
            feedback["llm_analysis"] = {
                "available": False, "performance_summary": None,
                "anomaly_summary": None,
                "notes": [reason],
            }
        # EM Agent 自身不调用 LLM（external_llm=True），实际调用方是 Runner。
        # 在 Agent 元数据里显式标注归属与本次是否产出解读，避免
        # llm_used=false 被误读为“EM 未使用 LLM”。
        if isinstance(em_agent_meta, dict) and em_agent_meta.get("enabled"):
            em_agent_meta["llm_call_owner"] = "runner"
            em_agent_meta["llm_interpretation_used"] = bool(
                (feedback.get("llm_analysis") or {}).get("available"))
        # 执行后 LLM 与知识入库分开计时：合并调用属于“执行后 LLM”，
        # 不是入库成本（否则汇总会把数秒的 LLM 推理归因到 knowledge_update）。
        t_post_llm = time.perf_counter()
        # 6. 知识更新（B/方案一：KM 按查询形状生成经验总结，同一形状仅一次；
        # 旧路径 use_llm=False，经验总结来自合并调用）
        feedback_input = {
            "query_id": query_id,
            "query": query,
            "query_analysis": analysis,
            "decision": decision,
            "monitoring": feedback,
            "database_state": self.config.database_state,
            "system_state": self.config.system_state,
            "timestamp": execution.get("timestamp"),
        }
        knowledge_update = None
        if bus is not None:
            bus_ctx["feedback_input"] = feedback_input
            bus_ctx["experience_note"] = experience_note
            payload, error = self._bus_request(
                bus, "runner", "knowledge_memory", "remember",
                {"query_id": query_id})
            if error is None:
                knowledge_update = payload.get("update")
            if not isinstance(knowledge_update, dict):
                knowledge_update = self.km.update(
                    feedback_input, experience_note=experience_note,
                    use_llm=llm_km is not None, llm=llm_km)
        else:
            knowledge_update = self.km.update(
                feedback_input, experience_note=experience_note,
                use_llm=llm_km is not None, llm=llm_km)
        t_update = time.perf_counter()
        # KM 的经验总结 LLM 调用发生在 update 内：从入库计时中扣除，
        # 归入“执行后 LLM”，避免再次把 LLM 推理误计为入库成本。
        km_llm_ms = 0.0
        if isinstance(knowledge_update, dict):
            km_llm_ms = float((knowledge_update.get("llm_analysis") or {})
                              .get("llm_ms") or 0.0)

        # 四 Agent 运行元数据（仅收集本次实际启用的 Agent；OD 的 trace["agent"] 保留）
        agent_metas = {}
        for name, meta in (
            ("query_analysis", qa_agent_meta),
            ("optimization_decision", agent_meta),
            ("execution_monitoring", em_agent_meta),
            ("knowledge_memory", km_agent_meta),
        ):
            if isinstance(meta, dict) and meta.get("enabled"):
                agent_metas[name] = meta

        trace = {
            "query_id": query_id,
            "query": query,
            "source": source,          # 来源文件（run_file 传入；手动查询为空串）
            "sampling": sampling,      # 每文件采样说明（无采样为 None）
            # 控制流/LLM 口径（2×2 实验聚合用；每个 trace 自描述，避免配置漂移）
            "control_flow": self._control_flow_label(),
            "llm_enabled": self.llm is not None,
            "llm_shared": self._agents_llm_shared,
            "decision_mode": (self.config.llm or {}).get("decision_mode"),
            "analysis": analysis,
            "knowledge_retrieval": {
                "matched_count": len(km_result.get("matched_records") or []),
                "knowledge_confidence": km_result.get("knowledge_confidence"),
            },
            "decision": decision,
            "execution": {
                "execution_status": execution.get("execution_status"),
                "failure_reason": execution.get("failure_reason"),
                "metrics": execution.get("metrics") or {},
                "baseline_metrics": execution.get("baseline_metrics"),
                "timestamp": execution.get("timestamp"),
            },
            "monitoring": feedback,
            "monitoring_reference": historical_reference,   # 效果判断所用的历史实测参照
            "knowledge_update": knowledge_update,
            # Agent 闭环元数据（未启用 → None）：尝试序列、终止原因、逐步 trace
            "agent": agent_meta,
            # 四 Agent 元数据（按名称索引；未启用的 Agent 不出现）
            "agents": agent_metas,
            # Agent 间消息（request/response；未启用 Agent → 空列表）
            "bus": bus.trace if bus is not None else [],
        }
        # ---- D8：优化机制各阶段成本与端到端计时（纯 runner 墙钟；不碰 Agent 输出）----
        ms = lambda t0, t1: round((t1 - t0) * 1000.0, 2)  # noqa: E731
        if agent_meta is not None:
            # Agent 模式：分项来自闭环内的累计计时（可能包含多次尝试）
            at = agent_meta.get("timings") or {}
            stage = {
                "analysis_ms": ms(t_entry, t_analysis),
                "retrieval_ms": ms(t_analysis, t_retrieval),
                "decision_ms": round(float(at.get("decision_ms") or 0.0), 2),
                "execution_stage_ms": round(float(at.get("execution_ms") or 0.0), 2),
                "monitoring_ms": round(float(at.get("monitoring_ms") or 0.0), 2),
                "post_llm_ms": round(ms(t_monitoring, t_post_llm) + km_llm_ms, 2),
                "knowledge_update_ms": round(
                    max(0.0, ms(t_post_llm, t_update) - km_llm_ms), 2),
            }
        else:
            stage = {
                "analysis_ms": ms(t_entry, t_analysis),
                "retrieval_ms": ms(t_analysis, t_retrieval),
                "decision_ms": ms(t_retrieval, t_decision),
                # 执行阶段 = 优化后语句的单次执行（含结果集完整消费）；
                # 与 response_time_ms 同一口径（客户端墙钟）
                "execution_stage_ms": ms(t_decision, t_execution),
                "monitoring_ms": ms(t_execution, t_monitoring),
                "post_llm_ms": round(ms(t_monitoring, t_post_llm) + km_llm_ms, 2),
                "knowledge_update_ms": round(
                    max(0.0, ms(t_post_llm, t_update) - km_llm_ms), 2),
            }
        trace["overhead"] = {
            **stage,
            # 优化机制自身成本合计（各行为之和，不含执行）
            "pipeline_overhead_ms": round(
                sum(v for k, v in stage.items() if k != "execution_stage_ms"), 2),
            # 端到端：请求进入优化系统 → 查询结果就绪（含决策开销与执行；
            # 监控/入库是执行后的收尾，不在用户等待路径上）
            "end_to_end_ms": ms(t_entry, t_execution),
            # 异步 LLM 推理补写耗时（同步模式/未采样 → None；不计入端到端）
            "llm_backfill_ms": None,
        }
        trace_path = self._write_trace(trace)
        if trace_path:
            with self._seq_lock:
                self._trace_paths[query_id] = trace_path
        # B4：异步增强——同步链路已完成，LLM 解读交由后台补写（不阻塞主链路）
        # 采样控制同样生效：未被采样的查询不入队（llm_analysis 保持未采样语义）
        if self._llm_async and self._llm_sampled(seq):
            self._enqueue_llm_backfill(trace, query, decision_input, monitoring_input)
        return trace

    # ------------------------------------------------------------------
    # B4：异步 LLM 增强（后台补写 llm_analysis，不阻塞确定性链路）
    # ------------------------------------------------------------------
    def _enqueue_llm_backfill(self, trace: dict, query: str,
                              decision_input: dict, monitoring_input: dict) -> None:
        if self._llm_executor is None:
            self._llm_executor = _DaemonBackfillPool(
                self._llm_workers, prefix="llm-backfill")
        fut = self._llm_executor.submit(
            lambda: self._backfill_llm(trace, query, decision_input,
                                       monitoring_input))
        with self._llm_futures_lock:
            self._llm_futures.append(fut)

    def _backfill_llm(self, trace: dict, query: str,
                      decision_input: dict, monitoring_input: dict) -> None:
        """后台补写：QA 摘要 + 决策解读 + 合并（监控解读+经验总结）。任何失败只记录。

        计时：llm_backfill_ms 记入 trace["overhead"]（异步尾延迟，不计入主链路）。
        """
        t_backfill = time.perf_counter()
        try:
            from mataof.agents.query_analysis.llm_enhance import (
                build_llm_analysis as qa_build,
            )
            trace["analysis"]["llm_analysis"] = qa_build(query, trace["analysis"], self.llm)

            from mataof.agents.optimization_decision.context import normalize_input
            from mataof.agents.optimization_decision.llm_enhance import (
                build_llm_analysis as dec_build,
            )
            ctx = normalize_input(decision_input)
            # 候选由 Agent 内置生成器产生；补进只读副本，保证 LLM 解读
            # prompt 看到与决策一致的候选集合（不改变决策输入本身）。
            llm_input = dict(decision_input)
            llm_input["candidate_strategies"] = {
                dim: [{"strategy": e["name"],
                       "equivalent_sql": e.get("equivalent_sql"),
                       "parameters": e.get("parameters")}
                      for e in entries]
                for dim, entries in ctx.candidates.items()
            }
            trace["decision"]["llm_analysis"] = dec_build(
                self.llm, llm_input, trace["decision"], ctx.candidates)

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
            trace.setdefault("overhead", {})["llm_backfill_ms"] = \
                round((time.perf_counter() - t_backfill) * 1000.0, 2)
            self._rewrite_trace_file(trace)

    def _rewrite_trace_file(self, trace: dict) -> None:
        """回写原 trace 文件（异步补写结果；路径来自 _write_trace 的记录）。

        原子写（tmp + os.replace）：进程退出时若补写被中止（stop_llm_executor），
        不会留下半写的损坏文件——中止前的内容保持完整。
        """
        with self._seq_lock:
            path = self._trace_paths.get(trace["query_id"])
        if not path:
            return
        try:
            tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(trace, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass

    def wait_llm_tasks(self, timeout_s: Optional[float] = None) -> int:
        """等待异步增强任务，返回**仍未完成**的任务数。

        timeout_s=None → 等待全部完成；
        timeout_s=0   → 不等待，立即返回未完成数（主流程不阻塞，B4 默认）；
        timeout_s>0   → 最多等待该秒数，超时放弃剩余任务。
        未完成的补写保持"未采样/未完成"语义，确定性产物不受影响。
        """
        with self._llm_futures_lock:
            futures = list(self._llm_futures)
        if timeout_s is None:
            for fut in futures:
                try:
                    fut.result()
                except Exception:
                    pass
        elif timeout_s > 0:
            import time as _time
            deadline = _time.monotonic() + timeout_s
            for fut in futures:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    break
                try:
                    fut.result(timeout=remaining)
                except Exception:
                    pass
        # timeout_s == 0：跳过等待
        with self._llm_futures_lock:
            self._llm_futures = [f for f in self._llm_futures if not f.done()]
        return sum(1 for f in futures if not f.done())

    def stop_llm_executor(self) -> None:
        """停用后台补写线程池：未完成补写即弃（daemon 线程，退出即终止）。

        补写池为 daemon 线程（_DaemonBackfillPool）：进程退出时
        threading._shutdown 不 join 它们，任何退出路径（正常/异常/Ctrl+C）
        都不会被积压补写阻塞。未完成补写按设计保持"未完成"语义
        （llm_analysis 不入库、确定性产物不受影响；重跑同查询集可命中
        LLM 缓存补齐）。本方法供 CLI 在汇总前显式调用；进程退出无需依赖它。
        """
        if self._llm_executor is not None:
            self._llm_executor.shutdown(wait=False, cancel_futures=True)
            self._llm_executor = None

    # ------------------------------------------------------------------
    # 批量运行
    # ------------------------------------------------------------------
    def run_statements(self, statements: list, source: str = "",
                       sampling: Optional[dict] = None, workers: int = 1,
                       progress_fn: Optional[Any] = None,
                       drain_llm: bool = True) -> list:
        """运行一组语句；source 为来源文件/名称，sampling 为采样说明（写入追踪）。

        workers>1 时用线程池并行处理（B2）：各查询的 LLM 调用可并发（vLLM
        continuous batching），知识库/执行层已做线程安全处理；结果按输入顺序返回。
        progress_fn：每条查询完成后回调（无参，用于 tqdm 进度条；线程安全）。
        drain_llm：结束后是否排空异步增强任务（多文件连续运行时传 False，
        把排空推迟到全部文件之后，避免每文件阻塞等待）。

        workers 语义（实测结论）：
        - 同步 LLM 模式（async_enhance=false）：主链路含 LLM 网络等待（I/O），
          多线程可重叠等待 → workers=4 实测提速 1.75×；
        - 异步模式 / 无 LLM：主链路为 CPU 密集（GIL 串行化），workers>1
          实测反而更慢（29k 库 896ms→2215ms/条）→ 自动降级为 1 并提示。
        """
        workers = max(1, int(workers))
        if workers > 1 and self._llm_async:
            print(f"[runner] 注意：异步 LLM 模式下主链路为 CPU 密集工作，"
                  f"workers={workers} 无收益（GIL），已按 workers=1 执行；"
                  f"需要并行请使用同步 LLM 模式（async_enhance=false）",
                  flush=True)
            workers = 1
        if workers == 1:
            traces = []
            for s in statements:
                traces.append(self.run_query(s, source=source, sampling=sampling))
                if progress_fn is not None:
                    progress_fn()
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as pool:
                def _run(s):
                    t = self.run_query(s, source=source, sampling=sampling)
                    if progress_fn is not None:
                        progress_fn()
                    return t
                traces = list(pool.map(_run, statements))   # map 保序
        if drain_llm:
            self.wait_llm_tasks()   # B4：批次结束排空异步增强任务
        self.km.store.flush()
        return traces

    def run_file(self, path: str, per_file_limit: Optional[int] = None,
                 sample_mode: Optional[str] = None,
                 sample_seed: Optional[int] = None,
                 workers: int = 1,
                 progress_fn: Optional[Any] = None,
                 drain_llm: bool = True) -> list:
        """运行查询文件：.sql 按分号拆分语句；.jsonl 每行 {"query_id", "query"}。

        按配置（或参数覆盖）做每文件采样：per_file_limit 条、seq/random 模式、
        随机种子 sample_seed（同种子可复现）。采样说明写入每条追踪与汇总日志。
        workers：并行处理查询数（B2，默认 1 串行）。
        progress_fn：每条查询完成回调（tqdm 进度条）。
        drain_llm：结束后是否排空异步增强（多文件连续运行时传 False）。
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
                if progress_fn is not None:
                    progress_fn()
            if drain_llm:
                self.wait_llm_tasks()
            self.km.store.flush()
            return traces
        with open(path, encoding="utf-8") as f:
            text = f.read()
        statements, sampling = sample_items(split_sql_statements(text), limit, mode, rng, seed)
        return self.run_statements(statements, source=source, sampling=sampling,
                                   workers=workers, progress_fn=progress_fn,
                                   drain_llm=drain_llm)

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
        applicable = {
            "alignment": bool((features.get("device") or {}).get("multi_device")),
            "fetch_size": True,
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
    @staticmethod
    def _agent_action_profile(agent_meta: Optional[dict],
                              agent_metas: dict) -> dict:
        """从各 Agent 的逐步 trace 提取动作序列摘要（写入 run_log）。

        不落完整动作序列（trace 文件已有），只保留：
        - 动作哈希（相同序列 → 相同哈希，用于统计“不同控制流”数量）；
        - 动作总数与按工具计数；
        - 每个 Agent 的步数与终止原因。
        """
        metas = dict(agent_metas or {})
        if isinstance(agent_meta, dict):
            metas["optimization_decision"] = agent_meta
        actions: list[str] = []
        steps: dict = {}
        terminations: dict = {}
        for name in _AGENT_NAMES:
            meta = metas.get(name)
            if not isinstance(meta, dict) or not meta.get("enabled"):
                continue
            trace = meta.get("trace") or []
            tools = [str(step.get("tool")) for step in trace
                     if isinstance(step, dict) and step.get("tool")]
            actions.extend(f"{name}:{tool}" for tool in tools)
            steps[name] = int(meta.get("steps") or 0)
            term = meta.get("terminated_by")
            if term:
                terminations[name] = str(term)
        counts: dict = {}
        for action in actions:
            counts[action] = counts.get(action, 0) + 1
        digest = None
        if actions:
            digest = hashlib.sha1(
                json.dumps(actions, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:12]
        return {
            "agent_action_hash": digest,
            "agent_action_count": len(actions),
            "agent_action_counts": counts,
            "agent_steps_by_agent": steps,
            "agent_terminations": terminations,
        }

    @staticmethod
    def _agent_trigger_profile(decision: dict, agent_meta: Optional[dict],
                               agent_metas: dict, feedback: dict,
                               knowledge_update: Optional[dict]) -> dict:
        """记录“agent 是否真的被事件触发”（2×2 实验的行为剖面）。"""
        metas = dict(agent_metas or {})
        qa = metas.get("query_analysis") or {}
        km = metas.get("knowledge_memory") or {}
        od = agent_meta if isinstance(agent_meta, dict) else {}
        em_la = (feedback or {}).get("llm_analysis") or {}
        km_la = (knowledge_update or {}).get("llm_analysis") or {}
        od_la = (decision or {}).get("llm_analysis") or {}
        attempts = od.get("attempts") or []
        km_reused = bool(km_la.get("note_reused"))
        triggers = {
            "replan": len(attempts) > 1,
            "recall": bool(od.get("evidence_recall")),
            "qa_llm": bool(qa.get("llm_used")),
            "em_llm": bool(em_la.get("available")),
            "km_llm": bool(km_la.get("available")) and not km_reused,
            "km_note_reused": km_reused,
            "km_adaptive_expand": int(km.get("rounds") or 0) > 1,
            "od_llm_decision": bool(od_la.get("llm_used_for_decision"))
            or bool(od.get("llm_used_for_decision")),
        }
        triggers["llm_engaged_count"] = sum(
            1 for key in ("qa_llm", "em_llm", "km_llm", "od_llm_decision")
            if triggers[key])
        return triggers

    @staticmethod
    def _bus_intent_profile(bus_trace: Any) -> dict:
        """统计总线请求 intent 分布（不把消息数本身当协作证据）。"""
        counts: dict = {}
        for entry in bus_trace or []:
            if not isinstance(entry, dict):
                continue
            request = entry.get("request") or {}
            intent = request.get("intent")
            if intent:
                key = str(intent)
                counts[key] = counts.get(key, 0) + 1
        return counts

    def _write_trace(self, trace: dict) -> Optional[str]:
        """写完整追踪记录（防同名覆盖）并追加 run_log 汇总行；返回文件路径。"""
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
        # 汇总行（追加，实验分析用；含 D8 各阶段成本与端到端）
        decision = trace.get("decision") or {}
        mon = trace.get("monitoring") or {}
        metrics = mon.get("metrics") or {}
        agent = trace.get("agent") or {}
        agent_meta_map = trace.get("agents") or {}
        recall_agent = (((agent_meta_map.get("optimization_decision") or {})
                         .get("evidence_recall") or {}).get("agent") or {})
        ov = trace.get("overhead") or {}
        plan_entry = (decision.get("decision") or {}).get("plan") or {}
        action_profile = self._agent_action_profile(agent, agent_meta_map)
        trigger_profile = self._agent_trigger_profile(
            decision, agent, agent_meta_map, mon,
            trace.get("knowledge_update") or {})
        summary = {
            "run_id": self.run_id,
            "query_id": query_id,
            "source": trace.get("source") or "",
            "sampling": trace.get("sampling"),
            "control_flow": trace.get("control_flow"),
            "llm_enabled": trace.get("llm_enabled"),
            "llm_shared": trace.get("llm_shared"),
            "decision_mode": trace.get("decision_mode"),
            "query_type": (trace.get("analysis") or {}).get("query_type"),
            "strategy_id": (decision.get("selected_strategy") or {}).get("strategy_id"),
            "decision_status": decision.get("decision_status"),
            # 计划（未生成 → null；便于按计划族/计划聚合实验指标）
            "plan_id": plan_entry.get("strategy"),
            "plan_family": plan_entry.get("family"),
            "plan_fallback": metrics.get("plan_fallback"),
            "execution_status": mon.get("execution_status"),
            "performance_assessment": mon.get("performance_assessment"),
            "anomaly_count": len(mon.get("anomalies") or []),
            "response_time_ms": metrics.get("response_time_ms"),
            "baseline_response_time_ms": (
                (trace.get("execution") or {}).get("baseline_metrics") or {}
            ).get("response_time_ms"),
            # Agent 闭环（未启用 → null）
            "agent_steps": agent.get("steps"),
            "agent_attempts": len(agent.get("attempts") or []),
            "agent_terminated_by": agent.get("terminated_by"),
            # 四 Agent 运行时（未启用 → [] / 0）；OD 的补充检索也计入总步数
            "agents_enabled": sorted(agent_meta_map.keys()),
            "agent_steps_total": (
                sum(int((m or {}).get("steps") or 0)
                    for m in agent_meta_map.values())
                + int(recall_agent.get("steps") or 0)),
            "bus_messages": len(trace.get("bus") or []),
            "bus_errors": sum(1 for e in (trace.get("bus") or [])
                              if e.get("error")),
            "bus_intents": self._bus_intent_profile(trace.get("bus")),
            # 行为剖面（2×2 对照：agent 是否真的产生了不同控制流）
            "agent_action_hash": action_profile["agent_action_hash"],
            "agent_action_count": action_profile["agent_action_count"],
            "agent_action_counts": action_profile["agent_action_counts"],
            "agent_steps_by_agent": action_profile["agent_steps_by_agent"],
            "agent_terminations": action_profile["agent_terminations"],
            "agent_triggers": trigger_profile,
            # D8：优化机制各阶段成本 + 端到端（run_log 供轮次汇总聚合）
            "analysis_ms": ov.get("analysis_ms"),
            "retrieval_ms": ov.get("retrieval_ms"),
            "decision_ms": ov.get("decision_ms"),
            "monitoring_ms": ov.get("monitoring_ms"),
            "post_llm_ms": ov.get("post_llm_ms"),
            "knowledge_update_ms": ov.get("knowledge_update_ms"),
            "pipeline_overhead_ms": ov.get("pipeline_overhead_ms"),
            "end_to_end_ms": ov.get("end_to_end_ms"),
            "cpu_utilization": metrics.get("cpu_utilization"),
            "cpu_utilization_peak": metrics.get("cpu_utilization_peak"),
            "memory_utilization": metrics.get("memory_utilization"),
            "memory_utilization_peak": metrics.get("memory_utilization_peak"),
            "knowledge_confidence": (trace.get("knowledge_retrieval") or {}).get("knowledge_confidence"),
            "stored": (trace.get("knowledge_update") or {}).get("stored"),
        }
        with open(os.path.join(self.config.results_dir, "run_log.jsonl"),
                  "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        return path

    def append_batch_row(self, total_time_s: float, total_queries: int,
                         source: str = "") -> None:
        """把一批次的墙钟总用时以 batch 行追加进 run_log（最终汇总据此算总用时/吞吐）。

        source：批次来源（文件名/位置参数等），供最终汇总区分各批次。
        """
        if not self.config.results_dir:
            return
        row = {"batch": True, "run_id": self.run_id, "query_id": None,
               "source": source,
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
        knowledge_backend=str(data.get("knowledge_backend") or "json"),
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
        async_drain_timeout=float(data.get("async_drain_timeout")
                                  if data.get("async_drain_timeout") is not None else 0.0),
        device_inventory=data.get("device_inventory"),
        device_inventory_file=data.get("device_inventory_file"),
        planning=data.get("planning") or {},
        agent=data.get("agent") or {},
        agents=data.get("agents") or {},
        plan_max_parallelism=int(data.get("plan_max_parallelism") or 8),
        plan_min_saving_ms=(float(data["plan_min_saving_ms"])
                            if data.get("plan_min_saving_ms") is not None else None),
        plan_min_saving_ratio=(float(data["plan_min_saving_ratio"])
                               if data.get("plan_min_saving_ratio") is not None else None),
    )
