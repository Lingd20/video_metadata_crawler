#!/usr/bin/env python3
"""YouTube 下载模块：按 subtitle_mode 下载音频（可选手动字幕）。

subtitle_mode:
  1) audio_only  — 不检查字幕，全部只下音频
  2) manual_only — 只下载有手动字幕的（音频+字幕）
  3) mixed       — 有手动字幕 → 音频+字幕；否则只下音频
"""

from __future__ import annotations

import re
from pathlib import Path

from yt_dlp_common import (
    YtDlpAudioOptions,
    YtDlpRuntimeOptions,
    append_subtitle_probe_cache,
    batch_probe_manual_subtitles,
    build_yt_dlp_cmd,
    filter_urls_by_archive,
    list_audio_files,
    load_download_archive,
    load_subtitle_probe_cache,
    normalize_subtitle_mode,
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


def _sub_extra_args(sub_langs: str = "") -> list[str]:
    langs = (sub_langs or "").strip() or "all"
    return [
        "--write-subs",
        "--no-write-auto-subs",
        "--sub-langs",
        langs,
        "--sub-format",
        "vtt",
        "--convert-subs",
        "vtt",
    ]


def _run_group(
    *,
    urls: list[str],
    parallel: int,
    out_dir: Path,
    archive_path: Path,
    cookies_path: str,
    audio: YtDlpAudioOptions,
    runtime: YtDlpRuntimeOptions,
    log_path: Path,
    record_base: dict,
    audio_ext: str,
    file_prefix: str,
    output_template: str,
    subtitle_output_template: str = "",
    extra_args: list[str] | None = None,
):
    if not urls:
        return
    if parallel > 1 and len(urls) > 1:
        run_yt_dlp_parallel(
            pending_urls=urls,
            parallel=parallel,
            out_dir=out_dir,
            archive_path=archive_path,
            cookies_path=cookies_path,
            audio=audio,
            runtime=runtime,
            log_path=log_path,
            record_base=record_base,
            audio_ext=audio_ext,
            file_prefix=file_prefix,
            output_template=output_template,
            subtitle_output_template=subtitle_output_template,
            extra_args=extra_args,
        )
        return

    pf = out_dir / f"{file_prefix}.txt"
    pf.write_text("\n".join(urls) + "\n", encoding="utf-8")
    cmd = build_yt_dlp_cmd(
        out_dir=out_dir,
        pending_file=pf,
        archive_path=archive_path,
        cookies_path=cookies_path,
        audio=audio,
        runtime=runtime,
        output_template=output_template,
        subtitle_output_template=subtitle_output_template,
        extra_args=extra_args,
    )
    run_yt_dlp_and_log(
        cmd=cmd,
        out_dir=out_dir,
        log_path=log_path,
        record_base=record_base,
        audio_ext=audio_ext,
    )


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
    subtitle_mode: str = "audio_only",
    sub_langs: str = "",
):
    mode = normalize_subtitle_mode(subtitle_mode)
    archive_keys = load_download_archive(archive_path)
    pending, skipped = filter_urls_by_archive(urls, archive_keys, url_to_archive_key)

    urls_file = out_dir / "youtube_urls.txt"
    urls_file.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    pending_file = out_dir / "youtube_urls_pending.txt"
    pending_file.write_text("\n".join(pending) + ("\n" if pending else ""), encoding="utf-8")

    print(
        f"[youtube] 断点预检: archive {len(archive_keys)} 条，本批 {len(urls)} 条，"
        f"跳过 {skipped} 条，待下载 {len(pending)} 条 | subtitle_mode={mode}"
    )
    if not pending:
        return

    audio_ext = (audio.audio_format or "wav").lower()
    record_base = {
        "platform": "youtube",
        "task_name": task_name,
        "out": str(out_dir),
        "archive": str(archive_path),
        "url_count": len(urls),
        "pending_count": len(pending),
        "skipped_by_archive": skipped,
        "subtitle_mode": mode,
    }

    flags: dict[str, bool] = {}
    before_files = list_audio_files(out_dir, audio_ext)

    if mode == "audio_only":
        _run_group(
            urls=pending,
            parallel=parallel,
            out_dir=out_dir,
            archive_path=archive_path,
            cookies_path=cookies_path,
            audio=audio,
            runtime=runtime,
            log_path=log_path,
            record_base={**record_base, "group": "audio_only"},
            audio_ext=audio_ext,
            file_prefix="youtube_pending",
            output_template="youtube/audio/%(id)s.%(ext)s",
        )
    else:
        probed_cache_path = out_dir / "subtitle_probed_urls.txt"
        manual_cache_path = out_dir / "subtitle_manual_urls.txt"
        cache = load_subtitle_probe_cache(probed_cache_path, manual_cache_path)
        flags = dict(cache)
        cache_hits = sum(1 for u in pending if u in cache)
        cache_manual = sum(1 for u in pending if cache.get(u))
        print(
            f"[youtube] 字幕探测缓存: 全量 {len(cache)} 条"
            f"（有字幕 {sum(1 for v in cache.values() if v)}），"
            f"本批命中 {cache_hits}/{len(pending)}（有字幕 {cache_manual}）"
            f" → {probed_cache_path.name} / {manual_cache_path.name}"
        )

        probe_batch_size = 50
        batch_count = (len(pending) + probe_batch_size - 1) // probe_batch_size
        print(f"[youtube] 小批交错处理: 每批最多 {probe_batch_size} 条，共 {batch_count} 批")

        for batch_index, start in enumerate(
            range(0, len(pending), probe_batch_size), start=1
        ):
            batch = pending[start : start + probe_batch_size]
            cached_urls = [u for u in batch if u in cache]
            need_probe = [u for u in batch if u not in cache]
            batch_flags: dict[str, bool] = {u: bool(cache[u]) for u in cached_urls}

            if need_probe:
                print(
                    f"[youtube] 第 {batch_index}/{batch_count} 批: "
                    f"缓存命中 {len(cached_urls)}，探测 {len(need_probe)} 条手动字幕…"
                )
                probed = batch_probe_manual_subtitles(
                    need_probe,
                    cookies_path,
                    batch_size=probe_batch_size,
                    parallel=parallel,
                    sub_langs=sub_langs,
                )
                added_probed, added_manual = append_subtitle_probe_cache(
                    probed_cache_path, manual_cache_path, probed
                )
                cache.update(probed)
                batch_flags.update(probed)
                if added_probed or added_manual:
                    print(
                        f"  [subtitle-probe] 缓存追加: 已探测 +{added_probed}, "
                        f"有字幕 +{added_manual}"
                    )
                failed_probe = len(need_probe) - len(probed)
                if failed_probe:
                    print(
                        f"  [subtitle-probe] 本批探测未成功 {failed_probe} 条"
                        f"（不写缓存，下次重试）"
                    )
            else:
                print(
                    f"[youtube] 第 {batch_index}/{batch_count} 批: "
                    f"全部命中缓存 {len(cached_urls)} 条，跳过探测"
                )

            flags.update(batch_flags)
            batch_subtitled = [u for u in batch if batch_flags.get(u)]
            batch_unsubtitled = [u for u in batch if not batch_flags.get(u)]
            print(
                f"[youtube] 第 {batch_index}/{batch_count} 批字幕结果: "
                f"有手动字幕 {len(batch_subtitled)} 条, "
                f"无/未知 {len(batch_unsubtitled)} 条"
            )

            if batch_subtitled:
                print(
                    f"[youtube] 第 {batch_index}/{batch_count} 批下载有字幕组: "
                    f"{len(batch_subtitled)} 条（音频+字幕）"
                )
                _run_group(
                    urls=batch_subtitled,
                    parallel=parallel,
                    out_dir=out_dir,
                    archive_path=archive_path,
                    cookies_path=cookies_path,
                    audio=audio,
                    runtime=runtime,
                    log_path=log_path,
                    record_base={
                        **record_base,
                        "group": "subtitled",
                        "batch": batch_index,
                    },
                    audio_ext=audio_ext,
                    file_prefix=f"youtube_sub_pending_{batch_index}",
                    output_template="subtitled/youtube/audios/%(id)s.%(ext)s",
                    subtitle_output_template="subtitled/youtube/subs/%(id)s.%(language)s.%(ext)s",
                    extra_args=_sub_extra_args(sub_langs),
                )
            if mode == "mixed" and batch_unsubtitled:
                print(
                    f"[youtube] 第 {batch_index}/{batch_count} 批下载无字幕组: "
                    f"{len(batch_unsubtitled)} 条（仅音频）"
                )
                _run_group(
                    urls=batch_unsubtitled,
                    parallel=parallel,
                    out_dir=out_dir,
                    archive_path=archive_path,
                    cookies_path=cookies_path,
                    audio=audio,
                    runtime=runtime,
                    log_path=log_path,
                    record_base={
                        **record_base,
                        "group": "unsubtitled",
                        "batch": batch_index,
                    },
                    audio_ext=audio_ext,
                    file_prefix=f"youtube_nosub_pending_{batch_index}",
                    output_template="unsubtitled/youtube/audios/%(id)s.%(ext)s",
                )

    if not metadata_path:
        return

    after_files = list_audio_files(out_dir, audio_ext)
    new_files = sorted(after_files - before_files)
    if not new_files:
        return

    meta_by_id: dict[str, dict] = {}
    for u, meta in (metadata_by_url or {}).items():
        vid = _extract_video_id(u)
        if vid:
            meta_by_id[vid] = meta

    flag_by_id: dict[str, bool] = {}
    for u, flag in flags.items():
        vid = _extract_video_id(u)
        if vid:
            flag_by_id[vid] = flag

    written = 0
    for fp in new_files:
        vid = fp.stem
        meta = meta_by_id.get(vid, {})
        metainfo = dict(meta.get("metainfo") or {})
        url = metainfo.get("url") or ""
        if not url:
            for u in urls:
                if _extract_video_id(u) == vid:
                    url = u
                    break
        has_subs = flag_by_id.get(vid)
        if has_subs is None and mode == "audio_only":
            has_subs = None
        write_metadata_entry(
            metadata_path,
            video_id=vid,
            platform="youtube",
            url=url,
            duration_s=meta.get("duration_s"),
            metainfo_extra=metainfo,
            has_manual_subs=has_subs,
        )
        written += 1

    print(f"  [youtube] metadata.jsonl 写入 {written} 条")


if __name__ == "__main__":
    raise SystemExit(
        "请使用 scripts/download/download_all.py 作为入口（它会按平台分发）。"
    )
