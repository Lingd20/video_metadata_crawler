#!/usr/bin/env python3
from __future__ import annotations

import re
import time
from pathlib import Path

from yt_dlp_common import (
    YtDlpAudioOptions,
    YtDlpRuntimeOptions,
    append_failed_urls,
    build_yt_dlp_cmd,
    fetch_single_video_metadata,
    filter_urls_by_archive,
    list_audio_files,
    load_failed_urls,
    load_download_archive,
    parse_yt_dlp_date,
    run_yt_dlp_and_log,
    run_yt_dlp_parallel,
    write_metadata_entry,
)
from concurrent.futures import ThreadPoolExecutor, as_completed


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
    """Return (url, base_bvid) from downloaded id.

    yt-dlp 对 B 站分P常用 id 形如 BVxxxx_p3；需要转成 ?p=3 才能正确抓到该分P的 duration 等信息。
    """
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
    output_tpl = "unsubtitled/bilibili/audios/%(id)s.%(ext)s"

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

    # Write metadata.jsonl for newly downloaded files
    if metadata_path:
        after_files = list_audio_files(out_dir, audio_ext)
        new_files = sorted(after_files - before_files)
        if new_files:
            print(f"[bilibili] 获取 {len(new_files)} 条新音频的元数据…")
            # 并发抓取元数据：避免单条 B 站请求慢导致整体卡住
            def _pick_best_url_for_vid(vid: str) -> str:
                base_url, base_bvid = _bilibili_part_url_from_id(vid)
                m_part = re.match(r"^(BV[a-zA-Z0-9]+)_p(\d+)$", vid, re.I)
                want_p = int(m_part.group(2)) if m_part else None
                for u in pending:
                    ubvid = _extract_bvid(u)
                    if not ubvid or ubvid.lower() != base_bvid.lower():
                        continue
                    if want_p is None:
                        return u
                    mp = re.search(r"[?&]p=(\d+)", u)
                    if mp and int(mp.group(1)) == want_p:
                        return u
                return base_url

            def _fetch_one(vid: str, url: str):
                info = fetch_single_video_metadata(url, cookies_path, timeout_sec=30)
                return vid, url, info

            vid_urls: list[tuple[str, str]] = []
            for fp in new_files:
                vid = fp.stem
                vid_urls.append((vid, _pick_best_url_for_vid(vid)))

            max_workers = min(8, max(1, len(vid_urls)))
            done = 0
            ok = 0
            fail = 0
            t0_meta = time.time()
            results: list[tuple[str, str, dict | None]] = []
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futs = [pool.submit(_fetch_one, vid, url) for vid, url in vid_urls]
                for fut in as_completed(futs):
                    vid, url, info = fut.result()
                    results.append((vid, url, info))
                    done += 1
                    if info:
                        ok += 1
                    else:
                        fail += 1
                        # 限频输出失败条目，避免刷屏
                        if fail <= 5 or fail % 20 == 0:
                            print(f"  [bilibili] metadata 失败: {vid}（将写空字段）")
                    if done % 10 == 0 or done == len(futs):
                        elapsed = int(time.time() - t0_meta)
                        print(
                            f"  [bilibili] metadata 进度: {done}/{len(futs)} "
                            f"(ok={ok}, fail={fail}, {elapsed}s)"
                        )

            # 写入时保持按文件名排序（更稳定）
            results.sort(key=lambda x: x[0])
            for vid, url, info in results:
                if info:
                    upload_date = parse_yt_dlp_date(info.get("upload_date"))
                    cats = info.get("categories") or []
                    category = ", ".join(cats) if isinstance(cats, list) else str(cats or "")
                    if not category:
                        category = info.get("category") or None
                    channel = info.get("channel") or info.get("uploader") or None
                    duration = info.get("duration") or 0
                else:
                    upload_date = None
                    category = None
                    channel = None
                    duration = 0

                write_metadata_entry(
                    metadata_path=metadata_path,
                    video_id=vid,
                    duration_s=float(duration),
                    platform="bilibili",
                    url=url,
                    channel=channel,
                    category=category or None,
                    publish_date=upload_date,
                )

            print(f"  [bilibili] metadata.jsonl 写入 {len(new_files)} 条")


if __name__ == "__main__":
    raise SystemExit(
        "请使用 scripts/download/download_all.py 作为入口（它会按平台分发）。"
    )
