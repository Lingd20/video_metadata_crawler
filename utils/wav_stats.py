#!/usr/bin/env python3
"""统计指定目录下所有 WAV 文件的数量和总时长。

用法:
  python utils/wav_stats.py <目录路径>
  python utils/wav_stats.py <目录路径> --by-dir    # 按子目录分组统计
  python utils/wav_stats.py <目录路径> -j 16       # 指定并发线程数
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _parse_wav_duration(path: str) -> float:
    """从 WAV 文件头解析时长（秒）。只读必要字节，不依赖 wave 模块。"""
    try:
        with open(path, "rb") as f:
            buf = f.read(12)
            if len(buf) < 12 or buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
                return 0.0
            # 扫描 fmt 和 data chunk
            sample_rate = 0
            block_align = 0
            data_size = 0
            while True:
                chunk_hdr = f.read(8)
                if len(chunk_hdr) < 8:
                    break
                chunk_id = chunk_hdr[:4]
                chunk_sz = struct.unpack_from("<I", chunk_hdr, 4)[0]
                if chunk_id == b"fmt ":
                    fmt = f.read(min(chunk_sz, 40))
                    if len(fmt) >= 16:
                        sample_rate = struct.unpack_from("<I", fmt, 4)[0]
                        block_align = struct.unpack_from("<H", fmt, 12)[0]
                    skip = chunk_sz - len(fmt)
                    if skip > 0:
                        f.seek(skip, 1)
                elif chunk_id == b"data":
                    data_size = chunk_sz
                    break
                else:
                    f.seek(chunk_sz, 1)
            if sample_rate and block_align:
                return data_size / (sample_rate * block_align)
    except Exception:
        pass
    return 0.0


def _scan_wav_files(root: str) -> list[str]:
    """os.scandir 递归收集所有 .wav 文件路径。"""
    result: list[str] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.name.lower().endswith(".wav") and entry.is_file(follow_symlinks=False):
                        result.append(entry.path)
        except PermissionError:
            pass
    return result


def format_hms(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


def main():
    parser = argparse.ArgumentParser(description="统计目录下所有 WAV 文件数量和总时长")
    parser.add_argument("directory", help="要扫描的目录路径")
    parser.add_argument("--by-dir", action="store_true", help="按直接父目录分组统计")
    parser.add_argument("-j", "--jobs", type=int, default=32, help="并发线程数（默认 32）")
    args = parser.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        raise SystemExit(f"目录不存在: {root}")

    print(f"正在扫描: {root}", file=sys.stderr)
    wav_files = _scan_wav_files(str(root))
    if not wav_files:
        print(f"未在 {root} 下找到任何 .wav 文件。")
        return

    print(f"找到 {len(wav_files)} 个 WAV 文件，正在解析时长 (×{args.jobs})…", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        durations = list(pool.map(_parse_wav_duration, wav_files))

    if args.by_dir:
        groups: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
        for path, dur in zip(wav_files, durations):
            rel = Path(path).relative_to(root)
            group = str(rel.parent) if rel.parent != Path(".") else "(根目录)"
            cnt, sec = groups[group]
            groups[group] = (cnt + 1, sec + dur)

        total_count = 0
        total_sec = 0.0
        print(f"\n{'子目录':<40} {'文件数':>8} {'时长':>12}")
        print("-" * 62)
        for name in sorted(groups):
            cnt, sec = groups[name]
            total_count += cnt
            total_sec += sec
            print(f"{name:<40} {cnt:>8} {format_hms(sec):>12}")
        print("-" * 62)
        print(f"{'合计':<40} {total_count:>8} {format_hms(total_sec):>12}")
    else:
        total_sec = sum(durations)
        print(f"\n文件数: {len(wav_files)}")
        print(f"总时长: {format_hms(total_sec)}  ({total_sec:.1f} 秒)")


if __name__ == "__main__":
    main()
