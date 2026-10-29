"""IoTDBExecutor：接入 Apache IoTDB 的真实执行器。

职责（Executor 协议）：
1. 执行决策 Agent 选出的策略：equivalent_sql 非空时优先执行该 SQL，
   否则执行原查询（分区裁剪/聚合位置等执行级策略由 IoTDB 引擎按其自身能力执行，
   系统不做超出 SQL 的能力假设）；
2. 采集真实指标：延迟（墙钟时间，含结果集完全消费）、CPU/内存利用率
   （执行期间采样，psutil）、执行状态与失败原因；
   单次执行不产生分位数（P50/P95/P99 置空，不编造）；IO 吞吐暂无法按查询归因，
   置空（如实 unknown）；
3. 可选 baseline 实测：开启 measure_baseline 时先执行原查询作为 baseline，
   为监控 Agent 提供真实对比数据。

错误分类 → execution_status：
- 超时类异常 → timeout；
- 连接类异常（TException/socket 等）→ failed（原因：连接异常）；
- SQL/语句错误 → failed（原因：数据库返回的错误信息）。
所有失败原因均来自真实异常信息，绝不编造。
"""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from mataof.executors.base import ExecutionResult, Executor
from mataof.executors.merge import MergeError, merge_results, verify_merged
from mataof.agents.optimization_decision.plans.descriptor import validate_plan_dict

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

try:
    from iotdb.Session import Session as _IotdbSession
except ImportError:  # pragma: no cover
    _IotdbSession = None


# 连接类异常关键词（真实错误信息分类，不猜测）
_CONNECTION_KEYWORDS = (
    "connection", "connect", "socket", "refused", "reset", "broken pipe",
    "thrift", "tclosedtransport", "texception", "transport",
    "eof", "closed", "unreachable",
)


def classify_execution_error(exc: Exception) -> tuple[str, str]:
    """把真实异常分类为 (execution_status, failure_reason)。"""
    msg = str(exc)
    low = msg.lower()
    if any(k in low for k in ("timeout", "timed out", "time limit", "deadline")):
        return "timeout", msg[:300]
    if any(k in low for k in _CONNECTION_KEYWORDS):
        return "failed", f"连接异常：{msg[:300]}"
    return "failed", f"{type(exc).__name__}: {msg[:300]}"


class _CpuSampler:
    """执行期间后台采样 CPU/内存利用率（真实测量；psutil 缺失时返回 None）。

    输出平均与峰值两个口径（对应开题报告 3.5：平均与峰值利用率/峰值占用）。
    """

    def __init__(self):
        self._stop = threading.Event()
        self._cpu_samples: list[float] = []
        self._mem_samples: list[float] = []
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if psutil is None:
            return
        psutil.cpu_percent(interval=None)  # 初始化基准
        def _run():
            while not self._stop.is_set():
                try:
                    self._cpu_samples.append(psutil.cpu_percent(interval=None))
                    self._mem_samples.append(psutil.virtual_memory().percent)
                except Exception:
                    break
                self._stop.wait(0.05)
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> tuple[Optional[float], Optional[float]]:
        """返回 (cpu_avg, cpu_peak)，归一化 0~1。"""
        if psutil is None:
            return None, None
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if not self._cpu_samples:
            return None, None
        avg = round(sum(self._cpu_samples) / len(self._cpu_samples) / 100.0, 4)
        peak = round(max(self._cpu_samples) / 100.0, 4)
        return avg, peak

    def memory_utilization(self) -> tuple[Optional[float], Optional[float]]:
        """返回 (mem_avg, mem_peak)，归一化 0~1。执行期样本优先，否则单次采样。"""
        if psutil is None:
            return None, None
        try:
            current = psutil.virtual_memory().percent
        except Exception:
            return None, None
        if self._mem_samples:
            avg = round(sum(self._mem_samples) / len(self._mem_samples) / 100.0, 4)
            peak = round(max(max(self._mem_samples), current) / 100.0, 4)
            return avg, peak
        return round(current / 100.0, 4), round(current / 100.0, 4)


class IoTDBExecutor:
    """Apache IoTDB 真实执行器。

    session_factory 用于注入连接工厂（测试时注入 mock）。
    """

    name = "iotdb"

    def __init__(self, host: str = "127.0.0.1", port: int = 6667,
                 user: str = "root", password: str = "root",
                 fetch_size: int = 1024, measure_baseline: bool = True,
                 session_factory: Optional[Callable[..., Any]] = None):
        self._host = host
        self._port = int(port)
        self._user = user
        self._password = password
        self._fetch_size = int(fetch_size)
        self.measure_baseline = bool(measure_baseline)
        self._session_factory = session_factory or _IotdbSession
        self._sessions: dict = {}          # fetch_size → session（参数级杠杆多会话缓存）
        self._exec_lock = threading.Lock()   # Session 非线程安全：执行串行化（B2 并行）
        # 计划执行专用会话池：(fetch_size, slot) → session。与 _sessions 分开，
        # 保证同一 worker slot 上的 Session 串行使用、不同 slot 可并行。
        self._plan_sessions: dict = {}
        self._plan_lock = threading.Lock()

    # ------------------------------------------------------------------
    def _open(self, fetch_size: Optional[int] = None) -> Any:
        fs = int(fetch_size) if fetch_size else self._fetch_size
        sess = self._sessions.get(fs)
        if sess is not None:
            return sess
        if self._session_factory is None:
            raise RuntimeError("未安装 apache-iotdb 客户端（pip install apache-iotdb）")
        sess = self._session_factory(
            self._host, self._port, self._user, self._password,
            fetch_size=fs,
        )
        sess.open(False)
        self._sessions[fs] = sess
        return sess

    def close(self) -> None:
        for sess in list(self._sessions.values()) + list(self._plan_sessions.values()):
            try:
                sess.close()
            except Exception:
                pass
        self._sessions = {}
        self._plan_sessions = {}

    def count_devices(self, pattern: str) -> Optional[int]:
        """COUNT DEVICES <pattern> → 设备数（只读；设备清单完整性校验用）。

        无结果/非整数 → None（调用方按“无法校验”处理，不猜测）；
        连接/执行异常向上抛出，由调用方决定降级方式
        （validate_device_inventory 会记录真实原因并剔除该清单）。
        """
        with self._exec_lock:
            session = self._open()
            dataset = session.execute_query_statement(f"COUNT DEVICES {pattern}")
            try:
                if not dataset.has_next():
                    return None
                fields = dataset.next().get_fields()
                if not fields:
                    return None
                try:
                    return int(str(fields[0]))
                except (TypeError, ValueError):
                    return None
            finally:
                dataset.close_operation_handle()

    # ------------------------------------------------------------------
    # 计划执行（多子查询 + DataFrame 合并）
    # ------------------------------------------------------------------
    def _open_plan_session(self, fetch_size: int, slot: int) -> Any:
        """计划执行会话：每个 worker slot 一个独立 Session（并行安全）。"""
        key = (int(fetch_size), int(slot))
        with self._plan_lock:
            sess = self._plan_sessions.get(key)
            if sess is not None:
                return sess
            if self._session_factory is None:
                raise RuntimeError("未安装 apache-iotdb 客户端（pip install apache-iotdb）")
            sess = self._session_factory(
                self._host, self._port, self._user, self._password,
                fetch_size=int(fetch_size),
            )
            sess.open(False)
            self._plan_sessions[key] = sess
            return sess

    def _run_sql_df(self, sql: str, fetch_size: int, slot: int):
        """执行一条子查询并完整消费 DataFrame 批次（next_df 循环）。"""
        import pandas as pd

        session = self._open_plan_session(fetch_size, slot)
        dataset = session.execute_query_statement(sql)
        try:
            frames = []
            while dataset.has_next_df():
                df = dataset.next_df()
                if df is not None:
                    frames.append(df)
            if frames:
                return pd.concat(frames, ignore_index=True)
            # 空结果：保留列结构（否则空分区的纵向/横向合并无法校验）
            columns = list(dataset.get_column_names()) \
                if hasattr(dataset, "get_column_names") else []
            return pd.DataFrame(columns=columns)
        finally:
            dataset.close_operation_handle()

    def _run_plan_frames(self, plan: dict) -> tuple[list, int, int, int]:
        """并行执行计划的子查询；返回 (按计划顺序的 frames, parts, parallel, fetch_size)。"""
        parts = list(plan.get("subqueries") or [])
        ep = plan.get("exec_params") or {}
        fetch_size = ep.get("fetch_size")
        fetch_size = int(fetch_size) if isinstance(fetch_size, (int, float)) \
            and not isinstance(fetch_size, bool) and fetch_size > 0 else self._fetch_size
        parallel = ep.get("parallelism")
        parallel = int(parallel) if isinstance(parallel, int) and not isinstance(parallel, bool) \
            else 1
        parallel = max(1, min(parallel, len(parts)))

        if parallel == 1:
            return [self._run_sql_df(sql, fetch_size, 0) for sql in parts], \
                len(parts), 1, fetch_size

        slots: queue.Queue = queue.Queue()
        for i in range(parallel):
            slots.put(i)

        def _task(sql: str):
            slot = slots.get()
            try:
                return self._run_sql_df(sql, fetch_size, slot)
            finally:
                slots.put(slot)

        with ThreadPoolExecutor(max_workers=parallel,
                                thread_name_prefix="mataof-plan") as pool:
            futures = [pool.submit(_task, sql) for sql in parts]
            frames = [f.result() for f in futures]   # 保持计划声明顺序
        return frames, len(parts), parallel, fetch_size

    def _execute_plan(self, query: str, plan: dict) -> tuple[dict, str, Optional[str]]:
        """执行计划：子查询 → 合并 → 结构校验；失败回退原查询（计入总耗时）。"""
        metrics: dict = {}
        t0 = time.perf_counter()
        sampler = _CpuSampler()
        sampler.start()
        merge_ms = 0.0
        try:
            frames, parts, parallel, fetch_size = self._run_plan_frames(plan)
            merge_t0 = time.perf_counter()
            merged = merge_results(plan, frames)
            verify_merged(merged, plan, frames)
            merge_ms = round((time.perf_counter() - merge_t0) * 1000.0, 2)
            attempt_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            sampler.stop()
        except Exception as exc:
            attempt_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            sampler.stop()
            return self._fallback_to_query(
                query, plan, exc, attempt_ms, metrics)

        metrics["response_time_ms"] = attempt_ms
        metrics["plan_parts"] = parts
        metrics["plan_parallelism"] = parallel
        metrics["plan_fetch_size"] = fetch_size
        metrics["plan_merge_mode"] = (plan.get("merge") or {}).get("mode")
        metrics["plan_merge_ms"] = merge_ms
        metrics["plan_rows"] = int(len(merged))
        metrics["plan_columns"] = int(len(merged.columns))
        metrics["plan_fallback"] = False
        cpu_avg, cpu_peak = sampler.stop()
        if cpu_avg is not None:
            metrics["cpu_utilization"] = cpu_avg
        if cpu_peak is not None:
            metrics["cpu_utilization_peak"] = cpu_peak
        mem_avg, mem_peak = sampler.memory_utilization()
        if mem_avg is not None:
            metrics["memory_utilization"] = mem_avg
        if mem_peak is not None:
            metrics["memory_utilization_peak"] = mem_peak
        return metrics, "success", None

    def _fallback_to_query(self, query: str, plan: dict, exc: Exception,
                           attempt_ms: float,
                           metrics: dict) -> tuple[dict, str, Optional[str]]:
        """计划失败 → 用原查询回退；总耗时 = 尝试 + 回退（不隐藏失败成本）。"""
        fallback_t0 = time.perf_counter()
        fb_metrics, fb_status, fb_reason = self._execute_single(query)
        fallback_ms = round((time.perf_counter() - fallback_t0) * 1000.0, 2)
        total_ms = round(attempt_ms + fallback_ms, 2)
        out = dict(fb_metrics)
        out.update({
            "response_time_ms": total_ms,
            "plan_fallback": True,
            "plan_attempt_ms": attempt_ms,
            "plan_fallback_ms": fallback_ms,
            "plan_parts": len(plan.get("subqueries") or []),
            "plan_parallelism": (plan.get("exec_params") or {}).get("parallelism"),
            "plan_merge_mode": (plan.get("merge") or {}).get("mode"),
            "plan_error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "plan_fallback_query_id": plan.get("plan_id"),
        })
        if fb_status == "success":
            return out, "success", None
        reason = (f"计划执行失败（{type(exc).__name__}: {str(exc)[:160]}）；"
                  f"回退原查询失败：{fb_reason}")
        return out, fb_status, reason

    # ------------------------------------------------------------------
    def _run_sql(self, sql: str, sampler: _CpuSampler,
                 fetch_size: Optional[int] = None) -> tuple[dict, str, Optional[str]]:
        """执行一条 SQL → (metrics, execution_status, failure_reason)。metrics 只含实测值。"""
        metrics: dict = {}
        try:
            session = self._open(fetch_size=fetch_size)
        except Exception as exc:
            status, reason = classify_execution_error(exc)
            return {}, status, reason

        t0 = time.perf_counter()
        sampler.start()
        try:
            dataset = session.execute_query_statement(sql)
            try:
                while dataset.has_next():
                    dataset.next()      # 完整消费结果集（真实代价）
            finally:
                dataset.close_operation_handle()
        except Exception as exc:
            sampler.stop()
            status, reason = classify_execution_error(exc)
            return metrics, status, reason
        finally:
            sampler.stop()

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        metrics["response_time_ms"] = elapsed_ms
        cpu_avg, cpu_peak = sampler.stop()
        if cpu_avg is not None:
            metrics["cpu_utilization"] = cpu_avg
        if cpu_peak is not None:
            metrics["cpu_utilization_peak"] = cpu_peak
        mem_avg, mem_peak = sampler.memory_utilization()
        if mem_avg is not None:
            metrics["memory_utilization"] = mem_avg
        if mem_peak is not None:
            metrics["memory_utilization_peak"] = mem_peak
        return metrics, "success", None

    def _execute_single(self, sql: str,
                        fetch_size: Optional[int] = None) -> tuple[dict, str, Optional[str]]:
        """执行一条 SQL（独立采样）→ (metrics, execution_status, failure_reason)。"""
        return self._run_sql(sql, _CpuSampler(), fetch_size=fetch_size)

    def _execute_single_df(self, sql: str,
                           fetch_size: Optional[int] = None
                           ) -> tuple[dict, str, Optional[str]]:
        """执行一条 SQL（DataFrame 批量消费，独立采样）→ (metrics, status, reason)。"""
        metrics: dict = {}
        sampler = _CpuSampler()
        t0 = time.perf_counter()
        sampler.start()
        try:
            # slot -1：独立的计划会话槽，避免与 agent 计划的 worker slot 重叠
            # （execute_raw 持 _exec_lock，计划执行走各自 slot，不共享 Session）
            df = self._run_sql_df(sql, int(fetch_size or self._fetch_size), -1)
        except Exception as exc:
            status, reason = classify_execution_error(exc)
            return metrics, status, reason
        finally:
            sampler.stop()

        metrics["response_time_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        metrics["result_mode"] = "df"
        metrics["result_rows"] = int(len(df))
        metrics["result_columns"] = int(len(df.columns))
        cpu_avg, cpu_peak = sampler.stop()
        if cpu_avg is not None:
            metrics["cpu_utilization"] = cpu_avg
        if cpu_peak is not None:
            metrics["cpu_utilization_peak"] = cpu_peak
        mem_avg, mem_peak = sampler.memory_utilization()
        if mem_avg is not None:
            metrics["memory_utilization"] = mem_avg
        if mem_peak is not None:
            metrics["memory_utilization_peak"] = mem_peak
        return metrics, "success", None

    @staticmethod
    def _fetch_size_of(strategy: dict) -> Optional[int]:
        """从策略参数提取 fetch_size（候选生成器给出的执行级参数）。"""
        sp = (strategy or {}).get("strategy_parameters") or {}
        if not isinstance(sp, dict):
            return None
        v = (sp.get("fetch_size") or {}).get("fetch_size")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return int(v)
        return None

    # ------------------------------------------------------------------
    def execute_raw(self, sql: str, result_mode: str = "row",
                    fetch_size: Optional[int] = None) -> ExecutionResult:
        """原生 SQL 直发（无策略/无计划）· 双口径。

        row：逐行消费（与历史 native baseline 完全一致）；
        df ：DataFrame 批量消费（与 agent 计划路径同消费方式）。
        用于隔离“客户端批量读”与“决策/计划”各自的贡献。
        """
        if result_mode not in ("row", "df"):
            raise ValueError(f"未知 result_mode: {result_mode}（仅支持 row/df）")
        with self._exec_lock:
            if result_mode == "df":
                metrics, status, reason = self._execute_single_df(sql, fetch_size)
            else:
                metrics, status, reason = self._execute_single(sql, fetch_size)
        return ExecutionResult(
            execution_status=status,
            failure_reason=reason,
            metrics=metrics,
            baseline_metrics=None,
            timestamp=int(time.time() * 1000),
        )

    def execute(self, query: str, query_id: str, strategy: dict) -> ExecutionResult:
        """执行查询（baseline 实测可选 + 策略执行），返回事实性结果。

        执行串行化（B2 并行下 Session 非线程安全；执行本身仅 ~30ms，
        锁开销可忽略——LLM 调用才是并行收益来源）。
        策略参数 fetch_size 由候选生成器给出，此处按参数选择会话批次。
        """
        with self._exec_lock:
            strategy = strategy or {}
            timestamp = int(time.time() * 1000)

            baseline_metrics: Optional[dict] = None
            if self.measure_baseline and query.strip():
                b_metrics, b_status, _ = self._execute_single(query)
                if b_status == "success":
                    baseline_metrics = b_metrics
                # baseline 失败：不携带 baseline（监控 Agent 如实标记，不编造）

            # 计划优先：decision 输出的 execution_plan 是决策 Agent 编译好的
            # 可执行动作（子查询 + 合并规格），执行器只执行、不再生成 SQL。
            plan_raw = strategy.get("execution_plan")
            plan, plan_error = validate_plan_dict(plan_raw) \
                if isinstance(plan_raw, dict) else (None, None)
            result_mode = ((plan or {}).get("exec_params") or {}).get("result_mode", "df")
            if plan is not None and result_mode == "df":
                metrics, status, reason = self._execute_plan(query, plan)
            else:
                sql = strategy.get("equivalent_sql")
                if not sql or not isinstance(sql, str):
                    sql = query   # 执行级策略：SQL 不变，由引擎按其能力执行
                fetch_size = self._fetch_size_of(strategy)
                metrics, status, reason = self._execute_single(sql, fetch_size=fetch_size)
                if plan_raw is not None:
                    metrics["plan_fallback"] = True
                    metrics["plan_error"] = plan_error or "计划 result_mode 不是 df"
        return ExecutionResult(
            execution_status=status,
            failure_reason=reason,
            metrics=metrics,
            baseline_metrics=baseline_metrics,
            timestamp=timestamp,
        )
