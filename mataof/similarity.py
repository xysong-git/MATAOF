"""历史记录相似度评估与加权（共享模块，历史知识使用原则的实现）。

本模块被 Knowledge Memory Agent（检索）与 Optimization Decision Agent（证据加权）
共同使用，保证两个 Agent 对"相似"的口径一致。

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
        return 1.0   # 双方都无聚合函数 = 完全匹配（"无聚合"是确定状态，不是未知）
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


def compact_features(analysis: dict) -> dict:
    """把 Query Analysis Agent 输出压缩为历史记录契约中的 query_features 摘要。

    历史记录与检索均使用该摘要做相似度计算（口径一致，见 HISTORICAL_RECORD_CONTRACT）。
    """
    f = analysis.get("query_features") or {}
    t = f.get("time") or {}
    d = f.get("device") or {}
    fl = f.get("filter") or {}
    agg = f.get("aggregation") or {}
    counts = fl.get("type_counts") or {}
    return {
        "query_type": analysis.get("query_type") or "unknown",
        "time": {
            "has_time_filter": bool(t.get("has_time_filter")),
            "time_span": t.get("time_span"),
            "range_level": t.get("range_level") or "unknown",
        },
        "device": {
            "device_count": d.get("device_count"),
            "multi_device": d.get("multi_device"),
        },
        "filter": {
            "non_time_filter_count": sum(v for k, v in counts.items() if k != "time"),
        },
        "aggregation": {
            "has_aggregation": bool(agg.get("has_aggregation")),
            "has_group_by": bool(agg.get("has_group_by")),
            "has_window": bool(agg.get("has_window")),
            "functions": sorted({r.get("function") for r in agg.get("aggregation_functions") or []}),
        },
    }


def _record_parts(record: dict) -> tuple:
    """兼容两种记录布局，直接读取嵌套字典（免重建 view，降低大库扫描开销）。

    决策契约布局（顶层 query_features/database_state/system_state）与
    KM 记录布局（query_record/context 嵌套）都支持。
    """
    qf = record.get("query_features")
    db = record.get("database_state")
    sy = record.get("system_state")
    if qf is None:
        qr = record.get("query_record")
        if isinstance(qr, dict):
            qf = qr.get("query_features")
    if db is None:
        ctx = record.get("context")
        if isinstance(ctx, dict):
            db = ctx.get("database_state")
    if sy is None:
        ctx = record.get("context")
        if isinstance(ctx, dict):
            sy = ctx.get("system_state")
    return qf or {}, db or {}, sy or {}


def component_similarities(current_compact: dict, current_db: dict, current_sys: dict,
                           record: dict) -> dict:
    """计算单条历史记录与当前上下文的分量相似度与综合相似度。

    返回 {"query": 0~1, "database": 0~1, "system": 0~1, "overall": 0~1}。
    """
    record_qf, record_db, record_sys = _record_parts(record)
    sim_q = _similarity_query(current_compact, record_qf)
    sim_db = _similarity_db(current_db, record_db)
    sim_sys = _similarity_system(current_sys, record_sys)
    overall = SIM_WEIGHT_QUERY * sim_q + SIM_WEIGHT_DB * sim_db + SIM_WEIGHT_SYSTEM * sim_sys
    return {
        "query": round(sim_q, 4),
        "database": round(sim_db, 4),
        "system": round(sim_sys, 4),
        "overall": round(overall, 4),
    }


def record_similarity(current_compact: dict, current_db: dict, current_sys: dict,
                      record: dict) -> float:
    """计算单条历史记录与当前上下文的综合相似度（0~1）。"""
    return component_similarities(current_compact, current_db, current_sys, record)["overall"]


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
    """汇总历史记录：相似度达标者参与决策，其余计入 below_threshold/invalid。

    H1：记录若携带预计算 `similarity`（KM 检索输出），直接复用；
    否则回退到现场计算（对无该字段的输入保持兼容）。
    """
    ev = HistoryEvidence()
    for rec in records:
        if not _valid_record(rec):
            ev.invalid += 1
            continue
        pre = rec.get("similarity")
        if isinstance(pre, (int, float)) and not isinstance(pre, bool) and 0.0 <= pre <= 1.0:
            sim = float(pre)                       # 预计算值，跳过重算
        else:
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


# ---------------------------------------------------------------------------
# 向量化相似度（路线 2：大知识库下替代逐条 Python 循环）
# ---------------------------------------------------------------------------

def _np():
    import numpy as _np
    return _np

# 查询特征字段契约（与 _QUERY_FIELDS 一一对应，权重必须一致）
_CAT_FIELDS = (
    ("query_type", 0.15), ("has_time_filter", 0.10), ("range_level", 0.10),
    ("multi_device", 0.05), ("has_aggregation", 0.10),
    ("has_group_by", 0.05), ("has_window", 0.05),
)
_NUM_FIELDS = (
    ("time_span", 0.10), ("device_count", 0.05), ("non_time_count", 0.10),
)
_FUNC_WEIGHT = 0.10
_QUERY_TOTAL_W = sum(w for _, w in _CAT_FIELDS) + sum(w for _, w in _NUM_FIELDS) + _FUNC_WEIGHT


def _extract_cat(compact: dict, field: str):
    """从 compact 特征取类别字段值（None=缺失）。"""
    if field == "query_type":
        return compact.get("query_type")
    t = compact.get("time") or {}
    d = compact.get("device") or {}
    f = compact.get("filter") or {}
    a = compact.get("aggregation") or {}
    if field == "has_time_filter":
        return t.get("has_time_filter")
    if field == "range_level":
        return t.get("range_level")
    if field == "multi_device":
        return d.get("multi_device")
    if field == "has_aggregation":
        return a.get("has_aggregation")
    if field == "has_group_by":
        return a.get("has_group_by")
    if field == "has_window":
        return a.get("has_window")
    return None


def _extract_num(compact: dict, field: str):
    if field == "time_span":
        return (compact.get("time") or {}).get("time_span")
    if field == "device_count":
        return (compact.get("device") or {}).get("device_count")
    if field == "non_time_count":
        v = (compact.get("filter") or {}).get("non_time_filter_count")
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return None


def _code_of(value, code_map: dict, next_code: list) -> int:
    """值 → 稳定整数码（同进程内一致；新增值分配新码）。"""
    if value is None:
        return -1
    key = repr(value)
    if key not in code_map:
        code_map[key] = next_code[0]
        next_code[0] += 1
    return code_map[key]


def encode_vector_row(record_parts: tuple, code_maps: list, next_code: list,
                      func_dict: list) -> tuple:
    """把一条记录编码为矩阵行：返回 (cat_codes, num_values, func_set)。

    record_parts = (query_features_compact, database_state, system_state)。
    与逐条计算完全一致的取值规则；未知/缺失 → -1 / NaN。
    """
    qf, db, sy = record_parts
    np = _np()

    cat_codes = np.empty(len(_CAT_FIELDS), dtype=np.int16)
    for i, (field, _w) in enumerate(_CAT_FIELDS):
        cat_codes[i] = _code_of(_extract_cat(qf, field), code_maps[i], next_code)

    num_values = np.full(len(_NUM_FIELDS), np.nan, dtype=np.float32)
    for i, (field, _w) in enumerate(_NUM_FIELDS):
        v = _extract_num(qf, field)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            num_values[i] = float(v)

    funcs = (qf.get("aggregation") or {}).get("functions") or []
    func_set = set(funcs) if isinstance(funcs, (list, set, tuple)) else set()
    for fn in func_set:
        if fn not in func_dict:
            func_dict.append(fn)
    return cat_codes, num_values, func_set


def build_vector_matrix(store) -> Optional[dict]:
    """从知识库构建（或增量更新）向量矩阵。

    返回 {"records": [...], "cat_codes": N×7 int16, "num_values": N×3 float32,
          "func_mat": N×D uint8, "func_dict": [...], "code_maps": [...],
          "db_codes": N×3, "sys_codes": N×1}
    db_codes 三列：scale_level 码、scale_level 存在位、device_scale 值(NaN=缺失)
    """
    np = _np()
    records = store.all()
    n = len(records)
    if n == 0:
        return None

    # 增量：已缓存的矩阵只追加新记录行
    mat = getattr(store, "_vec_mat", None)
    if mat is not None and mat.get("_len") == n:
        return mat

    start = mat["_len"] if mat is not None else 0
    # code_maps：7 个查询类别字段 + db 的 scale_level + sys 的 load_level 共 9 张
    code_maps = mat["code_maps"] if mat is not None else [{} for _ in range(len(_CAT_FIELDS) + 2)]
    next_code = [max((max(m.values(), default=-1) + 1) for m in code_maps)]
    func_dict = list(mat["func_dict"]) if mat is not None else []
    old_func_len = len(func_dict)

    new_cats, new_nums, new_funcs = [], [], []
    for rec in records[start:]:
        parts = _record_parts(rec)
        c, v, fs = encode_vector_row(parts, code_maps, next_code, func_dict)
        new_cats.append(c)
        new_nums.append(v)
        new_funcs.append(fs)

    n_new = n - start
    if mat is None:
        cat_codes = np.stack(new_cats) if new_cats else np.empty((0, len(_CAT_FIELDS)), np.int16)
        num_values = np.stack(new_nums) if new_nums else np.empty((0, len(_NUM_FIELDS)), np.float32)
        db_codes = np.zeros((n, 3), np.float32)
        sys_codes = np.zeros((n, 1), np.int16)
        func_mat = np.zeros((n, max(len(func_dict), 1)), np.uint8)
    else:
        cat_codes = np.vstack([mat["cat_codes"], np.stack(new_cats)]) if new_cats else mat["cat_codes"]
        num_values = np.vstack([mat["num_values"], np.stack(new_nums)]) if new_nums else mat["num_values"]
        db_codes = (np.vstack([mat["db_codes"], np.zeros((n_new, 3), np.float32)])
                    if n_new else mat["db_codes"])
        sys_codes = (np.vstack([mat["sys_codes"], np.zeros((n_new, 1), np.int16)])
                     if n_new else mat["sys_codes"])
        # 函数矩阵：先给旧行补齐新增字典列，再拼接新行（vstack 要求宽度一致）
        func_mat = mat["func_mat"]
        if len(func_dict) > func_mat.shape[1]:
            pad = np.zeros((func_mat.shape[0], len(func_dict) - func_mat.shape[1]), np.uint8)
            func_mat = np.hstack([func_mat, pad])
        if n_new:
            func_mat = np.vstack([func_mat, np.zeros((n_new, len(func_dict) or 1), np.uint8)])
    # 为新行填函数位
    for j, fs in enumerate(new_funcs):
        row_idx = start + j
        for fn in fs:
            func_mat[row_idx, func_dict.index(fn)] = 1
    # db/sys 列：为新行填充
    for j, rec in enumerate(records[start:]):
        db = (rec.get("context") or {}).get("database_state") or {}
        sy = (rec.get("context") or {}).get("system_state") or {}
        row_idx = start + j
        db_codes[row_idx, 0] = _code_of(db.get("data_scale_level"), code_maps[len(_CAT_FIELDS)],
                                        next_code)
        db_codes[row_idx, 1] = 1.0 if db.get("data_scale_level") is not None else 0.0
        ds = db.get("device_scale")
        db_codes[row_idx, 2] = float(ds) if isinstance(ds, (int, float)) and not isinstance(ds, bool) else np.nan
        ll = sy.get("load_level")
        sys_codes[row_idx, 0] = _code_of(ll, code_maps[len(_CAT_FIELDS) + 1], next_code)

    mat = {
        "_len": n,
        "records": records,
        "cat_codes": cat_codes,
        "num_values": num_values,
        "func_mat": func_mat,
        "func_dict": func_dict,
        "code_maps": code_maps,
        "db_codes": db_codes,
        "sys_codes": sys_codes,
    }
    store._vec_mat = mat
    return mat


def encode_current_query(current_compact: dict, current_db: dict, current_sys: dict,
                         code_maps: list, func_dict: list) -> tuple:
    """把当前查询上下文编码为与矩阵同布局的向量。"""
    np = _np()
    next_code = [max((max(m.values(), default=-1) + 1) for m in code_maps)]
    cur_cats = np.empty(len(_CAT_FIELDS), np.int16)
    for i, (field, _w) in enumerate(_CAT_FIELDS):
        cur_cats[i] = _code_of(_extract_cat(current_compact or {}, field),
                               code_maps[i], next_code)
    cur_nums = np.full(len(_NUM_FIELDS), np.nan, np.float32)
    for i, (field, _w) in enumerate(_NUM_FIELDS):
        v = _extract_num(current_compact or {}, field)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            cur_nums[i] = float(v)
    funcs = ((current_compact or {}).get("aggregation") or {}).get("functions") or []
    cur_func = np.zeros(max(len(func_dict), 1), np.float32)   # 与 func_mat 列宽一致（含空字典）
    for fn in funcs:
        if fn in func_dict:
            cur_func[func_dict.index(fn)] = 1.0
    cur_func_total = len(funcs)   # 当前函数总数（含字典外的名字——它们只增大并集）
    cur_db = np.zeros(3, np.float32)
    db = current_db or {}
    cur_db[0] = _code_of(db.get("data_scale_level"), code_maps[len(_CAT_FIELDS)], next_code)
    cur_db[1] = 1.0 if db.get("data_scale_level") is not None else 0.0
    ds = db.get("device_scale")
    cur_db[2] = float(ds) if isinstance(ds, (int, float)) and not isinstance(ds, bool) else np.nan
    sy = current_sys or {}
    cur_sys = _code_of(sy.get("load_level"), code_maps[len(_CAT_FIELDS) + 1], next_code)
    return cur_cats, cur_nums, cur_func, cur_func_total, cur_db, cur_sys


def vectorized_similarities(current_compact: dict, current_db: dict, current_sys: dict,
                            mat: dict) -> tuple:
    """矩阵化计算全部记录的 (query, database, system, overall) 相似度数组。

    语义与逐条 component_similarities 完全一致（测试保证等价）；
    返回未取整的 float32 数组，调用方按需 round。
    """
    np = _np()
    cur_c, cur_n, cur_f, cur_f_total, cur_db, cur_sys = encode_current_query(
        current_compact, current_db, current_sys, mat["code_maps"], mat["func_dict"])
    n = mat["cat_codes"].shape[0]
    q = np.zeros(n, np.float32)

    for i, (_field, w) in enumerate(_CAT_FIELDS):
        c = int(cur_c[i])
        col = mat["cat_codes"][:, i].astype(np.float32)
        q += w * np.where((col < 0) | (c < 0), 0.5, (col == c).astype(np.float32))

    for i, (_field, w) in enumerate(_NUM_FIELDS):
        V = mat["num_values"][:, i]
        v = float(cur_n[i])
        if np.isnan(v):
            # 当前缺失：记录缺失 → 双方缺失 0.5；记录存在 → 单方缺失 0.3
            q += w * np.where(np.isnan(V), 0.5, 0.3).astype(np.float32)
        else:
            # 当前存在：记录缺失 → 单方缺失 0.3（保持 0.3 基准）；其余按规则
            s = np.full(n, 0.3, np.float32)
            p = ~np.isnan(V)
            Vp = V[p]
            both_zero = (Vp == 0) & (v == 0)
            with np.errstate(invalid="ignore", divide="ignore"):
                ratio = np.minimum(Vp, v) / np.maximum(Vp, v)
            s[p] = np.where(both_zero, 1.0,
                            np.where((Vp <= 0) | (v <= 0), 0.0, ratio)).astype(np.float32)
            q += w * s

    F = mat["func_mat"].astype(np.float32)
    inter = F @ cur_f
    rowsum = F.sum(axis=1)
    union = rowsum + cur_f_total - inter
    s_f = np.where(union > 0, inter / np.maximum(union, 1e-12),
                   np.where(rowsum == 0, 1.0, 0.0)).astype(np.float32)
    q += _FUNC_WEIGHT * s_f
    q /= _QUERY_TOTAL_W

    # ---- database ----
    db_col0 = mat["db_codes"][:, 0].astype(np.float32)
    has_s = mat["db_codes"][:, 1] == 1.0
    c_s = int(cur_db[0])
    cur_has_s = cur_db[1] == 1.0
    common_s = has_s & cur_has_s
    s_s = np.where((db_col0 < 0) | (c_s < 0), 0.5, (db_col0 == c_s).astype(np.float32))
    dv = mat["db_codes"][:, 2]
    has_d = ~np.isnan(dv)
    c_dv = float(cur_db[2])
    cur_has_d = not np.isnan(c_dv)
    common_d = has_d & cur_has_d
    s_d = np.full(n, 0.3, np.float32)
    if cur_has_d:
        # 当前存在：记录缺失 → 单方缺失 0.3（保持基准）；其余按规则
        p = has_d
        Vp = dv[p]
        both_zero = (Vp == 0) & (c_dv == 0)
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio = np.minimum(Vp, c_dv) / np.maximum(Vp, c_dv)
        s_d[p] = np.where(both_zero, 1.0,
                          np.where((Vp <= 0) | (c_dv <= 0), 0.0, ratio)).astype(np.float32)
    else:
        # 当前缺失：记录缺失 → 0.5；记录存在 → 0.3
        s_d = np.where(has_d, 0.3, 0.5).astype(np.float32)
    total_w = 0.5 * common_s.astype(np.float32) + 0.5 * common_d.astype(np.float32)
    db_sim = np.where(total_w > 0,
                      (0.5 * common_s * s_s + 0.5 * common_d * s_d) / np.maximum(total_w, 1e-12),
                      0.5).astype(np.float32)

    # ---- system ----
    sc = mat["sys_codes"][:, 0].astype(np.float32)
    sys_sim = np.where((sc < 0) | (cur_sys < 0), 0.5, (sc == cur_sys).astype(np.float32))

    overall = (SIM_WEIGHT_QUERY * q + SIM_WEIGHT_DB * db_sim
               + SIM_WEIGHT_SYSTEM * sys_sim).astype(np.float32)
    return q, db_sim, sys_sim, overall
