#!/usr/bin/env python3
"""修复 metadata.jsonl 中 B 站分P条目的元数据（尤其是 duration=0 的情况）。

背景：
  yt-dlp 对 B 站分P的下载 id 常形如 BVxxxx_p3。
  若用 https://www.bilibili.com/video/BVxxxx_p3 这种 URL 抓元数据会失败；
  正确形式应为 https://www.bilibili.com/video/BVxxxx/?p=3

用法：
  python scripts/download/repair_metadata_bilibili.py --metadata F:/download/pt_0415/metadata.jsonl --cookies-bilibili config/bilibili_cookies.txt

输出：
  默认生成同目录的 metadata.repaired.jsonl（不覆盖原文件）。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from yt_dlp_common import fetch_single_video_metadata, parse_yt_dlp_date


def _to_part_url(vid: str) -> str | None:
    m = re.match(r"^(BV[a-zA-Z0-9]+)_p(\d+)$", (vid or "").strip(), re.I)
    if not m:
        return None
    bvid = m.group(1)
    p = int(m.group(2))
    return f"https://www.bilibili.com/video/{bvid}/?p={p}"


def main():
    ap = argparse.ArgumentParser(description="回填 B 站分P条目的 duration/channel/publish_date 等字段")
    ap.add_argument("--metadata", required=True, help="metadata.jsonl 路径")
    ap.add_argument("--out", default="", help="输出路径（默认: metadata.repaired.jsonl）")
    ap.add_argument("--cookies-bilibili", default="", help="B 站 cookies.txt（可选，但建议提供）")
    ap.add_argument("--only-zero", action="store_true", help="只修复 duration_s=0 的条目（默认开启）")
    args = ap.parse_args()

    src = Path(args.metadata)
    if not src.exists():
        raise SystemExit(f"metadata 文件不存在: {src}")

    out = Path(args.out) if args.out else (src.parent / "metadata.repaired.jsonl")
    only_zero = True if args.only_zero is False else True  # 保持默认 True

    fixed = 0
    total = 0

    with src.open("r", encoding="utf-8", errors="ignore") as fin, out.open("w", encoding="utf-8") as fout:
        for line in fin:
            total += 1
            s = (line or "").strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                fout.write(line)
                continue

            platform = (obj.get("metainfo") or {}).get("platform") or obj.get("platform")
            vid = str(obj.get("id") or "")
            dur = float(obj.get("duration_s") or 0.0)

            if str(platform).lower() != "bilibili":
                fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                continue

            part_url = _to_part_url(vid)
            if not part_url:
                fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                continue

            if only_zero and dur > 0:
                fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                continue

            info = fetch_single_video_metadata(part_url, args.cookies_bilibili)
            if not info:
                fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                continue

            new_dur = info.get("duration") or 0
            channel = info.get("channel") or info.get("uploader") or None
            upload_date = parse_yt_dlp_date(info.get("upload_date"))

            obj["duration_s"] = float(new_dur or 0)
            mi = obj.get("metainfo") or {}
            mi["url"] = part_url
            if channel:
                mi["channel"] = channel
            if upload_date:
                mi["publish_date"] = upload_date
            obj["metainfo"] = mi

            fixed += 1
            if fixed % 50 == 0:
                print(f"[repair] 已修复 {fixed} 条…")

            fout.write(json.dumps(obj, ensure_ascii=False) + "\n")

    print(f"[repair] 完成：总行数 {total}，修复 {fixed}，输出 → {out}")


if __name__ == "__main__":
    main()

