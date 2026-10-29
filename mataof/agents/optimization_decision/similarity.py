"""兼容 shim：相似度逻辑已上移到共享模块 mataof/similarity.py。

保留本文件是为了兼容既有导入路径（scoring.py / confidence.py / agent.py），
Knowledge Memory Agent 与 Optimization Decision Agent 使用同一套相似度口径。
"""

from mataof.similarity import (  # noqa: F401
    BASELINE_SAMPLE_SHARE,
    DECAY_HALF_LIFE_HOURS,
    HistoryEvidence,
    MIN_BASELINE_SAMPLES,
    SIMILARITY_THRESHOLD,
    SIM_WEIGHT_DB,
    SIM_WEIGHT_QUERY,
    SIM_WEIGHT_SYSTEM,
    baseline_sample_adequate,
    build_history_evidence,
    component_similarities,
    record_similarity,
    same_database_context,
    same_query_shape,
    same_shape_evidence,
)
