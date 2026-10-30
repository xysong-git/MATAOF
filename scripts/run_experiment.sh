#!/usr/bin/env bash
# MATAOF 实验运行脚本：控制流 × LLM 的 2×2 对照 + 固定策略/原生基线
#   --mode native        纯原生 IoTDB（不走任何 Agent 路径）
#   --mode pipeline      固定流水线（四 Agent 组件，无 replan/recall），无 LLM（P0）
#   --mode pipeline-llm  固定流水线 + 共享 LLM（P1）
#   --mode agent         四 Agent 观察驱动闭环，无 LLM（A0）
#   --mode llm           四 Agent 观察驱动闭环 + 共享 LLM（A1，主实验）
#   --mode fixed         固定策略对照（Baseline 2）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IOTDB_BIN="${IOTDB_BIN:-/opt/program-code/iotdb/distribution/target/apache-iotdb-2.0.6-all-bin/apache-iotdb-2.0.6-all-bin}"
LLM_URL="${LLM_URL:-http://127.0.0.1:8000/v1/models}"

MODE=""
SCALES="dbs,dbm,dbl"
QUERY_DIR=""          # 指定查询目录（只跑该目录下 q*.sql；设置后忽略 --scales）
FILES=""              # 逗号分隔的 q 文件名（如 q1,q4,q7）；空 = 全部 q*.sql
PER_FILE_LIMIT=""     # 每个文件取样条数；空 = 全量
SAMPLE="seq"          # seq | random
SEED=42
WORKERS=1             # 查询级并行（B2；逐条真实 LLM 推理时可用 4，缓存/无 LLM 保持 1）
RESULT_MODE="row"     # native 专用：row=逐行（历史口径）/ df=DataFrame 批量（同 agent）

usage() {
  cat <<EOF
用法: $0 --mode native|pipeline|pipeline-llm|agent|llm|fixed [选项]
  --mode MODE           运行模式：native（纯原生 IoTDB）/
                        pipeline（固定流水线，无 LLM，P0）/
                        pipeline-llm（固定流水线 + LLM，P1）/
                        agent（四 Agent 闭环，无 LLM，A0）/
                        llm（四 Agent 闭环 + LLM，A1）/
                        fixed（固定策略对照，Baseline 2）
  --query-dir DIR       只跑指定查询目录（如 query/queries_dbs；设置后忽略 --scales，
                        规模从目录名 queries_<scale> 推导，用于选择配置文件）
  --scales S            规模，逗号分隔（默认 dbs,dbm,dbl）
  --files F             只跑指定查询文件，如 q1,q4,q7（默认该目录下全部 q*.sql）
  --per-file-limit N    每个文件取样条数（默认全量；所有模式都生效）
  --sample seq|random   取样方式（默认 seq）
  --seed N              随机取样种子（默认 42，可复现）
  --workers N           查询级并行（默认 1；pipeline/pipeline-llm/agent/llm/fixed 生效）
  --result-mode MODE    native 专用结果消费口径：row（逐行，默认；历史 baseline）
                        或 df（DataFrame 批量，与 agent 计划同口径；输出到
                        results_baseline_df/）
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --query-dir) QUERY_DIR="$2"; shift 2 ;;
    --scales) SCALES="$2"; shift 2 ;;
    --files) FILES="$2"; shift 2 ;;
    --per-file-limit) PER_FILE_LIMIT="$2"; shift 2 ;;
    --sample) SAMPLE="$2"; shift 2 ;;
    --seed) SEED="$2"; shift 2 ;;
    --workers) WORKERS="$2"; shift 2 ;;
    --result-mode) RESULT_MODE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

case "$MODE" in
  native|pipeline|pipeline-llm|agent|llm|fixed) ;;
  *) echo "缺少 --mode（native/pipeline/pipeline-llm/agent/llm/fixed）" >&2; usage; exit 2 ;;
esac

case "$RESULT_MODE" in
  row|df) ;;
  *) echo "未知 --result-mode: $RESULT_MODE（仅 row/df）" >&2; exit 2 ;;
esac
if [[ "$MODE" != "native" && "$RESULT_MODE" != "row" ]]; then
  echo "注意: --result-mode 仅 native 模式生效；pipeline/pipeline-llm/agent/llm/fixed 固定走计划 df 路径" >&2
fi

# LLM 模式预检：vLLM 不可用则直接失败（防止静默退化污染实验）
if [[ "$MODE" == "llm" || "$MODE" == "pipeline-llm" ]]; then
  if ! curl -s --max-time 3 "$LLM_URL" 2>/dev/null | grep -q '"id"'; then
    echo "错误: LLM 模式要求本地 vLLM 服务可用（$LLM_URL 无响应），请先启动 vLLM" >&2
    exit 3
  fi
  echo "[预检] vLLM 可用：$LLM_URL"
fi

# 组装查询目录列表：--query-dir 优先（单目录，忽略 --scales），否则按规模展开
qdirs=()
if [[ -n "$QUERY_DIR" ]]; then
  if [[ ! -d "$QUERY_DIR" ]]; then
    echo "错误: 查询目录不存在: $QUERY_DIR" >&2; exit 2
  fi
  qdirs+=("$QUERY_DIR")
else
  for scale in ${SCALES//,/ }; do
    qdirs+=("$ROOT/query/queries_$scale")
  done
fi

# 每轮统一 run_id：本轮所有文件共享，最终汇总按轮次过滤（历史批次不混入）
ROUND_ID="round-$(date +%Y%m%d-%H%M%S)"

start=$(date +%s)
# 收集本轮文件列表（sort -V 版本排序：q1,q2,...,q10,q11，而非字典序 q1,q10,q11,q2）
file_list=()
for qdir in "${qdirs[@]}"; do
  while IFS= read -r f; do
    file_list+=("$f")
  done < <(for f in "$qdir"/q*.sql; do
    name=$(basename "$f" .sql)
    if [[ -n "$FILES" ]] && [[ ",$FILES," != *",$name,"* ]]; then continue; fi
    echo "$f"
  done | sort -V)
done
total_files=${#file_list[@]}

if [[ "$MODE" == "native" ]]; then
  # native：逐文件客户端直发原始 SQL（与 agent 模式同一客户端/同一计时代码，
  # 采样与 agent 共用 sample_items——同种子同抽样，口径一致）
  # 双口径：row（历史 baseline，逐行）与 df（同 agent 计划消费方式）分目录存放，
  # 互不覆盖；每轮只清理当前口径目录，防残留文件被汇总重复计数。
  BASELINE_DIR="$ROOT/results_baseline"
  [[ "$RESULT_MODE" == "df" ]] && BASELINE_DIR="$ROOT/results_baseline_df"
  mkdir -p "$BASELINE_DIR"
  rm -f "$BASELINE_DIR"/*.jsonl
  echo "== native 结果消费口径: $RESULT_MODE（输出 $BASELINE_DIR）=="
  file_no=0
  for f in "${file_list[@]}"; do
    name=$(basename "$f" .sql)
    scale=$(basename "$(dirname "$f")" | sed -n 's/^queries_\(.*\)$/\1/p')
    [[ -n "$scale" ]] || scale=${SCALES%%,*}
    file_no=$((file_no + 1))
    echo "== 文件 [$file_no/$total_files] $scale/$name =="
    out="$BASELINE_DIR/${scale}_${name}.jsonl"
    python3 "$ROOT/scripts/run_native_client.py" --file "$f" --out "$out" \
      --result-mode "$RESULT_MODE" \
      ${PER_FILE_LIMIT:+--per-file-limit "$PER_FILE_LIMIT"} \
      --sample "$SAMPLE" --seed "$SEED"
  done
else
  # pipeline/pipeline-llm/agent/llm/fixed：每规模一次 mataof 调用（多文件并行处理；
  # 异步增强不与逐文件排空互相阻塞，文件间连续执行）
  for qdir in "${qdirs[@]}"; do
    scale=$(basename "$qdir" | sed -n 's/^queries_\(.*\)$/\1/p')
    [[ -n "$scale" ]] || scale=${SCALES%%,*}
    case "$MODE" in
      pipeline) cfg="config.$scale.pipeline.json" ;;
      pipeline-llm) cfg="config.$scale.pipeline-llm.json" ;;
      agent) cfg="config.$scale.json" ;;
      llm) cfg="config.$scale.llm.json" ;;
      fixed) cfg="config.$scale.fixed.json" ;;
    esac
    files_this_scale=()
    while IFS= read -r f; do
      files_this_scale+=("$f")
    done < <(for f in "$qdir"/q*.sql; do
      name=$(basename "$f" .sql)
      if [[ -n "$FILES" ]] && [[ ",$FILES," != *",$name,"* ]]; then continue; fi
      echo "$f"
    done | sort -V)
    echo "== 规模 $scale：${#files_this_scale[@]} 个文件（本轮共 $total_files 个文件）=="
    ( cd "$ROOT" && python3 -m mataof run --config "$cfg" \
        ${PER_FILE_LIMIT:+--per-file-limit "$PER_FILE_LIMIT"} \
        --sample "$SAMPLE" --seed "$SEED" --workers "$WORKERS" \
        --run-id "$ROUND_ID" --file "${files_this_scale[@]}" )
  done
fi

echo "本轮墙钟总用时: $(( $(date +%s) - start ))s"

# 汇总报告
run_logs=()
case "$MODE" in
  native)
    BASELINE_DIR="$ROOT/results_baseline"
    [[ "$RESULT_MODE" == "df" ]] && BASELINE_DIR="$ROOT/results_baseline_df"
    echo "== 原生 baseline 汇总（$RESULT_MODE 口径，客户端墙钟）=="
    python3 "$ROOT/scripts/aggregate_native.py" "$BASELINE_DIR"/*.jsonl
    ;;
  pipeline)
    echo "== Pipeline 模式汇总（P0：固定流水线，无 LLM；轮次 $ROUND_ID）=="
    run_logs=("$ROOT"/results/round_pipeline_{dbs,dbm,dbl}/run_log.jsonl)
    ;;
  pipeline-llm)
    echo "== Pipeline+LLM 模式汇总（P1：固定流水线 + LLM；轮次 $ROUND_ID）=="
    run_logs=("$ROOT"/results/round_pipeline_llm_{dbs,dbm,dbl}/run_log.jsonl)
    ;;
  agent)
    echo "== Agent 模式汇总（A0：四 Agent 闭环，无 LLM；轮次 $ROUND_ID）=="
    run_logs=("$ROOT"/results/round_agent_{dbs,dbm,dbl}/run_log.jsonl)
    ;;
  llm)
    echo "== Agent+LLM 模式汇总（A1：四 Agent 闭环 + LLM；轮次 $ROUND_ID）=="
    run_logs=("$ROOT"/results/round_{dbs,dbm,dbl}_llm/run_log.jsonl)
    ;;
  fixed)
    echo "== 固定策略（Baseline 2）模式汇总（轮次 $ROUND_ID）=="
    run_logs=("$ROOT"/results/round_fixed_{dbs,dbm,dbl}/run_log.jsonl)
    ;;
esac

if [[ ${#run_logs[@]} -gt 0 ]]; then
  python3 "$ROOT/scripts/aggregate_agent.py" --run-id "$ROUND_ID" \
    "${run_logs[@]}"
  echo "== Agent 行为剖面（$MODE；轮次 $ROUND_ID）=="
  python3 "$ROOT/scripts/agent_profile.py" --run-id "$ROUND_ID" \
    "${run_logs[@]}"
fi
