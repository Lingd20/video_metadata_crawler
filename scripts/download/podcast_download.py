#!/usr/bin/env python3
"""通用播客/音频平台下载模块。

直接下载音频，并从 Excel 表格写入 metadata.jsonl；不再调用 yt-dlp -j
单条抓取元数据。URL→archive key 的解析逻辑（用于 yt-dlp --download-archive
断点续跑）完整保留。
"""

from __future__ import annotations

import hashlib
import html
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urlparse

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

# ---------------------------------------------------------------------------
# archive key 提取（保留，与 yt-dlp --download-archive 断点对齐）
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

_SBS_AUDIO_URL_RE = re.compile(
    r"""https?://[^\s"'<>]+?\.(?:mp3|m4a|aac|ogg|wav|m3u8)(?:\?[^\s"'<>]*)?""",
    re.IGNORECASE,
)


def _url_md5_short(url: str) -> str:
    return hashlib.md5(url.strip().encode()).hexdigest()[:12]


def _extract_direct_audio_id(url: str) -> str | None:
    parsed = urlparse(unquote(url.split("?")[0]))
    name = Path(parsed.path).stem
    if name and len(name) > 3:
        return name
    return None


def _short_podcast_id(seed: str) -> str:
    """生成尽可能短且稳定的文件 id（12 位 md5）。"""
    s = (seed or "").strip()
    if not s:
        s = "podcast"
    return "p" + hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


def _resolve_sbs_audio_url(url: str) -> str | None:
    """对 SBS podcast-episode 页面做兜底解析，尝试从 HTML 中提取直链音频 URL。

    说明：yt-dlp 对该类页面有时会报 Unsupported URL；直链通常可被 generic extractor 稳定下载。
    """
    u = (url or "").strip()
    if not u:
        return None
    ul = u.lower()
    if "sbs.com.au" not in ul or "/podcast-episode/" not in ul:
        return None

    req = urllib.request.Request(
        u,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    # timeout 主要用于避免网络握手/下载卡住拖慢整轮任务
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
    except Exception:
        return None

    # 尽量用 UTF-8 解码；失败则忽略非法字符，保证 regex 可跑
    page_html = raw.decode("utf-8", errors="ignore")
    candidates = _SBS_AUDIO_URL_RE.findall(page_html)
    if not candidates:
        return None

    # 优先选择 mp3，其次 m4a/aac，再其次 m3u8
    def _score(x: str) -> tuple[int, int]:
        xl = x.lower()
        if ".mp3" in xl:
            return (0, len(x))
        if ".m4a" in xl or ".aac" in xl:
            return (1, len(x))
        if ".m3u8" in xl:
            return (2, len(x))
        return (3, len(x))

    best = sorted(dict.fromkeys(candidates), key=_score)[0]
    return html.unescape(best)


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
    metadata_path: Path | None = None,
    metadata_by_url: dict[str, dict] | None = None,
):
    archive_keys = load_download_archive(archive_path)
    pending, skipped = filter_urls_by_archive(urls, archive_keys, url_to_archive_key)

    # Podcast 统一输出 WAV（无论传入 audio_format 是什么）
    audio = YtDlpAudioOptions(
        audio_format="wav",
        audio_sample_rate=int(audio.audio_sample_rate),
        audio_sample_fmt=str(audio.audio_sample_fmt),
        audio_channels=int(audio.audio_channels),
        format=str(audio.format or ""),
        flatten_output=bool(audio.flatten_output),
    )

    # SBS 页面兜底：把不稳定的页面 URL 替换成可直接下载的音频直链
    # 这样既能绕过 Unsupported URL，也能让 archive 用 podcast_direct 的 id 进行后续跳过
    if pending:
        # 通用预解析（并发）：未来新增其它平台解析器时也天然并发。
        # 注意：解析函数内部必须先做域名/路径判断，避免对所有 URL 发起网络请求。
        def _resolve_podcast_direct_url(u: str) -> str | None:
            # SBS
            direct = _resolve_sbs_audio_url(u)
            if direct:
                return direct
            return None

        # 只对“可能需要网络解析”的 URL 进入并发队列；其余 URL 保持原样。
        # 目前只有 SBS podcast-episode 页面会走网络解析；后续可扩展更多条件。
        need_idxs: list[int] = []
        for i, u in enumerate(pending):
            ul = (u or "").lower()
            if "sbs.com.au" in ul and "/podcast-episode/" in ul:
                need_idxs.append(i)

        if need_idxs:
            print(f"[podcast] 预解析音频直链: {len(need_idxs)} 条（并发）…")
            rewrite_hit = 0
            done = 0
            with ThreadPoolExecutor(max_workers=min(8, len(need_idxs))) as pool:
                fut_map = {pool.submit(_resolve_podcast_direct_url, pending[i]): i for i in need_idxs}
                for fut in as_completed(fut_map):
                    i = fut_map[fut]
                    try:
                        direct = fut.result(timeout=12)
                    except Exception:
                        direct = None
                    if direct and direct != pending[i]:
                        pending[i] = direct
                        rewrite_hit += 1
                    done += 1
                    if done % 10 == 0 or done == len(need_idxs):
                        print(
                            f"  [podcast] 预解析进度: {done}/{len(need_idxs)} "
                            f"(rewritten={rewrite_hit})"
                        )

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
    output_tpl = "podcast/audio/%(id)s.%(ext)s"

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
            file_prefix="podcast_pending",
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

    # 下载完成后：把本轮新增的音频文件重命名为短 id（避免过长文件名/路径问题）
    # 同时尽力保留 new_id -> source_url 的映射，供 metadata 写入使用。
    after_files = list_audio_files(out_dir, audio_ext)
    new_files = sorted(after_files - before_files)
    url_by_new_vid: dict[str, str] = {}
    if new_files:
        # 先用旧文件名匹配 pending URL（尽力而为）
        url_by_old_vid: dict[str, str] = {}
        for fp in new_files:
            old_vid = fp.stem
            matched = ""
            for u in pending:
                if old_vid and old_vid in u:
                    matched = u
                    break
            url_by_old_vid[old_vid] = matched

        renamed = 0
        t0_ren = time.time()
        renamed_files: list[Path] = []
        for fp in new_files:
            old_vid = fp.stem
            src_url = url_by_old_vid.get(old_vid, "")
            new_vid = _short_podcast_id(src_url or old_vid or fp.name)
            dst = fp.with_name(f"{new_vid}.{fp.suffix.lstrip('.')}")
            # 避免碰撞：极低概率，但仍防御性处理
            if dst.exists() and dst.resolve() != fp.resolve():
                new_vid = _short_podcast_id(f"{src_url}|{old_vid}|{fp.name}")
                dst = fp.with_name(f"{new_vid}.{fp.suffix.lstrip('.')}")
            if dst.resolve() != fp.resolve():
                try:
                    fp.replace(dst)
                    renamed += 1
                    fp = dst
                except Exception:
                    # 重命名失败就保持原样
                    new_vid = old_vid
            renamed_files.append(fp)
            url_by_new_vid[new_vid] = src_url
        new_files = renamed_files

        if renamed:
            print(
                f"[podcast] 重命名新音频: {renamed}/{len(new_files)} "
                f"个（耗时 {int(time.time() - t0_ren)}s）"
            )

    if not metadata_path:
        return

    if not new_files:
        return

    # 构造 archive_id -> (url, meta) 与 直链 id -> (url, meta) 两张反查表
    key_index: dict[tuple[str, str], tuple[str, dict]] = {}
    direct_id_index: dict[str, tuple[str, dict]] = {}
    for u, meta in (metadata_by_url or {}).items():
        key = url_to_archive_key(u)
        if key is not None:
            key_index[key] = (u, meta)
            if key[0] == "podcast_direct":
                direct_id_index[key[1]] = (u, meta)

    written = 0
    for fp in new_files:
        try:
            fp.relative_to(out_dir / "podcast" / "audio")
        except ValueError:
            continue
        vid = fp.stem
        matched_url = ""
        matched_meta: dict = {}

        # 1) 按 (extractor, id) 精确命中
        for (_ext, key_id), (u, meta) in key_index.items():
            if key_id == vid:
                matched_url = u
                matched_meta = meta
                break

        # 2) 直链文件名兜底（有时 id 会被 normalized）
        if not matched_url and vid in direct_id_index:
            matched_url, matched_meta = direct_id_index[vid]

        # 3) 子串命中（原脚本策略：`vid in u`）
        if not matched_url:
            for u, meta in (metadata_by_url or {}).items():
                if vid and vid in u:
                    matched_url = u
                    matched_meta = meta
                    break

        metainfo = dict(matched_meta.get("metainfo") or {})
        write_metadata_entry(
            metadata_path,
            video_id=vid,
            platform="podcast",
            url=matched_url,
            duration_s=matched_meta.get("duration_s"),
            metainfo_extra=metainfo,
        )
        written += 1

    print(f"  [podcast] metadata.jsonl 写入 {written} 条")


if __name__ == "__main__":
    raise SystemExit(
        "请使用 scripts/download/download_all.py 作为入口（它会按平台分发）。"
    )
