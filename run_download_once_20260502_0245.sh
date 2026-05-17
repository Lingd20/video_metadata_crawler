#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TARGET_LOCAL="2026-05-04 00:00:00"
CRAWLER_DIR="/d/workspace/huawei-asr/video_metadata_crawler"

target_ts="$(date -d "$TARGET_LOCAL" +%s)"
now_ts="$(date +%s)"

if (( now_ts > target_ts )); then
  echo "目标时间已过：$TARGET_LOCAL（当前：$(date '+%F %T')）。不执行。" >&2
  exit 2
fi

echo "将于本地时间 $TARGET_LOCAL 执行一次：$CRAWLER_DIR/download.sh"

while true; do
  now_ts="$(date +%s)"
  if (( now_ts >= target_ts )); then
    break
  fi
  sleep_for=$(( target_ts - now_ts ))
  (( sleep_for > 60 )) && sleep_for=60
  sleep "$sleep_for"
done

echo "开始执行：$(date '+%F %T')"
cd "$CRAWLER_DIR"
./download.sh
echo "完成：$(date '+%F %T')"

