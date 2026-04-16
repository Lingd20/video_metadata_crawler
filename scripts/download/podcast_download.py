#!/usr/bin/env python3
"""通用播客/音频平台下载模块。

覆盖 SBS、Spotify/Anchor、喜马拉雅、小宇宙FM、Podbean、SoundOn、
Buzzsprout、Omny、Acast、SoundCloud、RTHK、荔枝FM 等平台，
以及各种 CDN 直链（.mp3/.m4a）。大部分由 yt-dlp generic extractor 或
平台特定 extractor 自动处理。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from yt_dlp_common import (
    YtDlpAudioOptions,
    YtDlpRuntimeOptions,
    build_yt_dlp_cmd,
    filter_urls_by_archive,
    load_download_archive,
    run_yt_dlp_and_log,
    run_yt_dlp_parallel,
)

# ---------------------------------------------------------------------------
# archive key 提取：尽力从 URL 中抽取与 yt-dlp 一致的 (extractor, id)，
# 无法识别时返回 None，交给 yt-dlp --download-archive 兜底。
# ---------------------------------------------------------------------------

_SBS_SLUG_RE = re.compile(r"/podcast-episode/[^/]+/([a-z0-9]+)$")
_BUZZSPROUT_RE = re.compile(r"buzzsprout\.com/\d+/episodes/(\d+)")
_ANCHOR_PLAY_RE = re.compile(r"anchor\.fm/.+?/play/(\d+)/")
_SPOTIFY_EP_RE = re.compile(r"podcasters\.spotify\.com/.+?/episodes/.+-([a-z0-9]+)$")
_SOUNDCLOUD_RE = re.compile(r"soundcloud\.com/stream/(\d+)")
_ACAST_EP_RE = re.compile(r"acast\.com/.+?/episodes/(.+?)(?:\?|$)")
_XIMALAYA_TRACK_RE = re.compile(r"ximalaya\.com/sound/(\d+)")
_XIAOYUZHOU_EP_RE = re.compile(r"xiaoyuzhoufm\.com/episode/([a-f0-9]+)")
_SOUNDON_EP_RE = re.compile(r"player\.soundon\.fm/p/[^/]+/episodes/([a-f0-9-]+)")


def _url_md5_short(url: str) -> str:
    """URL 的 MD5 前 12 位，用于无法提取原生 ID 时做 fallback key。"""
    return hashlib.md5(url.strip().encode()).hexdigest()[:12]


def _extract_direct_audio_id(url: str) -> str | None:
    """从直链 URL 路径中提取不含扩展名的文件名作为 ID。"""
    parsed = urlparse(unquote(url.split("?")[0]))
    name = Path(parsed.path).stem
    if name and len(name) > 3:
        return name
    return None


def url_to_archive_key(url: str) -> tuple[str, str] | None:
    """尝试把 URL 映射到 yt-dlp archive 中的 (extractor, id) key。

    返回 None 表示无法预判，保留在 pending 交给 yt-dlp 自行判断。
    """
    u = (url or "").strip()
    if not u:
        return None
    ul = u.lower()

    if "sbs.com.au" in ul:
        m = _SBS_SLUG_RE.search(u)
        if m:
            return ("sbs", m.group(1))

    if "buzzsprout.com" in ul:
        m = _BUZZSPROUT_RE.search(u)
        if m:
            return ("buzzsprout", m.group(1))

    if "anchor.fm" in ul:
        m = _ANCHOR_PLAY_RE.search(u)
        if m:
            return ("anchor", m.group(1))

    if "podcasters.spotify.com" in ul:
        m = _SPOTIFY_EP_RE.search(u)
        if m:
            return ("spotify_podcasters", m.group(1))

    if "soundcloud.com" in ul:
        m = _SOUNDCLOUD_RE.search(u)
        if m:
            return ("soundcloud", m.group(1))

    if "acast.com" in ul:
        m = _ACAST_EP_RE.search(u)
        if m:
            return ("acast", m.group(1))

    if "ximalaya.com" in ul:
        m = _XIMALAYA_TRACK_RE.search(u)
        if m:
            return ("ximalaya", m.group(1))

    if "xiaoyuzhoufm.com" in ul:
        m = _XIAOYUZHOU_EP_RE.search(u)
        if m:
            return ("xiaoyuzhou", m.group(1))

    if "player.soundon.fm" in ul:
        m = _SOUNDON_EP_RE.search(u)
        if m:
            return ("soundon", m.group(1))

    # 直链音频：以 .mp3 / .m4a / .mp4 / .wav / .ogg 结尾的 URL
    path_lower = urlparse(ul).path
    if any(path_lower.endswith(ext) for ext in (".mp3", ".m4a", ".mp4", ".wav", ".ogg")):
        fid = _extract_direct_audio_id(u)
        if fid:
            return ("podcast_direct", fid)

    return None


def download_podcast(
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
):
    archive_keys = load_download_archive(archive_path)
    pending, skipped = filter_urls_by_archive(urls, archive_keys, url_to_archive_key)

    urls_file = out_dir / "podcast_urls.txt"
    urls_file.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    pending_file = out_dir / "podcast_urls_pending.txt"
    pending_file.write_text("\n".join(pending) + ("\n" if pending else ""), encoding="utf-8")

    print(
        f"[podcast] 断点预检: archive {len(archive_keys)} 条，本批 {len(urls)} 条，"
        f"跳过 {skipped} 条，待下载 {len(pending)} 条。"
    )
    if not pending:
        return

    record_base = {
        "platform": "podcast",
        "task_name": task_name,
        "out": str(out_dir),
        "archive": str(archive_path),
        "url_count": len(urls),
        "pending_count": len(pending),
        "skipped_by_archive": skipped,
    }
    audio_ext = (audio.audio_format or "wav").lower()

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
            file_prefix="podcast_pending",
        )
    else:
        cmd = build_yt_dlp_cmd(
            out_dir=out_dir,
            pending_file=pending_file,
            archive_path=archive_path,
            cookies_path=cookies_path,
            audio=audio,
            runtime=runtime,
        )
        run_yt_dlp_and_log(
            cmd=cmd,
            out_dir=out_dir,
            log_path=log_path,
            record_base=record_base,
            audio_ext=audio_ext,
        )


if __name__ == "__main__":
    raise SystemExit(
        "请使用 scripts/download/download_all.py 作为入口（它会按平台分发）。"
    )
