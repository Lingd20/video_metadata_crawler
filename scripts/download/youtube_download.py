#!/usr/bin/env python3
"""YouTube 下载模块：检测手动字幕 → 分组下载 → 写 metadata.jsonl。

流程：
  1) 批量获取待下载视频的元数据（yt-dlp -j，流式）
  2) 根据是否存在手动字幕，分为 subtitled / unsubtitled 两组
  3) 分组并行下载，subtitled 组同时下载音频和字幕
  4) 对缺少发布日期+类别的视频，用 yt-dlp -j 单独补全
  5) 写入 metadata.jsonl
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import tempfile
from contextlib import suppress
from pathlib import Path

from yt_dlp_common import (
    YtDlpAudioOptions,
    YtDlpRuntimeOptions,
    build_yt_dlp_cmd,
    fetch_single_video_metadata,
    filter_urls_by_archive,
    load_download_archive,
    parse_yt_dlp_date,
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


def _load_metadata_cache(cache_path: Path) -> dict[str, dict]:
    """Load cached yt-dlp JSON lines into {video_id: info_dict}."""
    if not cache_path.exists():
        return {}
    mapping: dict[str, dict] = {}
    for line in cache_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = (line or "").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = obj.get("id")
        if vid:
            mapping[str(vid)] = obj
    return mapping


def _append_metadata_cache(cache_path: Path, info: dict):
    """Append one info_dict to cache as JSONL (best-effort)."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with cache_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(info, ensure_ascii=False) + "\n")
    except Exception:
        # 缓存是加速/断点续跑用，失败不影响主流程
        return


def _iter_batches(items: list[str], batch_size: int):
    bs = max(1, int(batch_size or 1))
    for i in range(0, len(items), bs):
        yield (i // bs), items[i : i + bs]


# ---------------------------------------------------------------------------
# Phase 1: batch metadata fetch (streaming) for subtitle detection
# ---------------------------------------------------------------------------

def _batch_fetch_metadata(
    urls: list[str],
    cookies_path: str,
    *,
    show_progress: bool = True,
    per_item_log: bool = False,
    cache_path: Path | None = None,
) -> dict[str, dict]:
    """Fetch metadata for multiple YouTube URLs using yt-dlp -j.

    Returns {video_id: info_dict} for successfully fetched videos.
    """
    if not urls:
        return {}

    cmd = [
        "yt-dlp",
        "--match-filter", "availability = public",
        "-j",
        "--ignore-errors",
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        "--extractor-args", "youtube:player_client=android",
        "--extractor-args", "youtube:skip=live,members_only",
    ]
    if cookies_path:
        ck = Path(cookies_path)
        if ck.exists():
            cmd.extend(["--cookies", str(ck)])
        # 元数据抓取阶段固定用 android client；cookies 仅用于鉴权，不切换 player_client。
    # Windows 下把大量 URL 直接拼到命令行会触发 WinError 206（文件名或扩展名太长）。
    # 用 yt-dlp 的 batch file 机制避免命令行过长。
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            delete=False,
        ) as tf:
            for u in urls:
                u = (u or "").strip()
                if u:
                    tf.write(u + "\n")
            tmp_path = tf.name
        cmd.extend(["-a", tmp_path])

        result: dict[str, dict] = {}
        stderr_lines: list[str] = []

        def _render_bar(done: int, total: int, width: int = 24) -> str:
            if total <= 0:
                return "[" + ("#" * width) + "]"
            ratio = min(1.0, max(0.0, done / float(total)))
            filled = int(round(ratio * width))
            return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"

        def _progress(done_ok: int, total: int):
            if not show_progress:
                return
            bar = _render_bar(done_ok, total)
            msg = f"\r  [youtube] 元数据进度 {bar} {done_ok}/{total}（成功）"
            # 用 stderr 输出进度，避免与 stdout 的 JSON 流/其他打印混在一起
            sys.stderr.write(msg)
            sys.stderr.flush()

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )

        def _drain_err():
            if proc.stderr:
                for line in proc.stderr:
                    stderr_lines.append(line)

        t = threading.Thread(target=_drain_err, daemon=True)
        t.start()

        count = 0
        total = len(urls)
        _progress(count, total)
        if proc.stdout:
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        info = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    vid = info.get("id")
                    if vid:
                        result[vid] = info
                        if cache_path is not None:
                            _append_metadata_cache(cache_path, info)
                        count += 1
                        if per_item_log:
                            print(f"  [youtube] 元数据 OK: {vid}")
                        elif show_progress and (count % 5 == 0 or count == 1):
                            _progress(count, total)
            except KeyboardInterrupt:
                # 及时终止子进程；已写入 cache 的部分可用于下次续跑
                with suppress(Exception):
                    proc.terminate()
                with suppress(Exception):
                    proc.wait(timeout=5)
                if show_progress:
                    sys.stderr.write("\n")
                    sys.stderr.flush()
                print("  [youtube] 检测到 Ctrl+C：已抓取的元数据已暂存，可下次续跑。")
                raise

        proc.wait(timeout=7200)
        t.join(timeout=5)

        if show_progress:
            _progress(count, total)
            sys.stderr.write("\n")
            sys.stderr.flush()

        if stderr_lines:
            # 只输出错误信息（stderr），避免刷屏
            print("  [youtube] 元数据抓取 stderr（仅在有错误时输出）:")
            for ln in stderr_lines[-200:]:
                print("  " + ln.rstrip("\n"))

        print(f"  [youtube] 元数据获取完成: {len(result)} 条成功 / {len(urls)} 条请求")
        return result
    finally:
        if tmp_path:
            with suppress(Exception):
                Path(tmp_path).unlink(missing_ok=True)


def _has_manual_subtitles(info: dict) -> bool:
    """Check if the video has manually uploaded subtitles (not auto-generated)."""
    subs = info.get("subtitles")
    if not subs or not isinstance(subs, dict):
        return False
    # subtitles dict has language codes as keys; empty dict means no manual subs
    return len(subs) > 0


def _parse_sub_langs(sub_langs: str | None) -> list[str]:
    s = (sub_langs or "").strip()
    if not s:
        return []
    parts = re.split(r"[,\s]+", s)
    langs = []
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        langs.append(p)
    # 去重但保持顺序
    seen: set[str] = set()
    out: list[str] = []
    for x in langs:
        k = x.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    return out


def _has_manual_subtitles_for_langs(info: dict, langs: list[str]) -> bool:
    """是否存在命中指定语种的手动字幕。langs 为空时退化为“有任意手动字幕”."""
    subs = info.get("subtitles")
    if not subs or not isinstance(subs, dict) or not subs:
        return False
    if not langs:
        return True
    keys = [str(k) for k in subs.keys()]
    keys_l = [k.lower() for k in keys]
    for lang in langs:
        l = (lang or "").strip().lower()
        if not l:
            continue
        for k in keys_l:
            if k == l or k.startswith(l + "-"):
                return True
    return False


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

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
    batch_size: int = 500,
    sub_langs: str = "",
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

    cache_path = out_dir / "youtube_metadata_phase1_cache.jsonl"
    cached_map = _load_metadata_cache(cache_path)  # video_id -> info_dict（跨 batch 复用）
    if cached_map:
        print(f"[youtube] Phase 1 缓存已加载: {len(cached_map)} 条（{cache_path.name}）")
    wanted_langs = _parse_sub_langs(sub_langs)
    if wanted_langs:
        print(f"[youtube] 目标手动字幕语种: {', '.join(wanted_langs)}")

    # --- Batched pipeline: Phase 1 → Phase 2 → Phase 3 ---
    total_batches = (len(pending) + max(1, int(batch_size)) - 1) // max(1, int(batch_size))
    for bidx, batch_urls in _iter_batches(pending, batch_size):
        print(f"[youtube] Batch {bidx + 1}/{total_batches}: {len(batch_urls)} 条")

        # -------------------------
        # Phase 1 (per batch)
        # -------------------------
        print("[youtube] Phase 1: 获取元数据 & 检测手动字幕…")
        url_by_id: dict[str, str] = {}
        to_fetch: list[str] = []
        for u in batch_urls:
            vid = _extract_video_id(u)
            if vid:
                url_by_id[vid] = u
                if vid not in cached_map:
                    to_fetch.append(u)
            else:
                to_fetch.append(u)

        if to_fetch:
            print(f"  [youtube] Phase 1 本批需抓取: {len(to_fetch)} 条（其余复用缓存）")
            fetched_map = _batch_fetch_metadata(
                to_fetch,
                cookies_path,
                cache_path=cache_path,
            )
            cached_map.update(fetched_map)
        else:
            fetched_map = {}

        # metadata_map 仅用于本批的字幕检测 & metadata.jsonl 写入
        metadata_map = cached_map

        subtitled_urls: list[str] = []
        unsubtitled_urls: list[str] = []
        meta_collected: dict[str, dict] = {}  # video_id → info for metadata.jsonl

        for u in batch_urls:
            vid = _extract_video_id(u)
            if not vid:
                unsubtitled_urls.append(u)
                continue
            info = metadata_map.get(vid)
            if info:
                meta_collected[vid] = info
                if _has_manual_subtitles_for_langs(info, wanted_langs):
                    subtitled_urls.append(u)
                else:
                    unsubtitled_urls.append(u)
            else:
                unsubtitled_urls.append(u)

        print(
            f"[youtube] 字幕检测结果: 有手动字幕 {len(subtitled_urls)} 条, "
            f"无字幕 {len(unsubtitled_urls)} 条"
        )

        # -------------------------
        # Phase 2 (per batch)
        # -------------------------
        audio_ext = (audio.audio_format or "wav").lower()
        record_base = {
            "platform": "youtube",
            "task_name": task_name,
            "out": str(out_dir),
            "archive": str(archive_path),
            "url_count": len(urls),
            "pending_count": len(pending),
            "skipped_by_archive": skipped,
        }

        # Download subtitled group
        if subtitled_urls:
            print(f"[youtube] Phase 2a: 下载有字幕视频 ({len(subtitled_urls)} 条)…")
            sub_out_tpl = "subtitled/youtube/audios/%(id)s.%(ext)s"
            # 字幕输出到 subs 目录；文件名由 id + language 组成，确保不会出现双扩展
            sub_srt_tpl = "subtitled/youtube/subs/%(id)s.%(language)s.%(ext)s"
            sub_extra = [
                "--write-subs",
                "--no-write-auto-subs",
                "--sub-langs",
                ",".join(wanted_langs) if wanted_langs else "all",
                "--sub-format",
                "vtt",
                "--convert-subs",
                "vtt",
            ]

            if parallel > 1 and len(subtitled_urls) > 1:
                run_yt_dlp_parallel(
                    pending_urls=subtitled_urls,
                    parallel=parallel,
                    out_dir=out_dir,
                    archive_path=archive_path,
                    cookies_path=cookies_path,
                    audio=audio,
                    runtime=runtime,
                    log_path=log_path,
                    record_base={**record_base, "group": "subtitled", "batch": bidx + 1},
                    audio_ext=audio_ext,
                    file_prefix=f"youtube_sub_pending_b{bidx+1}",
                    output_template=sub_out_tpl,
                    subtitle_output_template=sub_srt_tpl,
                    extra_args=sub_extra,
                )
            else:
                pf = out_dir / f"youtube_sub_pending_b{bidx+1}.txt"
                pf.write_text("\n".join(subtitled_urls) + "\n", encoding="utf-8")
                cmd = build_yt_dlp_cmd(
                    out_dir=out_dir,
                    pending_file=pf,
                    archive_path=archive_path,
                    cookies_path=cookies_path,
                    audio=audio,
                    runtime=runtime,
                    output_template=sub_out_tpl,
                    subtitle_output_template=sub_srt_tpl,
                    extra_args=sub_extra,
                )
                run_yt_dlp_and_log(
                    cmd=cmd,
                    out_dir=out_dir,
                    log_path=log_path,
                    record_base={**record_base, "group": "subtitled", "batch": bidx + 1},
                    audio_ext=audio_ext,
                )

        # 校验：字幕必须实际产出 .vtt；否则视作无字幕（音频放到 unsubtitled/audios）
        if wanted_langs and subtitled_urls:
            subs_root = out_dir / "subtitled" / "youtube" / "subs"
            sub_audio_root = out_dir / "subtitled" / "youtube" / "audios"
            nosub_audio_root = out_dir / "unsubtitled" / "youtube" / "audios"
            nosub_audio_root.mkdir(parents=True, exist_ok=True)

            moved = 0
            for u in list(subtitled_urls):
                vid = _extract_video_id(u)
                if not vid:
                    continue
                # 是否存在任意 vtt（匹配语种后缀或无后缀）
                has_vtt = False
                if subs_root.exists():
                    # 允许 id.<lang>.vtt 或 id.vtt
                    for p in subs_root.glob(f"{vid}*.vtt"):
                        if p.is_file():
                            has_vtt = True
                            break
                if has_vtt:
                    continue

                # 没有 vtt：把音频移动到无字幕目录，并将该 URL 加入无字幕组
                src_audio = sub_audio_root / f"{vid}.{audio_ext}"
                dst_audio = nosub_audio_root / f"{vid}.{audio_ext}"
                if src_audio.exists() and not dst_audio.exists():
                    with suppress(Exception):
                        src_audio.replace(dst_audio)
                        moved += 1

                # 清理可能下载到的非 vtt 字幕（例如 json）
                if subs_root.exists():
                    for p in subs_root.glob(f"{vid}*"):
                        if p.is_file() and not p.name.lower().endswith(".vtt"):
                            with suppress(Exception):
                                p.unlink()

                if u not in unsubtitled_urls:
                    unsubtitled_urls.append(u)

            if moved:
                print(f"[youtube] 字幕校验: {moved} 条未产出 vtt，已转入无字幕组（仅保留音频）")

        # Download unsubtitled group
        if unsubtitled_urls:
            print(f"[youtube] Phase 2b: 下载无字幕视频 ({len(unsubtitled_urls)} 条)…")
            nosub_out_tpl = "unsubtitled/youtube/audios/%(id)s.%(ext)s"

            if parallel > 1 and len(unsubtitled_urls) > 1:
                run_yt_dlp_parallel(
                    pending_urls=unsubtitled_urls,
                    parallel=parallel,
                    out_dir=out_dir,
                    archive_path=archive_path,
                    cookies_path=cookies_path,
                    audio=audio,
                    runtime=runtime,
                    log_path=log_path,
                    record_base={**record_base, "group": "unsubtitled", "batch": bidx + 1},
                    audio_ext=audio_ext,
                    file_prefix=f"youtube_nosub_pending_b{bidx+1}",
                    output_template=nosub_out_tpl,
                )
            else:
                pf = out_dir / f"youtube_nosub_pending_b{bidx+1}.txt"
                pf.write_text("\n".join(unsubtitled_urls) + "\n", encoding="utf-8")
                cmd = build_yt_dlp_cmd(
                    out_dir=out_dir,
                    pending_file=pf,
                    archive_path=archive_path,
                    cookies_path=cookies_path,
                    audio=audio,
                    runtime=runtime,
                    output_template=nosub_out_tpl,
                )
                run_yt_dlp_and_log(
                    cmd=cmd,
                    out_dir=out_dir,
                    log_path=log_path,
                    record_base={**record_base, "group": "unsubtitled", "batch": bidx + 1},
                    audio_ext=audio_ext,
                )

        # -------------------------
        # Phase 3 (per batch)
        # -------------------------
        if metadata_path and meta_collected:
            print("[youtube] Phase 3: 写入 metadata.jsonl…")
            need_fetch: list[str] = []

            for vid, info in meta_collected.items():
                upload_date = parse_yt_dlp_date(info.get("upload_date"))
                cats = info.get("categories") or []
                category = ", ".join(cats) if isinstance(cats, list) else str(cats or "")
                if not category:
                    category = info.get("category") or None
                if not upload_date and not category:
                    need_fetch.append(vid)

            if need_fetch:
                print(f"  [youtube] {len(need_fetch)} 条缺少发布日期+类别，补全中…")
                for i, vid in enumerate(need_fetch):
                    watch_url = url_by_id.get(vid)
                    if not watch_url:
                        continue
                    supplemental = fetch_single_video_metadata(watch_url, cookies_path)
                    if supplemental:
                        meta_collected[vid] = supplemental
                        cached_map[vid] = supplemental
                        _append_metadata_cache(cache_path, supplemental)
                    if (i + 1) % 20 == 0:
                        print(f"  [youtube] 补全进度: {i + 1}/{len(need_fetch)}")

            written = 0
            for vid, info in meta_collected.items():
                watch_url = info.get("webpage_url") or url_by_id.get(vid, "")
                duration = info.get("duration") or 0
                channel = info.get("channel") or info.get("uploader") or None
                upload_date = parse_yt_dlp_date(info.get("upload_date"))
                cats = info.get("categories") or []
                category = ", ".join(cats) if isinstance(cats, list) else str(cats or "")
                if not category:
                    category = info.get("category") or None

                write_metadata_entry(
                    metadata_path=metadata_path,
                    video_id=vid,
                    duration_s=float(duration),
                    platform="youtube",
                    url=watch_url,
                    channel=channel,
                    category=category or None,
                    publish_date=upload_date,
                    has_manual_subs=_has_manual_subtitles_for_langs(info, wanted_langs),
                )
                written += 1

            print(f"  [youtube] metadata.jsonl 写入 {written} 条（batch {bidx + 1}）")


if __name__ == "__main__":
    raise SystemExit(
        "请使用 scripts/download/download_all.py 作为入口（它会按平台分发）。"
    )
