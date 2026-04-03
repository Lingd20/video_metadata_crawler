#!/usr/bin/env bash
set -euo pipefail

# -----------------------------
# 预定义默认参数（可被命令行覆盖）
# -----------------------------
DEFAULT_EXCEL="results/yue_metadata.xlsx"
DEFAULT_OUT_DIR="downloads"
DEFAULT_TASK_NAME=""             # 留空则由 scripts/download.py 从 xlsx 文件名推导（去掉 _metadata）
DEFAULT_COOKIES_PATH="config/bilibili_cookies.txt"
DEFAULT_LIMIT=""
DEFAULT_ARCHIVE=""              # 留空则由 scripts/download.py 使用 <out>/downloaded.txt
DEFAULT_SLEEP_INTERVAL="1"
DEFAULT_MAX_SLEEP_INTERVAL="3"
DEFAULT_FORMAT=""               # 留空让 yt-dlp 自选

usage() {
  cat <<'EOF'
用法:
  ./download.sh [excel.xlsx] [--excel FILE] [--out DIR] [--task-name NAME] [--cookies FILE] [--limit N]
               [--archive FILE] [--sleep-interval S] [--max-sleep-interval S] [--format FMT]

说明:
  - 推荐直接使用 Python 脚本：python scripts/download.py --excel <excel.xlsx> ...
  - 本脚本是对 Python 脚本的薄封装。

示例:
  ./download.sh                      # 使用默认 results/yue_metadata.xlsx
  ./download.sh results/yue_metadata.xlsx
  ./download.sh results/yue_metadata.xlsx --out downloads
  ./download.sh results/yue_metadata.xlsx --task-name input_yue
  ./download.sh results/yue_metadata.xlsx --cookies config/bilibili_cookies.txt
  ./download.sh results/yue_metadata.xlsx --limit 10
EOF
}

if [[ "${1:-}" == "-h" ]] || [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

EXCEL_PATH="$DEFAULT_EXCEL"
OUT_DIR="$DEFAULT_OUT_DIR"
TASK_NAME="$DEFAULT_TASK_NAME"
COOKIES_PATH="$DEFAULT_COOKIES_PATH"
LIMIT="$DEFAULT_LIMIT"
ARCHIVE_PATH="$DEFAULT_ARCHIVE"
SLEEP_INTERVAL="$DEFAULT_SLEEP_INTERVAL"
MAX_SLEEP_INTERVAL="$DEFAULT_MAX_SLEEP_INTERVAL"
FORMAT="$DEFAULT_FORMAT"

# 兼容：第一个位置参数为 excel.xlsx（可省略）
if [[ $# -ge 1 ]] && [[ "${1:-}" != --* ]] && [[ "${1:-}" != "" ]]; then
  EXCEL_PATH="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --excel)
      EXCEL_PATH="$2"
      shift 2
      ;;
    --out)
      OUT_DIR="$2"
      shift 2
      ;;
    --task-name)
      TASK_NAME="$2"
      shift 2
      ;;
    --cookies)
      COOKIES_PATH="$2"
      shift 2
      ;;
    --limit)
      LIMIT="$2"
      shift 2
      ;;
    --archive)
      ARCHIVE_PATH="$2"
      shift 2
      ;;
    --sleep-interval)
      SLEEP_INTERVAL="$2"
      shift 2
      ;;
    --max-sleep-interval)
      MAX_SLEEP_INTERVAL="$2"
      shift 2
      ;;
    --format)
      FORMAT="$2"
      shift 2
      ;;
    *)
      echo "未知参数: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ ! -f "$EXCEL_PATH" ]]; then
  echo "Excel 不存在: $EXCEL_PATH" >&2
  exit 1
fi

PY_ARGS=(--excel "$EXCEL_PATH" --out "$OUT_DIR")
if [[ -n "$TASK_NAME" ]]; then
  PY_ARGS+=(--task-name "$TASK_NAME")
fi
if [[ -n "$COOKIES_PATH" ]]; then
  PY_ARGS+=(--cookies "$COOKIES_PATH")
fi
if [[ -n "$LIMIT" ]]; then
  PY_ARGS+=(--limit "$LIMIT")
fi
if [[ -n "$ARCHIVE_PATH" ]]; then
  PY_ARGS+=(--archive "$ARCHIVE_PATH")
fi
if [[ -n "$SLEEP_INTERVAL" ]]; then
  PY_ARGS+=(--sleep-interval "$SLEEP_INTERVAL")
fi
if [[ -n "$MAX_SLEEP_INTERVAL" ]]; then
  PY_ARGS+=(--max-sleep-interval "$MAX_SLEEP_INTERVAL")
fi
if [[ -n "$FORMAT" ]]; then
  PY_ARGS+=(--format "$FORMAT")
fi

python "scripts/download.py" "${PY_ARGS[@]}"

