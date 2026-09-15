"""历史记录相似度评估与加权（历史知识使用原则的实现）。

原则：历史结果属于"证据"，不是绝对规则。
- 只有查询特征 / 数据库状态 / 系统状态三方面都与当前上下文较为接近的记录
  才参与决策（相似度阈值 SIMILARITY_THRESHOLD）；
- 记录按相似度加权，且可按时效衰减（record_time 与 reference_time 都存在时）；
- 结构不合法或缺少关键字段的记录被计为 invalid，不参与决策。

相似度公式（确定性，全部基于输入数据，无任何编造）：
    similarity = 0.6 * sim_query + 0.25 * sim_db + 0.15 * sim_system
- sim_query：对摘要特征逐项加权（类别相等 1/0；数值用 min/max 比例；缺失双方 0.5、
  单方缺失 0.3；函数集合用 Jaccard）；
- sim_db / sim_system：仅对双方都提供的键比较；无共同键 → 0.5（中性）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

SIMILARITY_THRESHOLD = 0.5
SIM_WEIGHT_QUERY = 0.6
SIM_WEIGHT_DB = 0.25
SIM_WEIGHT_SYSTEM = 0.15
DECAY_HALF_LIFE_HOURS = 72.0

_QUERY_FIELDS = (
    ("query_type", "categorical", 0.15),
    ("time.has_time_filter", "categorical", 0.10),
    ("time.range_level", "categorical", 0.10),
    ("time.time_span", "numeric", 0.10),
    ("device.multi_device", "categorical", 0.05),
    ("device.device_count", "numeric", 0.05),
    ("filter.non_time_filter_count", "numeric", 0.10),
    ("aggregation.has_aggregation", "categorical", 0.10),
    ("aggregation.has_group_by", "categorical", 0.05),
    ("aggregation.has_window", "categorical", 0.05),
    ("aggregation.functions", "jaccard", 0.10),
)


def _get_path(obj: dict, path: str) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _categorical_sim(a: Any, b: Any) -> float:
    if a is None or b is None:
        return 0.5
    return 1.0 if a == b else 0.0


def _numeric_sim(a: Any, b: Any) -> float:
    a_ok = isinstance(a, (int, float)) and not isinstance(a, bool)
    b_ok = isinstance(b, (int, float)) and not isinstance(b, bool)
    if not a_ok and not b_ok:
        return 0.5
    if not a_ok or not b_ok:
        return 0.3
    if a == 0 and b == 0:
        return 1.0
    if a <= 0 or b <= 0:
        return 0.0
    return min(a, b) / max(a, b)


def _jaccard_sim(a: Any, b: Any) -> float:
    sa = set(a) if isinstance(a, (list, set, tuple)) else set()
    sb = set(b) if isinstance(b, (list, set, tuple)) else set()
    if not sa and not sb:
        return 0.5
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _similarity_query(current: dict, record: dict) -> float:
    total_w, total_s = 0.0, 0.0
    for path, kind, w in _QUERY_FIELDS:
        a, b = _get_path(current, path), _get_path(record, path)
        if kind == "categorical":
            s = _categorical_sim(a, b)
        elif kind == "numeric":
            s = _numeric_sim(a, b)
        else:
            s = _jaccard_sim(a, b)
        total_w += w
        total_s += w * s
    return total_s / total_w if total_w else 0.5


def _similarity_db(current_db: dict, record_db: dict) -> float:
    current_db = current_db or {}
    record_db = record_db or {}
    # 双方都提供的键才比较
    numeric_keys = ("device_scale",)
    cat_keys = ("data_scale_level",)
    total_w, total_s = 0.0, 0.0
    for k in cat_keys:
        if k in current_db and k in record_db:
            total_w += 0.5
            total_s += 0.5 * _categorical_sim(current_db[k], record_db[k])
    for k in numeric_keys:
        if k in current_db and k in record_db:
            total_w += 0.5
            total_s += 0.5 * _numeric_sim(current_db[k], record_db[k])
    if total_w == 0:
        return 0.5  # 无共同键 → 中性
    return total_s / total_w


def _similarity_system(current_sys: dict, record_sys: dict) -> float:
    current_sys = current_sys or {}
    record_sys = record_sys or {}
    if "load_level" in current_sys and "load_level" in record_sys:
        return _categorical_sim(current_sys["load_level"], record_sys["load_level"])
    return 0.5


def record_similarity(current_compact: dict, current_db: dict, current_sys: dict,
                      record: dict) -> float:
    """计算单条历史记录与当前上下文的综合相似度（0~1）。"""
    record_qf = record.get("query_features") or {}
    sim_q = _similarity_query(current_compact, record_qf)
    sim_db = _similarity_db(current_db, record.get("database_state") or {})
    sim_sys = _similarity_system(current_sys, record.get("system_state") or {})
    return SIM_WEIGHT_QUERY * sim_q + SIM_WEIGHT_DB * sim_db + SIM_WEIGHT_SYSTEM * sim_sys


def _decay(record: dict, reference_time: Optional[int]) -> float:
    """时效衰减：1/(1 + age_hours/72)。无 record_time 或 reference_time → 不衰减。"""
    if reference_time is None:
        return 1.0
    t = record.get("record_time")
    if not isinstance(t, (int, float)) or isinstance(t, bool):
        return 1.0
    hours = (reference_time - t) / 3_600_000.0
    if hours < 0:
        return 0.5  # 未来时间戳：保守取 0.5
    return 1.0 / (1.0 + hours / DECAY_HALF_LIFE_HOURS)


def _valid_record(record: dict) -> bool:
    if not isinstance(record, dict):
        return False
    if not record.get("record_id") or not isinstance(record.get("query_features"), dict):
        return False
    strategy = record.get("strategy")
    if not isinstance(strategy, dict):
        return False
    fb = record.get("execution_feedback") or {}
    lat = fb.get("latency_ms")
    if not isinstance(lat, (int, float)) or isinstance(lat, bool) or lat < 0:
        return False
    return True


@dataclass
class HistoryEvidence:
    """历史证据集合：相似度达标的记录 + 按维度/策略聚合的统计。"""

    used: list = field(default_factory=list)   # [{"record_id", "similarity", "weight", "record"}]
    below_threshold: int = 0
    invalid: int = 0

    def count_for(self, dim: str, strategy: str) -> int:
        return sum(
            1 for u in self.used
            if u["record"]["strategy"].get(dim) == strategy
        )

    def weight_for(self, dim: str, strategy: str) -> float:
        w = sum(
            u["weight"] for u in self.used
            if u["record"]["strategy"].get(dim) == strategy
        )
        return min(1.0, w)

    def weighted_latency(self, dim: str, strategy: str) -> Optional[float]:
        items = [(u["weight"], u["record"]["execution_feedback"]["latency_ms"])
                 for u in self.used if u["record"]["strategy"].get(dim) == strategy]
        if not items:
            return None
        total_w = sum(w for w, _ in items)
        return sum(w * lat for w, lat in items) / total_w if total_w else None

    def total_weight(self, dim: str) -> float:
        return min(1.0, sum(
            u["weight"] for u in self.used if u["record"]["strategy"].get(dim) is not None
        ))

    def best_latency(self, dim: str) -> Optional[float]:
        lats = [
            lat for u in self.used
            if u["record"]["strategy"].get(dim) is not None
            for lat in [u["record"]["execution_feedback"]["latency_ms"]]
        ]
        return min(lats) if lats else None


def build_history_evidence(records: list, current_compact: dict, current_db: dict,
                           current_sys: dict, reference_time: Optional[int]) -> HistoryEvidence:
    """汇总历史记录：相似度达标者参与决策，其余计入 below_threshold/invalid。"""
    ev = HistoryEvidence()
    for rec in records:
        if not _valid_record(rec):
            ev.invalid += 1
            continue
        sim = record_similarity(current_compact, current_db, current_sys, rec)
        if sim < SIMILARITY_THRESHOLD:
            ev.below_threshold += 1
            continue
        ev.used.append({
            "record_id": rec["record_id"],
            "similarity": round(sim, 4),
            "weight": round(sim * _decay(rec, reference_time), 4),
            "record": rec,
        })
    return ev
