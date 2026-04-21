#!/usr/bin/env python3
"""Bilibili 下载模块：直接下载音频，并从 Excel 表格写入 metadata.jsonl。

不再使用 yt-dlp -j 单条抓取 duration/channel/category 等字段——这些都已在
url_crawl 阶段写入 Excel，本脚本读表写盘即可。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from yt_dlp_common import (
    YtDlpAudioOptions,
    YtDlpRuntimeOptions,
    append_failed_urls,
    build_yt_dlp_cmd,
    filter_urls_by_archive,
    list_audio_files,
    load_failed_urls,
    load_download_archive,
    run_yt_dlp_and_log,
    run_yt_dlp_parallel,
    write_metadata_entry,
)


def url_to_archive_key(url: str) -> tuple[str, str] | None:
    u = (url or "").strip()
    if not u:
        return None
    if "bilibili.com" in u or "b23.tv" in u:
        m = re.search(r"(BV[a-zA-Z0-9]+)", u, re.I)
        if m:
            return ("bilibili", m.group(1))
    return None


def _expand_multipart_prefixes(archive: set[tuple[str, str]]) -> set[tuple[str, str]]:
    extra: set[tuple[str, str]] = set()
    for ext, vid in archive:
        if ext == "bilibili" and "_" in vid and vid.startswith("BV"):
            extra.add((ext, vid.split("_", 1)[0]))
    return extra


def _extract_bvid(url: str) -> str | None:
    m = re.search(r"(BV[a-zA-Z0-9]+)", url or "", re.I)
    return m.group(1) if m else None


def _bilibili_part_url_from_id(vid: str) -> tuple[str, str]:
    """从下载后的 id 还原 (url, base_bvid)。id 形如 BVxxxx 或 BVxxxx_p3。"""
    v = (vid or "").strip()
    m = re.match(r"^(BV[a-zA-Z0-9]+)_p(\d+)$", v, re.I)
    if m:
        bvid = m.group(1)
        p = int(m.group(2))
        return f"https://www.bilibili.com/video/{bvid}/?p={p}", bvid
    bvid = _extract_bvid(v) or v
    return f"https://www.bilibili.com/video/{bvid}", bvid


def download_bilibili(
    *,
    urls: list[str],
    out_dir: Path,
    archive_path: Path,
    cookies_path: str,
    audio: YtDlpAudioOptions,
    runtime: YtDlpRuntimeOptions,
    log_path: Path,
    task_name: str,
    parallel: int = 1,
    metadata_path: Path | None = None,
    metadata_by_url: dict[str, dict] | None = None,
):
    archive_keys = load_download_archive(archive_path)
    archive_keys = archive_keys | _expand_multipart_prefixes(archive_keys)
    pending, skipped = filter_urls_by_archive(urls, archive_keys, url_to_archive_key)

    failed_path = out_dir / "bilibili_failed_urls.txt"
    failed_urls = load_failed_urls(failed_path)
    if failed_urls:
        before = len(pending)
        pending = [u for u in pending if (u or "").strip() not in failed_urls]
        skipped_failed = before - len(pending)
    else:
        skipped_failed = 0

    urls_file = out_dir / "bilibili_urls.txt"
    urls_file.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    pending_file = out_dir / "bilibili_urls_pending.txt"
    pending_file.write_text("\n".join(pending) + ("\n" if pending else ""), encoding="utf-8")

    print(
        f"[bilibili] 断点预检: archive {len(archive_keys)} 条，本批 {len(urls)} 条，"
        f"跳过 {skipped} 条，失败跳过 {skipped_failed} 条，待下载 {len(pending)} 条。"
    )
    if not pending:
        return

    record_base = {
        "platform": "bilibili",
        "task_name": task_name,
        "out": str(out_dir),
        "archive": str(archive_path),
        "url_count": len(urls),
        "pending_count": len(pending),
        "skipped_by_archive": skipped,
    }
    audio_ext = (audio.audio_format or "wav").lower()
    output_tpl = "bilibili/audio/%(id)s.%(ext)s"

    before_files = list_audio_files(out_dir, audio_ext)
    bili_fmt = "bestaudio"

    if parallel > 1 and len(pending) > 1:
        run_yt_dlp_parallel(
            pending_urls=pending,
            parallel=parallel,
            out_dir=out_dir,
            archive_path=archive_path,
            cookies_path=cookies_path,
            audio=audio,
            runtime=runtime,
            log_path=log_path,
            record_base=record_base,
            audio_ext=audio_ext,
            file_prefix="bilibili_pending",
            output_template=output_tpl,
            format_override=bili_fmt,
        )
    else:
        cmd = build_yt_dlp_cmd(
            out_dir=out_dir,
            pending_file=pending_file,
            archive_path=archive_path,
            cookies_path=cookies_path,
            audio=audio,
            runtime=runtime,
            output_template=output_tpl,
            format_override=bili_fmt,
        )
        run_yt_dlp_and_log(
            cmd=cmd,
            out_dir=out_dir,
            log_path=log_path,
            record_base=record_base,
            audio_ext=audio_ext,
        )

    after_files = list_audio_files(out_dir, audio_ext)
    new_files = sorted(after_files - before_files)
    # 统计本轮仍未进入 archive 的 URL，写入 failed 列表，避免下次运行反复刷同样错误
    try:
        after_archive = load_download_archive(archive_path)
        still_failed: list[str] = []
        for u in pending:
            key = url_to_archive_key(u)
            if key is None:
                # 无法提取 key：不自动判定失败，避免误伤
                continue
            if (key[0].lower(), key[1]) not in after_archive:
                still_failed.append(u)
        wrote = append_failed_urls(failed_path, still_failed)
        if wrote:
            print(f"[bilibili] 本轮记录失败 URL: {wrote} 条 → {failed_path}")
    except Exception:
        pass

    if not metadata_path:
        return

    if not new_files:
        return

    # 构造 bvid -> [(url, part_idx, meta)] 反查索引
    bvid_index: dict[str, list[tuple[str, int | None, dict]]] = {}
    for u, meta in (metadata_by_url or {}).items():
        bvid = _extract_bvid(u)
        if not bvid:
            continue
        mp = re.search(r"[?&]p=(\d+)", u)
        part = int(mp.group(1)) if mp else None
        bvid_index.setdefault(bvid.lower(), []).append((u, part, meta))

    written = 0
    for fp in new_files:
        try:
            fp.relative_to(out_dir / "bilibili" / "audio")
        except ValueError:
            continue
        vid = fp.stem
        base_url, base_bvid = _bilibili_part_url_from_id(vid)
        m_part = re.match(r"^(BV[a-zA-Z0-9]+)_p(\d+)$", vid, re.I)
        want_p = int(m_part.group(2)) if m_part else None

        matched_url = base_url
        matched_meta: dict = {}
        for u, part, meta in bvid_index.get(base_bvid.lower(), []):
            if want_p is None:
                matched_url = u
                matched_meta = meta
                break
            if part == want_p:
                matched_url = u
                matched_meta = meta
                break
        else:
            # 分P 情况下，若表格里只收录了主页 URL（无 ?p=），也接受作为兜底
            for u, part, meta in bvid_index.get(base_bvid.lower(), []):
                if part is None:
                    matched_url = u
                    matched_meta = meta
                    break

        metainfo = dict(matched_meta.get("metainfo") or {})
        write_metadata_entry(
            metadata_path,
            video_id=vid,
            platform="bilibili",
            url=matched_url,
            duration_s=matched_meta.get("duration_s"),
            metainfo_extra=metainfo,
        )
        written += 1

    print(f"  [bilibili] metadata.jsonl 写入 {written} 条")


if __name__ == "__main__":
    raise SystemExit(
        "请使用 scripts/download/download_all.py 作为入口（它会按平台分发）。"
    )
