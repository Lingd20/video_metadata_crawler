#!/usr/bin/env python3
"""从一个任务目录抽样生成 pt_test 子集目录。

需求：
- src 下的 subtitled/ **全部复制**到 dst
- src 下的 unsubtitled/**/audios 随机抽 N 条音频复制到 dst（默认 100）
- 生成两份 metadata.jsonl：
  - dst/subtitled/metadata.jsonl：仅包含 subtitled 音频条目
  - dst/unsubtitled/metadata.jsonl：仅包含抽样的 unsubtitled 音频条目
- 且要求每条 metainfo.channel / metainfo.publish_date 都非空
  - 若缺失：尝试用 yt-dlp 按 metainfo.url 回填（YouTube/B站均可）
  - 仍缺失：该音频不会进入抽样集合（会自动补抽，直到凑够 N 或耗尽候选）

用法（PowerShell）：
  python utils/make_pt_test_subset.py --src "F:/download/pt_0415" --dst "F:/download/pt_test" --unsub-count 100 --seed 42
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from pathlib import Path

# 说明：根据需求，缺失字段不做 yt-dlp 回填，直接跳过并补抽。


def _copy_tree(src: Path, dst: Path):
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _copy_file(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _iter_audio_files(root: Path, ext: str) -> list[Path]:
    e = ext.lower().lstrip(".")
    if not root.exists():
        return []
    return [p for p in root.rglob(f"*.{e}") if p.is_file()]


def _load_metadata_map(path: Path) -> dict[str, dict]:
    m: dict[str, dict] = {}
    if not path.exists():
        return m
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            s = (line or "").strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            vid = obj.get("id")
            if vid:
                m[str(vid)] = obj
    return m


def _has_required_fields(rec: dict) -> bool:
    mi = rec.get("metainfo") or {}
    ch = (mi.get("channel") or "").strip() if isinstance(mi.get("channel"), str) else mi.get("channel")
    pd = (mi.get("publish_date") or "").strip() if isinstance(mi.get("publish_date"), str) else mi.get("publish_date")
    return bool(ch) and bool(pd)


def main():
    ap = argparse.ArgumentParser(description="从 pt_0415 抽样生成 pt_test 子集")
    ap.add_argument("--src", required=True, help="源任务目录，如 F:/download/pt_0415")
    ap.add_argument("--dst", required=True, help="目标目录，如 F:/download/pt_test")
    ap.add_argument("--unsub-count", type=int, default=100, help="unsubtitled 随机抽样数量（默认 100）")
    ap.add_argument("--seed", type=int, default=0, help="随机种子（默认 0 表示不固定）")
    ap.add_argument("--audio-ext", default="wav", help="音频扩展名（默认 wav）")
    args = ap.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)
    audio_ext = str(args.audio_ext or "wav").lower().lstrip(".")

    if not src.exists():
        raise SystemExit(f"src 不存在: {src}")

    # 1) 复制 subtitled 全量目录
    _copy_tree(src / "subtitled", dst / "subtitled")

    # 2) 收集候选音频
    unsub_audio_files = _iter_audio_files(src / "unsubtitled", audio_ext)
    if not unsub_audio_files:
        raise SystemExit(f"未找到 unsubtitled 音频: {src/'unsubtitled'} (**/*.{audio_ext})")

    # 3) 加载 metadata
    src_meta = src / "metadata.jsonl"
    meta_map = _load_metadata_map(src_meta)
    if not meta_map:
        raise SystemExit(f"未加载到任何 metadata 记录: {src_meta}")

    # 4) 确定 subtitled 的已选 id（从 subtitled/audios 下的实际文件来）
    subtitled_audio_files = _iter_audio_files(src / "subtitled", audio_ext)
    subtitled_ids: set[str] = {p.stem for p in subtitled_audio_files}
    # subtitled 要全量复制，因此其 metadata 必须完整；否则无法靠补抽修复
    missing_sub = [vid for vid in sorted(subtitled_ids) if not _has_required_fields(meta_map.get(vid, {}))]
    if missing_sub:
        raise SystemExit(
            f"subtitled 中有 {len(missing_sub)} 条 metadata 缺失 metainfo.channel/publish_date，"
            f"无法满足要求。示例: {missing_sub[:5]}"
        )

    # 5) 随机抽取 unsubtitled（需满足 metainfo.channel/publish_date 非空；缺失则尝试回填）
    rng = random.Random(args.seed) if args.seed else random.Random()
    rng.shuffle(unsub_audio_files)

    selected_unsub_files: list[Path] = []
    unsubtitled_ids: set[str] = set()
    needed = max(0, int(args.unsub_count))
    for fp in unsub_audio_files:
        if len(selected_unsub_files) >= needed:
            break
        vid = fp.stem
        rec = meta_map.get(vid)
        if not rec:
            continue
        if not _has_required_fields(rec):
            continue
        selected_unsub_files.append(fp)
        unsubtitled_ids.add(vid)

    if len(selected_unsub_files) < needed:
        raise SystemExit(f"unsubtitled 只选到 {len(selected_unsub_files)}/{needed} 条（满足 channel/publish_date 的候选不足）")

    # 6) 复制选中的 unsubtitled 音频（保持相对路径）
    for fp in selected_unsub_files:
        rel = fp.relative_to(src)
        _copy_file(fp, dst / rel)

    # 7) 生成两份 metadata.jsonl（各自“包含且仅包含”对应音频，且字段非空）
    out_meta_sub = dst / "subtitled" / "metadata.jsonl"
    out_meta_unsub = dst / "unsubtitled" / "metadata.jsonl"

    written_sub = 0
    out_meta_sub.parent.mkdir(parents=True, exist_ok=True)
    with out_meta_sub.open("w", encoding="utf-8") as f:
        for vid in sorted(subtitled_ids):
            rec = meta_map.get(vid)
            if not rec:
                raise SystemExit(f"subtitled 音频 {vid} 缺少 metadata 记录，已中止。")
            if not _has_required_fields(rec):
                raise SystemExit(f"subtitled 音频 {vid} 的 metadata 缺失 channel/publish_date，已中止。")
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written_sub += 1

    written_unsub = 0
    out_meta_unsub.parent.mkdir(parents=True, exist_ok=True)
    with out_meta_unsub.open("w", encoding="utf-8") as f:
        for vid in sorted(unsubtitled_ids):
            rec = meta_map.get(vid)
            if not rec:
                raise SystemExit(f"unsubtitled 音频 {vid} 缺少 metadata 记录，已中止。")
            if not _has_required_fields(rec):
                raise SystemExit(f"unsubtitled 音频 {vid} 的 metadata 缺失 channel/publish_date，已中止。")
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written_unsub += 1

    print(f"[pt_test] subtitled 音频: {len(subtitled_audio_files)}")
    print(f"[pt_test] unsubtitled 抽样音频: {len(selected_unsub_files)}")
    print(f"[pt_test] 写入 subtitled/metadata.jsonl: {written_sub} 条 → {out_meta_sub}")
    print(f"[pt_test] 写入 unsubtitled/metadata.jsonl: {written_unsub} 条 → {out_meta_unsub}")


if __name__ == "__main__":
    main()

