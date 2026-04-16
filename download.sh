#!/usr/bin/env bash
set -euo pipefail

# -----------------------------
# 预定义默认参数（可被命令行覆盖）
# -----------------------------
DEFAULT_EXCEL="results/pt_0415.xlsx"
# DEFAULT_OUT_DIR="downloads"
DEFAULT_OUT_DIR="F:/download"
DEFAULT_TASK_NAME=""             # 留空：单 xlsx 用文件名推导（去掉 _metadata）；多 xlsx 默认 merged
DEFAULT_BILIBILI_COOKIES_PATH="config/bilibili_cookies.txt"
DEFAULT_YOUTUBE_COOKIES_PATH="config/youtube_cookies.txt"
DEFAULT_LIMIT=""
DEFAULT_ARCHIVE=""              # 留空则使用 <任务目录>/downloaded.txt
DEFAULT_SLEEP_INTERVAL="1"
DEFAULT_MAX_SLEEP_INTERVAL="3"
DEFAULT_FORMAT=""               # 留空让 yt-dlp 自选
DEFAULT_PARALLEL="2"           # 并行下载数（同时运行的 yt-dlp 进程数）

usage() {
  cat <<'EOF'
用法:
  ./download.sh [excel1.xlsx excel2.xlsx ...] [--excel FILE]... [--excels FILE...]
               [--out DIR] [--task-name NAME]
               [--cookies-bilibili FILE] [--cookies-youtube FILE]
               [--limit N] [--archive FILE] [--sleep-interval S] [--max-sleep-interval S] [--format FMT]
               [-j|--parallel N]

说明:
  - 推荐直接使用 Python 脚本：python scripts/download/download_all.py --excel <a.xlsx> --excel <b.xlsx> ...
  - 本脚本是对 Python 脚本的薄封装，支持多个 xlsx 合并去重 URL。

示例:
  ./download.sh                      # 使用默认 results/yue_metadata.xlsx
  ./download.sh results/yue_metadata.xlsx
  ./download.sh a.xlsx b.xlsx c.xlsx
  ./download.sh results/yue_metadata.xlsx --out downloads
  ./download.sh results/yue_metadata.xlsx --task-name input_yue
  ./download.sh results/yue_metadata.xlsx --cookies-bilibili config/bilibili_cookies.txt
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
COOKIES_BILIBILI="$DEFAULT_BILIBILI_COOKIES_PATH"
COOKIES_YOUTUBE="$DEFAULT_YOUTUBE_COOKIES_PATH"
LIMIT="$DEFAULT_LIMIT"
ARCHIVE_PATH="$DEFAULT_ARCHIVE"
SLEEP_INTERVAL="$DEFAULT_SLEEP_INTERVAL"
MAX_SLEEP_INTERVAL="$DEFAULT_MAX_SLEEP_INTERVAL"
FORMAT="$DEFAULT_FORMAT"
PARALLEL="$DEFAULT_PARALLEL"

EXCELS=()

# 兼容：若一开始给了多个位置参数（直到遇到 -* 选项），视为 excel 列表
while [[ $# -gt 0 ]] && [[ "${1:-}" != --* ]] && [[ "${1:-}" != -* ]] && [[ "${1:-}" != "" ]]; do
  EXCELS+=("$1")
  shift
done

# 若没有给位置参数，则使用默认 excel
if [[ ${#EXCELS[@]} -eq 0 ]]; then
  EXCELS+=("$EXCEL_PATH")
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --excel)
      EXCELS+=("$2")
      shift 2
      ;;
    --excels)
      shift
      while [[ $# -gt 0 ]] && [[ "${1:-}" != --* ]] && [[ "${1:-}" != "" ]]; do
        EXCELS+=("$1")
        shift
      done
      ;;
    --out)
      OUT_DIR="$2"
      shift 2
      ;;
    --task-name)
      TASK_NAME="$2"
      shift 2
      ;;
    --cookies-bilibili)
      COOKIES_BILIBILI="$2"
      shift 2
      ;;
    --cookies-youtube)
      COOKIES_YOUTUBE="$2"
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
    -j|--parallel)
      PARALLEL="$2"
      shift 2
      ;;
    *)
      echo "未知参数: $1" >&2
      usage
      exit 1
      ;;
  esac
done

for x in "${EXCELS[@]}"; do
  if [[ ! -f "$x" ]]; then
    echo "Excel 不存在: $x" >&2
    exit 1
  fi
done

PY_ARGS=(--out "$OUT_DIR")
for x in "${EXCELS[@]}"; do
  PY_ARGS+=(--excel "$x")
done
if [[ -n "$TASK_NAME" ]]; then
  PY_ARGS+=(--task-name "$TASK_NAME")
fi
if [[ -n "$COOKIES_BILIBILI" ]]; then
  PY_ARGS+=(--cookies-bilibili "$COOKIES_BILIBILI")
fi
if [[ -n "$COOKIES_YOUTUBE" ]]; then
  PY_ARGS+=(--cookies-youtube "$COOKIES_YOUTUBE")
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
if [[ -n "$PARALLEL" ]]; then
  PY_ARGS+=(--parallel "$PARALLEL")
fi

python "scripts/download/download_all.py" "${PY_ARGS[@]}"

