#!/usr/bin/env python3
"""YouTube 下载模块：直接按 URL 批量下载音频，并用 Excel 中已有的 metainfo
写入 `metadata.jsonl`。不再做字幕检测 / 分组 / yt-dlp -j 二次抓取。
"""

from __future__ import annotations

import re
from pathlib import Path

from yt_dlp_common import (
    YtDlpAudioOptions,
    YtDlpRuntimeOptions,
    build_yt_dlp_cmd,
    filter_urls_by_archive,
    list_audio_files,
    load_download_archive,
    run_yt_dlp_and_log,
    run_yt_dlp_parallel,
    write_metadata_entry,
)


def _extract_video_id(url: str) -> str | None:
    m = re.search(
        r"(?:[?&]v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})(?:[&/?#]|$)",
        url or "",
    )
    return m.group(1) if m else None


def url_to_archive_key(url: str) -> tuple[str, str] | None:
    u = (url or "").strip()
    if not u:
        return None
    vid = _extract_video_id(u)
    if vid:
        return ("youtube", vid)
    return None


def download_youtube(
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
    pending, skipped = filter_urls_by_archive(urls, archive_keys, url_to_archive_key)

    urls_file = out_dir / "youtube_urls.txt"
    urls_file.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    pending_file = out_dir / "youtube_urls_pending.txt"
    pending_file.write_text("\n".join(pending) + ("\n" if pending else ""), encoding="utf-8")

    print(
        f"[youtube] 断点预检: archive {len(archive_keys)} 条，本批 {len(urls)} 条，"
        f"跳过 {skipped} 条，待下载 {len(pending)} 条。"
    )
    if not pending:
        return

    audio_ext = (audio.audio_format or "wav").lower()
    output_tpl = "youtube/audio/%(id)s.%(ext)s"

    record_base = {
        "platform": "youtube",
        "task_name": task_name,
        "out": str(out_dir),
        "archive": str(archive_path),
        "url_count": len(urls),
        "pending_count": len(pending),
        "skipped_by_archive": skipped,
    }

    before_files = list_audio_files(out_dir, audio_ext)

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
            file_prefix="youtube_pending",
            output_template=output_tpl,
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
        )
        run_yt_dlp_and_log(
            cmd=cmd,
            out_dir=out_dir,
            log_path=log_path,
            record_base=record_base,
            audio_ext=audio_ext,
        )

    if not metadata_path:
        return

    after_files = list_audio_files(out_dir, audio_ext)
    new_files = sorted(after_files - before_files)
    if not new_files:
        return

    # 按 video id 回查表格元信息
    meta_by_id: dict[str, dict] = {}
    for u, meta in (metadata_by_url or {}).items():
        vid = _extract_video_id(u)
        if vid:
            meta_by_id[vid] = meta

    written = 0
    for fp in new_files:
        # 只写入命中 youtube audio 目录的文件
        try:
            fp.relative_to(out_dir / "youtube" / "audio")
        except ValueError:
            continue
        vid = fp.stem
        meta = meta_by_id.get(vid, {})
        metainfo = dict(meta.get("metainfo") or {})
        url = metainfo.get("url") or ""
        if not url:
            # 兜底：从原始 URL 列表里找 id
            for u in urls:
                if _extract_video_id(u) == vid:
                    url = u
                    break
        write_metadata_entry(
            metadata_path,
            video_id=vid,
            platform="youtube",
            url=url,
            duration_s=meta.get("duration_s"),
            metainfo_extra=metainfo,
        )
        written += 1

    print(f"  [youtube] metadata.jsonl 写入 {written} 条")


if __name__ == "__main__":
    raise SystemExit(
        "请使用 scripts/download/download_all.py 作为入口（它会按平台分发）。"
    )
