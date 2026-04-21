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
DEFAULT_FORMAT=""               # 留空让 yt-dlp 自选
DEFAULT_PARALLEL="4"           # 并行下载数（同时运行的 yt-dlp 进程数）
DEFAULT_SLEEP_YOUTUBE="10"        # 留空使用 Python 默认 2.0
DEFAULT_SLEEP_BILIBILI=""       # 留空使用 Python 默认 1.0
DEFAULT_SLEEP_PODCAST=""        # 留空使用 Python 默认 0.5

usage() {
  cat <<'EOF'
用法:
  ./download.sh [excel1.xlsx excel2.xlsx ...] [--excel FILE]... [--excels FILE...]
               [--out DIR] [--task-name NAME]
               [--cookies-bilibili FILE] [--cookies-youtube FILE]
               [--limit N] [--archive FILE] [--format FMT]
               [-j|--parallel N]
               [--sleep-youtube S] [--sleep-bilibili S] [--sleep-podcast S]

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
FORMAT="$DEFAULT_FORMAT"
PARALLEL="$DEFAULT_PARALLEL"
SLEEP_YOUTUBE="$DEFAULT_SLEEP_YOUTUBE"
SLEEP_BILIBILI="$DEFAULT_SLEEP_BILIBILI"
SLEEP_PODCAST="$DEFAULT_SLEEP_PODCAST"

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
    --sleep-youtube)
      SLEEP_YOUTUBE="$2"
      shift 2
      ;;
    --sleep-bilibili)
      SLEEP_BILIBILI="$2"
      shift 2
      ;;
    --sleep-podcast)
      SLEEP_PODCAST="$2"
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
if [[ -n "$SLEEP_YOUTUBE" ]]; then
  PY_ARGS+=(--sleep-youtube "$SLEEP_YOUTUBE")
fi
if [[ -n "$SLEEP_BILIBILI" ]]; then
  PY_ARGS+=(--sleep-bilibili "$SLEEP_BILIBILI")
fi
if [[ -n "$SLEEP_PODCAST" ]]; then
  PY_ARGS+=(--sleep-podcast "$SLEEP_PODCAST")
fi
if [[ -n "$FORMAT" ]]; then
  PY_ARGS+=(--format "$FORMAT")
fi
if [[ -n "$PARALLEL" ]]; then
  PY_ARGS+=(--parallel "$PARALLEL")
fi

python "scripts/download/download_all.py" "${PY_ARGS[@]}"

