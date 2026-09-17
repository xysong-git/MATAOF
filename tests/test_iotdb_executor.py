"""IoTDBExecutor 单元测试（mock session）与真实 IoTDB 集成测试。

单元测试用 FakeSession 注入，不依赖真实服务器；
集成测试在无法连接真实 IoTDB 时自动跳过（不伪造结果）。
运行：pytest tests/test_iotdb_executor.py -v
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mataof.executors.iotdb import IoTDBExecutor, classify_execution_error  # noqa: E402
from mataof.executors import build_executor  # noqa: E402


# ---------------------------------------------------------------------------
# Fake session（测试注入）
# ---------------------------------------------------------------------------

class FakeDataset:
    def __init__(self, rows: int = 3):
        self._n = rows

    def has_next(self) -> bool:
        return self._n > 0

    def next(self):
        self._n -= 1
        return object()

    def close_operation_handle(self):
        pass


class FakeSession:
    """按 execute_query_statement 收到的 SQL 文本触发行为。"""

    def __init__(self, host, port, user, password, fetch_size=1024):
        self.host, self.port, self.user, self.password = (host, port, user, password)
        self.executed: list[str] = []

    def open(self, *a, **k):
        pass

    def close(self):
        pass

    def execute_query_statement(self, sql: str):
        self.executed.append(sql)
        if "FAIL_ME" in sql:
            raise RuntimeError("701: SemanticErrorException: unsupported query")
        if "TIMEOUT_ME" in sql:
            raise TimeoutError("query timed out after 10000 ms")
        return FakeDataset(rows=3)


class BrokenSession(FakeSession):
    def open(self, *a, **k):
        raise ConnectionError("connection refused")


def _make_executor(**kwargs) -> IoTDBExecutor:
    kwargs.setdefault("session_factory", FakeSession)
    kwargs.setdefault("measure_baseline", False)
    return IoTDBExecutor(**kwargs)


# ---------------------------------------------------------------------------
# 错误分类（真实异常信息 → 状态，不猜测）
# ---------------------------------------------------------------------------

def test_classify_execution_error():
    assert classify_execution_error(TimeoutError("query timed out after 10s"))[0] == "timeout"
    status, reason = classify_execution_error(ConnectionError("socket refused"))
    assert status == "failed" and "连接异常" in reason
    status, reason = classify_execution_error(RuntimeError("701: unsupported"))
    assert status == "failed" and "unsupported" in reason


# ---------------------------------------------------------------------------
# 单元测试（mock session）
# ---------------------------------------------------------------------------

def test_success_collects_real_metrics():
    ex = _make_executor()
    r = ex.execute("SELECT s_0 FROM root.test.g_0.d_0 LIMIT 3", "q1", {})
    assert r.execution_status == "success"
    assert isinstance(r.metrics["response_time_ms"], float) and r.metrics["response_time_ms"] >= 0
    assert r.timestamp is not None
    ex.close()


def test_equivalent_sql_preferred():
    ex = _make_executor()
    strategy = {
        "strategy_id": "time_pruning=full_scan|filter_order=time_first",
        "equivalent_sql": "SELECT s_0 FROM root.test.g_0.d_0 WHERE time >= 1 ORDER BY time",
    }
    ex.execute("SELECT s_0 FROM root.test.g_0.d_0 LIMIT 3", "q1", strategy)
    assert ex._session.executed == [strategy["equivalent_sql"]]   # 执行的是等价 SQL
    ex.close()


def test_null_equivalent_sql_runs_original():
    ex = _make_executor()
    original = "SELECT s_0 FROM root.test.g_0.d_0 LIMIT 3"
    ex.execute(original, "q1", {"strategy_id": "x", "equivalent_sql": None})
    assert ex._session.executed == [original]
    ex.close()


def test_baseline_measured_from_original_query():
    ex = _make_executor(measure_baseline=True)
    original = "SELECT s_0 FROM root.test.g_0.d_0 LIMIT 3"
    strategy = {"strategy_id": "x",
                "equivalent_sql": "SELECT s_0 FROM root.test.g_0.d_0 LIMIT 3"}
    r = ex.execute(original, "q1", strategy)
    # 先执行 baseline（原查询），再执行策略 SQL
    assert ex._session.executed == [original, strategy["equivalent_sql"]]
    assert r.baseline_metrics is not None
    assert "response_time_ms" in r.baseline_metrics
    ex.close()


def test_sql_error_maps_to_failed():
    ex = _make_executor()
    r = ex.execute("SELECT FAIL_ME FROM root.test.g_0.d_0", "q1", {})
    assert r.execution_status == "failed"
    assert "unsupported" in (r.failure_reason or "")
    ex.close()


def test_timeout_maps_to_timeout():
    ex = _make_executor()
    r = ex.execute("SELECT TIMEOUT_ME FROM root.test.g_0.d_0", "q1", {})
    assert r.execution_status == "timeout"
    ex.close()


def test_connection_error_maps_to_failed():
    ex = IoTDBExecutor(session_factory=BrokenSession, measure_baseline=False)
    r = ex.execute("SELECT s_0 FROM root.test.g_0.d_0", "q1", {})
    assert r.execution_status == "failed"
    assert "连接异常" in (r.failure_reason or "")
    ex.close()


def test_build_executor_iotdb_spec():
    ex = build_executor({"type": "iotdb", "host": "127.0.0.1", "port": 6667,
                         "measure_baseline": False})
    assert isinstance(ex, IoTDBExecutor)
    assert ex.measure_baseline is False
    ex2 = build_executor("iotdb")
    assert isinstance(ex2, IoTDBExecutor)


# ---------------------------------------------------------------------------
# 真实 IoTDB 集成测试（无法连接则跳过，不伪造）
# ---------------------------------------------------------------------------

def _real_iotdb_available() -> bool:
    try:
        from iotdb.Session import Session
        s = Session("127.0.0.1", 6667, "root", "root", fetch_size=16)
        s.open(False)
        s.close()
        return True
    except Exception:
        return False


REAL_IOTDB = pytest.mark.skipif(not _real_iotdb_available(),
                                reason="真实 IoTDB（127.0.0.1:6667）不可用")


@REAL_IOTDB
def test_real_execution_success():
    ex = IoTDBExecutor(measure_baseline=False)
    r = ex.execute(
        "SELECT s_0 FROM root.test.g_0.d_0 WHERE time >= 1640966400000 "
        "AND time <= 1640966650000", "q_real", {})
    ex.close()
    assert r.execution_status == "success"
    assert r.metrics["response_time_ms"] > 0


@REAL_IOTDB
def test_real_execution_error_classified():
    ex = IoTDBExecutor(measure_baseline=False)
    r = ex.execute("SELECT s_0 FROM root.test.g_0.d_0 WHERE s_0 > -5", "q_real", {})
    ex.close()
    # DOUBLE 列与 INT 字面量比较 → 数据库真实报错 → failed + 真实原因
    assert r.execution_status == "failed"
    assert r.failure_reason


@REAL_IOTDB
def test_real_baseline_measurement():
    ex = IoTDBExecutor(measure_baseline=True)
    r = ex.execute(
        "SELECT s_0 FROM root.test.g_0.d_0 WHERE time >= 1640966400000 "
        "AND time <= 1640966650000", "q_real",
        {"strategy_id": "x", "equivalent_sql": None})
    ex.close()
    assert r.execution_status == "success"
    assert r.baseline_metrics is not None
    assert r.baseline_metrics["response_time_ms"] > 0
