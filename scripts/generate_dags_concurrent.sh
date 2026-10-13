#!/usr/bin/env bash
# 并发生成 DAG；每个任务独立调用 dag_factory，日志写入单独文件。
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

CATEGORIES="1-16"
PER_CATEGORY=1
WORKERS=4
OUT_DIR="$PROJECT_ROOT/dags/concurrent"
LOG_DIR="$PROJECT_ROOT/dags/concurrent-logs"
BASE_SEED=20260712
ATTEMPTS=5
PERSONA=""
PERSONA_FILE=""
LIMIT=""
SHUFFLE=0
FORCE=0
DRY_RUN=0

usage() {
  cat <<'EOF'
用法: scripts/generate_dags_concurrent.sh [选项]

只并发生成 DAG，不生成 World。每张 DAG 和日志均使用独立文件。

两种模式（二选一）:
  A) 领域模式（默认）: 按 --categories × --per-category 铺开。
  B) persona 模式: 给 --persona-file，把 jsonl 切成 --workers 份分片并发，每 persona 一张。

选项:
  --categories SPEC     [领域模式] 领域编号，如 1-16、1,4,7 或 1-4,8（默认: 1-16）
  --per-category N      [领域模式] 每个领域生成 N 张（默认: 1）
  --persona-file PATH   [persona 模式] persona jsonl（每行 {"persona": ...}）
  --limit N             [persona 模式] 全局只取前 N 条 persona（分片前截断）
  --shuffle             [persona 模式] 按 --seed 打乱后再取/分片
  --workers N           最大并发数（默认: 4）
  --out-dir DIR         DAG 输出目录（默认: dags/concurrent）
  --log-dir DIR         日志目录（默认: dags/concurrent-logs）
  --seed N              基础随机种子（默认: 20260712）
  --attempts N          每张 DAG 的校验重试上限（默认: 5）
  --persona TEXT        [领域模式] 所有 DAG 共用的分析者画像（默认由 dag_factory 选择）
  --force               覆盖已存在的 DAG
  --dry-run             只打印计划，不运行
  -h, --help            显示帮助

示例:
  scripts/generate_dags_concurrent.sh --categories 1-4 --per-category 2 --workers 4
  scripts/generate_dags_concurrent.sh --persona-file your_personas.jsonl --limit 200 --workers 8
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
require_positive() { [[ "$2" =~ ^[1-9][0-9]*$ ]] || die "$1 必须是正整数"; }
resolve_path() { [[ "$1" = /* ]] && printf '%s\n' "$1" || printf '%s/%s\n' "$PROJECT_ROOT" "$1"; }

while (($#)); do
  case "$1" in
    --categories) (($# >= 2)) || die "$1 缺少参数"; CATEGORIES="$2"; shift 2 ;;
    --per-category) (($# >= 2)) || die "$1 缺少参数"; PER_CATEGORY="$2"; shift 2 ;;
    --workers) (($# >= 2)) || die "$1 缺少参数"; WORKERS="$2"; shift 2 ;;
    --out-dir) (($# >= 2)) || die "$1 缺少参数"; OUT_DIR="$(resolve_path "$2")"; shift 2 ;;
    --log-dir) (($# >= 2)) || die "$1 缺少参数"; LOG_DIR="$(resolve_path "$2")"; shift 2 ;;
    --seed) (($# >= 2)) || die "$1 缺少参数"; BASE_SEED="$2"; shift 2 ;;
    --attempts) (($# >= 2)) || die "$1 缺少参数"; ATTEMPTS="$2"; shift 2 ;;
    --persona) (($# >= 2)) || die "$1 缺少参数"; PERSONA="$2"; shift 2 ;;
    --persona-file) (($# >= 2)) || die "$1 缺少参数"; PERSONA_FILE="$(resolve_path "$2")"; shift 2 ;;
    --limit) (($# >= 2)) || die "$1 缺少参数"; LIMIT="$2"; shift 2 ;;
    --shuffle) SHUFFLE=1; shift ;;
    --force) FORCE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

require_positive --per-category "$PER_CATEGORY"
require_positive --workers "$WORKERS"
require_positive --attempts "$ATTEMPTS"
[[ "$BASE_SEED" =~ ^-?[0-9]+$ ]] || die "--seed 必须是整数"
[[ -z "$LIMIT" ]] || require_positive --limit "$LIMIT"
if [[ -n "$PERSONA_FILE" ]]; then
  [[ -s "$PERSONA_FILE" ]] || die "persona 文件不存在或为空: $PERSONA_FILE"
fi

mkdir -p "$OUT_DIR" "$LOG_DIR"
cd "$PROJECT_ROOT"

# ---------------- persona 模式：把 jsonl 切成 WORKERS 份分片并发 ----------------
if [[ -n "$PERSONA_FILE" ]]; then
  SHARD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/dagfac-shards.XXXXXX")"
  trap 'rm -rf "$SHARD_DIR"' EXIT

  # 复用 dag_factory 的 load_personas 做「打乱→截断」，保证与 CLI 口径一致，再按行轮转切片。
  n_written=$(SHARD_DIR="$SHARD_DIR" PERSONA_FILE="$PERSONA_FILE" WORKERS="$WORKERS" \
    LIMIT="$LIMIT" SHUFFLE="$SHUFFLE" BASE_SEED="$BASE_SEED" python3 - <<'PY'
import json, os
from pathlib import Path
from dag_factory.envfile import load_personas_indexed

shard_dir = Path(os.environ["SHARD_DIR"])
workers = int(os.environ["WORKERS"])
limit = int(os.environ["LIMIT"]) if os.environ.get("LIMIT") else None
shuffle = os.environ["SHUFFLE"] == "1"
seed = int(os.environ["BASE_SEED"])

# 带全局索引：轮转切片后，每行透传 index，使同一 persona 在任何分片都映射到
# 固定输出文件 p{index:06d}.json —— 断点续跑与分片间去重的基石。
items = load_personas_indexed(Path(os.environ["PERSONA_FILE"]), limit=limit, shuffle=shuffle, seed=seed)
shards = min(workers, len(items)) or 0
handles = [open(shard_dir / f"shard_{i:03d}.jsonl", "w", encoding="utf-8") for i in range(shards)]
for k, (index, persona) in enumerate(items):
    handles[k % shards].write(json.dumps({"persona": persona, "index": index}, ensure_ascii=False) + "\n")
for h in handles:
    h.close()
print(shards)
PY
  ) || die "persona 分片失败（检查 PYTHONPATH / persona 文件格式）"
  ((n_written > 0)) || die "persona 文件里没有可用的 persona"

  failed=0
  pids=()
  for ((s=0; s<n_written; s++)); do
    shard="$(printf '%s/shard_%03d.jsonl' "$SHARD_DIR" "$s")"
    log="$(printf '%s/persona_shard_%03d.log' "$LOG_DIR" "$s")"
    seed=$((BASE_SEED + s))
    cmd=(python3 -m dag_factory --persona-file "$shard" --out-dir "$OUT_DIR"
         --seed "$seed" --attempts "$ATTEMPTS")
    if ((DRY_RUN)); then
      printf '[plan] '; printf '%q ' "${cmd[@]}"; printf '> %q 2>&1\n' "$log"
      continue
    fi
    printf '[start] persona 分片 %d/%d\n' "$((s + 1))" "$n_written"
    ( "${cmd[@]}" >"$log" 2>&1 && printf '[ok] persona 分片 %d -> %s\n' "$s" "$OUT_DIR" \
        || { printf '[fail] persona 分片 %d (log=%s)\n' "$s" "$log" >&2; exit 1; } ) &
    pids+=("$!")
    if ((${#pids[@]} >= WORKERS)); then
      wait "${pids[0]}" || ((failed+=1))
      pids=("${pids[@]:1}")
    fi
  done
  for pid in "${pids[@]}"; do wait "$pid" || ((failed+=1)); done
  printf '\n完成: persona 分片=%d, 失败=%d, DAG=%s, 日志=%s\n' "$n_written" "$failed" "$OUT_DIR" "$LOG_DIR"
  ((failed == 0)); exit
fi

# ---------------- 领域模式 ----------------
parse_categories() {
  local spec="$1" part start end i
  local -A seen=()
  IFS=',' read -ra parts <<< "$spec"
  for part in "${parts[@]}"; do
    if [[ "$part" =~ ^([0-9]+)-([0-9]+)$ ]]; then
      start="${BASH_REMATCH[1]}"; end="${BASH_REMATCH[2]}"
      ((start <= end)) || die "领域范围起点不能大于终点: $part"
      for ((i=start; i<=end; i++)); do seen[$i]=1; done
    elif [[ "$part" =~ ^[0-9]+$ ]]; then
      seen[$part]=1
    else
      die "非法 --categories: $part"
    fi
  done
  for i in "${!seen[@]}"; do ((i >= 1 && i <= 16)) || die "领域编号越界: $i"; done
  for ((i=1; i<=16; i++)); do [[ -n "${seen[$i]:-}" ]] && printf '%s\n' "$i"; done
}

mapfile -t CATEGORY_LIST < <(parse_categories "$CATEGORIES")
((${#CATEGORY_LIST[@]} > 0)) || die "没有可运行的领域"

run_job() {
  local category="$1" index="$2" ordinal="$3"
  local name out log seed
  name="$(printf 'cat%02d_%03d' "$category" "$index")"
  out="$OUT_DIR/$name.json"
  log="$LOG_DIR/$name.log"
  seed=$((BASE_SEED + ordinal))

  if [[ -s "$out" && "$FORCE" -eq 0 ]]; then
    printf '[skip] %s 已存在\n' "$out"
    return 0
  fi

  local -a cmd=(python3 -m dag_factory --category "$category" --out "$out" --seed "$seed" --attempts "$ATTEMPTS")
  [[ -n "$PERSONA" ]] && cmd+=(--persona "$PERSONA")
  if ((DRY_RUN)); then
    printf '[plan] '; printf '%q ' "${cmd[@]}"; printf '> %q 2>&1\n' "$log"
    return 0
  fi

  printf '[start] %s\n' "$name"
  if "${cmd[@]}" >"$log" 2>&1; then
    printf '[ok] %s -> %s\n' "$name" "$out"
  else
    local code=$?
    rm -f "$out"
    printf '[fail] %s (exit=%d, log=%s)\n' "$name" "$code" "$log" >&2
    return "$code"
  fi
}

failed=0
ordinal=0
pids=()
for category in "${CATEGORY_LIST[@]}"; do
  for ((index=1; index<=PER_CATEGORY; index++)); do
    run_job "$category" "$index" "$ordinal" &
    pids+=("$!")
    ((ordinal+=1))
    if ((${#pids[@]} >= WORKERS)); then
      wait "${pids[0]}" || ((failed+=1))
      pids=("${pids[@]:1}")
    fi
  done
done
for pid in "${pids[@]}"; do wait "$pid" || ((failed+=1)); done

printf '\n完成: 总计=%d, 失败=%d, DAG=%s, 日志=%s\n' "$ordinal" "$failed" "$OUT_DIR" "$LOG_DIR"
((failed == 0))
