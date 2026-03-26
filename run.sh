#!/bin/bash
# Video Metadata Crawler 启动脚本
set -e
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
export PYTHONPATH="$PROJECT_ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}"

log() {
    echo -e "$(date '+%Y-%m-%dT%H:%M:%S') $*"
}

config="$PROJECT_ROOT/config/config.yaml"
input_file=""
output_file=""

while [ $# -gt 0 ]; do
    case "$1" in
        --config)   config="$2";      shift 2 ;;
        --input)    input_file="$2";   shift 2 ;;
        --output)   output_file="$2";  shift 2 ;;
        -h|--help)
            echo "用法: $0 [选项]"
            echo ""
            echo "选项:"
            echo "  --config FILE    配置文件路径 (默认: config/config.yaml)"
            echo "  --input FILE     输入文件路径 (覆盖配置文件)"
            echo "  --output FILE    输出文件路径 (覆盖配置文件)"
            echo "  -h, --help       显示帮助"
            exit 0
            ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

log "=========================================="
log "Video Metadata Crawler"
log "=========================================="
log "配置文件: $config"

CMD="python $PROJECT_ROOT/scripts/crawl_all.py --config $config"
[ -n "$input_file" ]  && CMD="$CMD --input $input_file"
[ -n "$output_file" ] && CMD="$CMD --output $output_file"

$CMD

log "=========================================="
log "执行完毕"
log "=========================================="
