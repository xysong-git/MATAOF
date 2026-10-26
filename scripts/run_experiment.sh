#!/usr/bin/env bash
# MATAOF 实验运行脚本：三种模式切换
#   --mode native  纯原生 IoTDB（不走任何 Agent 路径）
#   --mode agent   Agent 确定性链路（无 LLM）
#   --mode llm     Agent + 本地 vLLM 推理
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
WORKERS=1             # 查询级并行（B2；LLM 模式建议 4）

usage() {
  cat <<EOF
用法: $0 --mode native|agent|llm|fixed [选项]
  --mode MODE           运行模式：native（纯原生 IoTDB）/ agent（Agent 无 LLM）/
                        llm（Agent+LLM，异步增强）/ fixed（固定策略对照，Baseline 2）
  --query-dir DIR       只跑指定查询目录（如 query/queries_dbs；设置后忽略 --scales，
                        规模从目录名 queries_<scale> 推导，用于选择配置文件）
  --scales S            规模，逗号分隔（默认 dbs,dbm,dbl）
  --files F             只跑指定查询文件，如 q1,q4,q7（默认该目录下全部 q*.sql）
  --per-file-limit N    每个文件取样条数（默认全量；所有模式都生效）
  --sample seq|random   取样方式（默认 seq）
  --seed N              随机取样种子（默认 42，可复现）
  --workers N           查询级并行（默认 1；agent/llm/fixed 模式生效）
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
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

case "$MODE" in
  native|agent|llm|fixed) ;;
  *) echo "缺少 --mode（native/agent/llm/fixed）" >&2; usage; exit 2 ;;
esac

# llm 模式预检：vLLM 不可用则直接失败（防止静默退化为 agent 模式污染实验）
if [[ "$MODE" == "llm" ]]; then
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
  # 每轮开始前清理旧 .jsonl：防其他规模/历史轮的残留文件被汇总重复计数
  rm -f "$ROOT"/results_baseline/*.jsonl
  file_no=0
  for f in "${file_list[@]}"; do
    name=$(basename "$f" .sql)
    scale=$(basename "$(dirname "$f")" | sed -n 's/^queries_\(.*\)$/\1/p')
    [[ -n "$scale" ]] || scale=${SCALES%%,*}
    file_no=$((file_no + 1))
    echo "== 文件 [$file_no/$total_files] $scale/$name =="
    out="$ROOT/results_baseline/${scale}_${name}.jsonl"
    python3 "$ROOT/scripts/run_native_client.py" --file "$f" --out "$out" \
      ${PER_FILE_LIMIT:+--per-file-limit "$PER_FILE_LIMIT"} \
      --sample "$SAMPLE" --seed "$SEED"
  done
else
  # agent/llm/fixed：每规模一次 mataof 调用（多文件并行处理；
  # 异步增强不与逐文件排空互相阻塞，文件间连续执行）
  for qdir in "${qdirs[@]}"; do
    scale=$(basename "$qdir" | sed -n 's/^queries_\(.*\)$/\1/p')
    [[ -n "$scale" ]] || scale=${SCALES%%,*}
    cfg="config.$scale.json"
    [[ "$MODE" == "llm" ]] && cfg="config.$scale.llm.json"
    [[ "$MODE" == "fixed" ]] && cfg="config.$scale.fixed.json"
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
case "$MODE" in
  native)
    echo "== 原生 baseline 汇总（客户端墙钟，与 agent 同口径）=="
    python3 "$ROOT/scripts/aggregate_native.py" "$ROOT"/results_baseline/*.jsonl
    ;;
  agent)
    echo "== Agent 模式汇总（轮次 $ROUND_ID）=="
    python3 "$ROOT/scripts/aggregate_agent.py" --run-id "$ROUND_ID" \
      "$ROOT"/results/round_{dbs,dbm,dbl}/run_log.jsonl
    ;;
  llm)
    echo "== Agent+LLM 模式汇总（轮次 $ROUND_ID）=="
    python3 "$ROOT/scripts/aggregate_agent.py" --run-id "$ROUND_ID" \
      "$ROOT"/results/round_{dbs,dbm,dbl}_llm/run_log.jsonl
    ;;
  fixed)
    echo "== 固定策略（Baseline 2）模式汇总（轮次 $ROUND_ID）=="
    python3 "$ROOT/scripts/aggregate_agent.py" --run-id "$ROUND_ID" \
      "$ROOT"/results/round_fixed_{dbs,dbm,dbl}/run_log.jsonl
    ;;
esac
