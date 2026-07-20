#!/usr/bin/env bash
# YouTube 手动字幕：搜索 + 即时下载（单阶段）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
export PYTHONPATH="$PROJECT_ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}"

log() {
    echo -e "$(date '+%Y-%m-%dT%H:%M:%S') $*"
}

config="$PROJECT_ROOT/config/config.yaml"
input_file=""
out_dir=""
task_name=""
refresh_pending=""

while [ $# -gt 0 ]; do
    case "$1" in
        --config)  config="$2";      shift 2 ;;
        --input)   input_file="$2"; shift 2 ;;
        --out)     out_dir="$2";     shift 2 ;;
        --task-name) task_name="$2"; shift 2 ;;
        --refresh-pending-urls) refresh_pending="1"; shift ;;
        -h|--help)
            cat <<'EOF'
用法: ./subtitled_download.sh [选项]

YouTube 手动字幕单阶段流水线：与 run.sh 相同 config/input，发现手动字幕后立即下载。

选项:
  --config FILE    配置文件（默认 config/config.yaml）
  --input FILE     输入文件（覆盖 config 中的 input_file）
  --out DIR        下载根目录（默认 downloads）
  --task-name NAME 任务子目录名（默认 input 文件名）
  --refresh-pending-urls  强制重新拉取视频 URL 列表
  -h, --help       显示帮助

示例:
  ./subtitled_download.sh --config config/config.yaml --input input/input_yue.txt
EOF
            exit 0
            ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

log "=========================================="
log "YouTube 手动字幕 · 搜索+下载"
log "=========================================="
log "配置文件: $config"

CMD="python $PROJECT_ROOT/scripts/subtitled_search_download/run.py --config $config"
[ -n "$input_file" ]  && CMD="$CMD --input $input_file"
[ -n "$out_dir" ]     && CMD="$CMD --out $out_dir"
[ -n "$task_name" ]   && CMD="$CMD --task-name $task_name"
[ -n "$refresh_pending" ] && CMD="$CMD --refresh-pending-urls"

$CMD

log "=========================================="
log "执行完毕"
log "=========================================="
