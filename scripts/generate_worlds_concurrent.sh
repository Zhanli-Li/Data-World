#!/usr/bin/env bash
# 扫描已有 DAG 并并发生成 World；不生成或修改 DAG。
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

DAGS_DIR="$PROJECT_ROOT/dags/concurrent"
OUT_DIR="$PROJECT_ROOT/outputs/concurrent"
LOG_DIR="$PROJECT_ROOT/outputs/concurrent-logs"
WORKERS=2
MODEL="ops"
PERSONA="通用数据分析师"
PERSONA_MAP=""
BASE_SEED=20260712
MAX_TURNS=300
TIMEOUT=21600
LOG_TRAJECTORIES=0
CCR_BASE_PORT=3456
CCR_BIN=""
FORCE=0
DRY_RUN=0

usage() {
  cat <<'EOF'
用法: scripts/generate_worlds_concurrent.sh [选项]

只扫描已有 *.json DAG 并并发生成 World，不生成或修改 DAG。
输出目录名与 DAG 文件名一致。已有完整 World 会跳过，存在进度文件的未完成 World 会自动 --resume。

选项:
  --dags-dir DIR        DAG 输入目录（默认: dags/concurrent）
  --out-dir DIR         World 输出根目录（默认: outputs/concurrent）
  --log-dir DIR         日志目录（默认: outputs/concurrent-logs）
  --workers N           最大并发数（默认: 2）
  --model NAME          Claude Code agent 模型或别名（默认: ops）
  --persona TEXT        World 分析者画像（默认: 通用数据分析师）
  --persona-map FILE    逐 DAG 画像 jsonl（含 dag_file/persona 字段）；命中时覆盖 --persona
  --seed N              基础随机种子（默认: 20260712）
  --max-turns N         单阶段最大 turns（默认: 300）
  --timeout N           单阶段超时秒数（默认: 21600）
  --log-trajectories    采集轨迹；每个并发任务使用独立 CCR 端口
  --ccr-base-port PORT  CCR 起始端口（默认: 3456）
  --ccr-bin PATH        claude-code-router 可执行文件（默认: data_world 内建默认值）
  --force               删除并重建已有 World；与自动 --resume 二选一
  --dry-run             只打印计划，不运行
  -h, --help            显示帮助

示例:
  scripts/generate_worlds_concurrent.sh --workers 2
  scripts/generate_worlds_concurrent.sh --dags-dir dags/concurrent --log-trajectories
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
require_positive() { [[ "$2" =~ ^[1-9][0-9]*$ ]] || die "$1 必须是正整数"; }
resolve_path() { [[ "$1" = /* ]] && printf '%s\n' "$1" || printf '%s/%s\n' "$PROJECT_ROOT" "$1"; }

while (($#)); do
  case "$1" in
    --dags-dir) (($# >= 2)) || die "$1 缺少参数"; DAGS_DIR="$(resolve_path "$2")"; shift 2 ;;
    --out-dir) (($# >= 2)) || die "$1 缺少参数"; OUT_DIR="$(resolve_path "$2")"; shift 2 ;;
    --log-dir) (($# >= 2)) || die "$1 缺少参数"; LOG_DIR="$(resolve_path "$2")"; shift 2 ;;
    --workers) (($# >= 2)) || die "$1 缺少参数"; WORKERS="$2"; shift 2 ;;
    --model) (($# >= 2)) || die "$1 缺少参数"; MODEL="$2"; shift 2 ;;
    --persona) (($# >= 2)) || die "$1 缺少参数"; PERSONA="$2"; shift 2 ;;
    --persona-map) (($# >= 2)) || die "$1 缺少参数"; PERSONA_MAP="$(resolve_path "$2")"; shift 2 ;;
    --seed) (($# >= 2)) || die "$1 缺少参数"; BASE_SEED="$2"; shift 2 ;;
    --max-turns) (($# >= 2)) || die "$1 缺少参数"; MAX_TURNS="$2"; shift 2 ;;
    --timeout) (($# >= 2)) || die "$1 缺少参数"; TIMEOUT="$2"; shift 2 ;;
    --log-trajectories) LOG_TRAJECTORIES=1; shift ;;
    --ccr-base-port) (($# >= 2)) || die "$1 缺少参数"; CCR_BASE_PORT="$2"; shift 2 ;;
    --ccr-bin) (($# >= 2)) || die "$1 缺少参数"; CCR_BIN="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

require_positive --workers "$WORKERS"
require_positive --max-turns "$MAX_TURNS"
require_positive --timeout "$TIMEOUT"
[[ "$BASE_SEED" =~ ^-?[0-9]+$ ]] || die "--seed 必须是整数"
[[ "$CCR_BASE_PORT" =~ ^[0-9]+$ ]] || die "--ccr-base-port 必须是整数"
((CCR_BASE_PORT >= 1)) || die "CCR 起始端口越界"
[[ -d "$DAGS_DIR" ]] || die "DAG 目录不存在: $DAGS_DIR"
mkdir -p "$OUT_DIR" "$LOG_DIR"
cd "$PROJECT_ROOT"

# 单实例锁：防止多编排器并行导致同 world 重复开工（锁随进程退出自动释放）。
exec 9>"$LOG_DIR/.orchestrator.lock"
flock -n 9 || die "已有另一个编排器在运行（锁: $LOG_DIR/.orchestrator.lock）"

# 逐 DAG 画像映射：jsonl 每行需含 dag_file 与 persona 字段；命中文件名时覆盖 --persona。
declare -A PERSONA_MAP_ARR=()
if [[ -n "$PERSONA_MAP" ]]; then
  [[ -f "$PERSONA_MAP" ]] || die "--persona-map 文件不存在: $PERSONA_MAP"
  while IFS=$'\t' read -r _f _p; do
    [[ -n "$_f" ]] && PERSONA_MAP_ARR["$_f"]="$_p"
  done < <(python3 - "$PERSONA_MAP" <<'PY'
import json
import sys
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    r = json.loads(line)
    persona = str(r["persona"]).replace("\t", " ").replace("\n", " ")
    print(f"{r['dag_file']}\t{persona}")
PY
)
  ((${#PERSONA_MAP_ARR[@]} > 0)) || die "--persona-map 未解析出任何映射: $PERSONA_MAP"
  printf '[map] 载入 persona 映射 %d 条 <- %s\n' "${#PERSONA_MAP_ARR[@]}" "$PERSONA_MAP"
fi

DAGS=()
while IFS= read -r -d '' _f; do DAGS+=("$_f"); done < <(find "$DAGS_DIR" -maxdepth 1 -type f -name '*.json' -print0 | sort -z)
((${#DAGS[@]} > 0)) || die "DAG 目录中没有 *.json: $DAGS_DIR"
((CCR_BASE_PORT + ${#DAGS[@]} - 1 <= 65535)) || die "CCR 端口范围越界"

world_complete() {
  local world="$1" progress="$world/.build/progress.json"
  [[ -s "$world/WORLD.md" && -d "$world/env" && -s "$progress" ]] || return 1
  python3 - "$progress" <<'PY' >/dev/null 2>&1
import json
import sys
p = json.load(open(sys.argv[1], encoding="utf-8"))
layers = p.get("layer_status", {})
ok = p.get("contract") == "done" and p.get("projection") == "done" and p.get("audit") == "done"
ok = ok and all(value == "done" for value in layers.values())
raise SystemExit(0 if ok else 1)
PY
}

run_job() {
  local dag="$1" ordinal="$2"
  local filename name world log seed ccr_port persona
  # 关闭继承的锁 fd：flock 生命周期只应绑定编排器主进程，job 子树不得持有。
  exec 9>&-
  filename="${dag##*/}"
  name="${filename%.json}"
  world="$OUT_DIR/$name"
  log="$LOG_DIR/$name.log"
  seed=$((BASE_SEED + ordinal))
  ccr_port=$((CCR_BASE_PORT + ordinal))
  persona="$PERSONA"
  if ((${#PERSONA_MAP_ARR[@]} > 0)); then
    persona="${PERSONA_MAP_ARR[$filename]:-$PERSONA}"
  fi

  if world_complete "$world" && ((FORCE == 0)); then
    printf '[skip] %s 已完整\n' "$world"
    return 0
  fi

  # 已有该 world 的生成进程在跑（如上轮编排的孤儿 job、手动 resume）时跳过，
  # 避免同 .build 双写与 CCR 端口互撞。--output 可能是绝对或相对路径，按 /$name 后缀匹配。
  if pgrep -f "python3 -m data_world --dag .*--output [^ ]*/$name " >/dev/null 2>&1; then
    printf '[skip] %s 已有进程在跑\n' "$world"
    return 0
  fi

  local -a cmd=(python3 -m data_world --dag "$dag" --output "$world" --persona "$persona"
                --seed "$seed" --model "$MODEL"
                --max-turns "$MAX_TURNS" --timeout "$TIMEOUT")
  [[ -n "$CCR_BIN" ]] && cmd+=(--ccr-bin "$CCR_BIN")
  if ((FORCE)); then
    cmd+=(--force)
  elif [[ -s "$world/.build/progress.json" ]]; then
    cmd+=(--resume)
  elif [[ -d "$world" ]] && [[ -n "$(find "$world" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    printf '[fail] %s 非空但没有可恢复的 progress.json；使用 --force 或先清理\n' "$world" >&2
    return 1
  fi
  ((LOG_TRAJECTORIES)) && cmd+=(--log-trajectories --ccr-port "$ccr_port")

  if ((DRY_RUN)); then
    printf '[plan] '; printf '%q ' "${cmd[@]}"; printf '> %q 2>&1\n' "$log"
    return 0
  fi

  printf '[start] %s\n' "$name"
  if "${cmd[@]}" >"$log" 2>&1; then
    printf '[ok] %s -> %s\n' "$name" "$world"
  else
    local code=$?
    printf '[fail] %s (exit=%d, log=%s)\n' "$name" "$code" "$log" >&2
    return "$code"
  fi
}

pids=()
failed=0
ordinal=0

# 收缩 pids 到仍在跑的进程；已结束的收割退出码（失败计数）。
reap() {
  local alive=() p
  for p in "${pids[@]}"; do
    if kill -0 "$p" 2>/dev/null; then
      alive+=("$p")
    else
      wait "$p" || ((failed+=1))
    fi
  done
  pids=("${alive[@]+"${alive[@]}"}")
}

# 动态并发池：任一 job 结束立即补位（不做顺序等位，避免长尾堵门）。
for dag in "${DAGS[@]}"; do
  run_job "$dag" "$ordinal" &
  pids+=("$!")
  ((ordinal+=1))
  if ((${#pids[@]} >= WORKERS)); then
    wait -n
    reap
  fi
done
while ((${#pids[@]} > 0)); do
  wait -n
  reap
done

printf '\n完成: 总计=%d, 失败=%d, World=%s, 日志=%s\n' "$ordinal" "$failed" "$OUT_DIR" "$LOG_DIR"
((failed == 0))
