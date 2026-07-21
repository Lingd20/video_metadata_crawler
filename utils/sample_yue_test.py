#!/usr/bin/env python3
"""从现有下载目录中按平台/字幕分组抽样，生成 yue_test 子集目录及对应 metadata.jsonl。

需求概述（默认约定）：
  - 源目录形如：F:/download/yue_0415
  - 目标目录与源同级：F:/download/yue_test
  - 在目标目录下生成两套子集：50/ 与 100/（可自定义）
  - 对 subtitled 与 unsubtitled 两个分组分别处理
  - 每个平台（bilibili/youtube/podcast）在每个分组中各抽样 N 条
  - 若对应目录不存在则跳过
  - 从源目录的 metadata.jsonl 中选出对应条目，分别写到 subtitled/metadata.jsonl 与
    unsubtitled/metadata.jsonl
  - 若某条目的 platform 或 publish_date 为空，则不使用该条目，继续抽取其它候选

输出结构（在目标目录下，保留平台分目录）：
  yue_test/
    subtitled/
      bilibili/audios/
      youtube/audios/
      youtube/subs/
      podcast/audios/
      metadata.jsonl
    unsubtitled/
      bilibili/audios/
      youtube/audios/
      podcast/audios/
      metadata.jsonl

说明：
  - 抽样的“条目”以源目录 audios 目录下的音频文件为准（默认 .wav；也兼容其它扩展名）。
  - YouTube 的字幕会从源目录的 subtitled/youtube/subs/<id>*.vtt 复制到目标的 subtitled/youtube/subs/。
  - 复制方式默认 copy；可选 --link 使用硬链接（同盘速度快、占用小；跨盘会自动回退 copy）。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
from dataclasses import dataclass
from pathlib import Path


PLATFORMS: tuple[str, ...] = ("bilibili", "youtube", "podcast")
GROUPS: tuple[str, ...] = ("subtitled", "unsubtitled")


@dataclass(frozen=True)
class MetaIndex:
    """metadata.jsonl 的索引：按 (platform, id) 定位条目。"""

    by_key: dict[tuple[str, str], dict]

    @staticmethod
    def load(path: Path) -> "MetaIndex":
        mapping: dict[tuple[str, str], dict] = {}
        if not path.exists():
            raise SystemExit(f"metadata.jsonl 不存在: {path}")
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = (line or "").strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except json.JSONDecodeError:
                    continue
                vid = str(obj.get("id") or "").strip()
                mi = obj.get("metainfo") or {}
                platform = str(mi.get("platform") or obj.get("platform") or "").strip().lower()
                if not vid or not platform:
                    continue
                mapping[(platform, vid)] = obj
        return MetaIndex(by_key=mapping)

    def get_valid(self, platform: str, vid: str) -> dict | None:
        key = (platform.lower().strip(), str(vid).strip())
        obj = self.by_key.get(key)
        if not obj:
            return None
        mi = obj.get("metainfo") or {}
        p = (mi.get("platform") or obj.get("platform") or "")
        publish_date = mi.get("publish_date") or mi.get("publish_data")  # 兼容用户写法
        if not str(p or "").strip():
            return None
        if not str(publish_date or "").strip():
            return None
        return obj


def _iter_audio_files(audios_dir: Path) -> list[Path]:
    if not audios_dir.exists() or not audios_dir.is_dir():
        return []
    out: list[Path] = []
    for p in audios_dir.iterdir():
        if not p.is_file():
            continue
        # 排除一些非音频产物（尽量保守）
        if p.suffix.lower() in (".json", ".txt", ".log"):
            continue
        out.append(p)
    out.sort(key=lambda x: x.name.lower())
    return out


def _safe_link_or_copy(src: Path, dst: Path, *, link: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    if link:
        try:
            os.link(src, dst)
            return
        except OSError:
            # 跨盘/权限等失败则回退 copy
            pass
    shutil.copy2(src, dst)


def _copy_youtube_subs_if_any(src_root: Path, dst_root: Path, *, vid: str, link: bool) -> int:
    src_subs = src_root / "subtitled" / "youtube" / "subs"
    if not src_subs.exists():
        return 0
    dst_subs = dst_root / "subtitled" / "youtube" / "subs"
    copied = 0
    for p in src_subs.glob(f"{vid}*.vtt"):
        if not p.is_file():
            continue
        _safe_link_or_copy(p, dst_subs / p.name, link=link)
        copied += 1
    return copied


def _select_ids(
    *,
    rng: random.Random,
    candidates: list[str],
    platform: str,
    meta: MetaIndex,
    want_n: int,
) -> list[str]:
    """从候选 id 中抽样 want_n 个，且必须能在 metadata 里找到有效条目。"""
    if want_n <= 0:
        return []
    pool = list(dict.fromkeys([c.strip() for c in candidates if c and c.strip()]))  # 去重保序
    rng.shuffle(pool)
    picked: list[str] = []
    for vid in pool:
        if len(picked) >= want_n:
            break
        if meta.get_valid(platform, vid) is None:
            continue
        picked.append(vid)
    return picked


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for obj in rows:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def build_subset(
    *,
    src_root: Path,
    dst_root: Path,
    meta: MetaIndex,
    n: int,
    seed: int,
    link: bool,
    dry_run: bool,
) -> None:
    rng = random.Random(int(seed))

    # 记录每个分组写入的 metadata
    out_meta: dict[str, list[dict]] = {g: [] for g in GROUPS}

    for group in GROUPS:
        for platform in PLATFORMS:
            src_audios = src_root / group / platform / "audios"
            if not src_audios.exists():
                print(f"[skip] 不存在目录: {src_audios}")
                continue

            audio_files = _iter_audio_files(src_audios)
            if not audio_files:
                print(f"[skip] 无音频文件: {src_audios}")
                continue

            # 候选 id 来自文件名 stem
            cand_ids = [p.stem for p in audio_files]
            picked_ids = _select_ids(rng=rng, candidates=cand_ids, platform=platform, meta=meta, want_n=n)

            if len(picked_ids) < n:
                print(
                    f"[warn] {group}/{platform} 候选 {len(cand_ids)}，有效 {len(picked_ids)}，"
                    f"不足 {n}（已尽力过滤 platform/publish_date 空）。"
                )
            else:
                print(f"[ok] {group}/{platform} 选取 {len(picked_ids)}/{n}")

            # 拷贝文件 + 收集 metadata
            for vid in picked_ids:
                m = meta.get_valid(platform, vid)
                if not m:
                    # 理论上不会发生（已过滤）；防御性跳过
                    continue

                # 音频：尽力匹配原后缀（候选列表里可能混有不同扩展名）
                # 这里优先找任意与 stem 相同的文件（按名称排序稳定）
                src_matches = sorted([p for p in audio_files if p.stem == vid], key=lambda x: x.name.lower())
                if not src_matches:
                    continue
                src_audio = src_matches[0]
                dst_audio = dst_root / group / platform / "audios" / src_audio.name

                if not dry_run:
                    _safe_link_or_copy(src_audio, dst_audio, link=link)

                # YouTube subtitled：复制字幕
                if group == "subtitled" and platform == "youtube":
                    if not dry_run:
                        _copy_youtube_subs_if_any(src_root, dst_root, vid=vid, link=link)

                out_meta[group].append(m)

        # 每个 group 写一个 metadata.jsonl（在 group 根目录下）
        meta_out_path = dst_root / group / "metadata.jsonl"
        # 稳定排序：platform + id
        rows = sorted(
            out_meta[group],
            key=lambda o: (
                str((o.get("metainfo") or {}).get("platform") or o.get("platform") or "").lower(),
                str(o.get("id") or ""),
            ),
        )
        if not dry_run:
            _write_jsonl(meta_out_path, rows)
        print(f"[write] {meta_out_path} ({len(rows)} 条)")


def main() -> None:
    ap = argparse.ArgumentParser(description="从 yue_0415 抽样生成 yue_test（含 metadata.jsonl）")
    ap.add_argument("--src", default="F:/download/yue_0415", help="源目录（默认: F:/download/yue_0415）")
    ap.add_argument("--dst-name", default="yue_test", help="目标目录名（与 src 同级，默认: yue_test）")
    ap.add_argument("--n", type=int, default=50, help="每组每平台抽样条数（默认 50；例如 100）")
    ap.add_argument("--seed", type=int, default=20260420, help="随机种子（默认固定）")
    ap.add_argument("--link", action="store_true", help="尽量用硬链接代替复制（失败则自动回退 copy）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    args = ap.parse_args()

    src_root = Path(args.src).resolve()
    if not src_root.exists():
        raise SystemExit(f"源目录不存在: {src_root}")
    if not src_root.is_dir():
        raise SystemExit(f"源路径不是目录: {src_root}")

    dst_root_base = src_root.parent / str(args.dst_name)
    meta_path = src_root / "metadata.jsonl"
    meta = MetaIndex.load(meta_path)

    n = int(args.n)
    if n <= 0:
        raise SystemExit("--n 必须是正整数（例如 --n 50 或 --n 100）")

    dst_root = dst_root_base
    print(f"\n=== 生成子集: {dst_root} (每组每平台 {n} 条) ===")
    if not args.dry_run:
        dst_root.mkdir(parents=True, exist_ok=True)
    build_subset(
        src_root=src_root,
        dst_root=dst_root,
        meta=meta,
        n=n,
        seed=int(args.seed) + int(n) * 1000,
        link=bool(args.link),
        dry_run=bool(args.dry_run),
    )


if __name__ == "__main__":
    main()

