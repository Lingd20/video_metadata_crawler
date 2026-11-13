from __future__ import annotations

import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from yt_dlp_common import (
    YtDlpAudioOptions,
    YtDlpRuntimeOptions,
    build_yt_dlp_cmd,
    parse_sub_lang_patterns,
    write_jsonl,
)


def extract_youtube_video_id(url: str) -> str | None:
    m = re.search(
        r"(?:[?&]v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})(?:[&/?#]|$)",
        url or "",
    )
    return m.group(1) if m else None


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


def _find_subtitle_files(out_dir: Path, video_id: str) -> list[Path]:
    subs_dir = out_dir / "subtitled" / "youtube" / "subs"
    if not video_id or not subs_dir.is_dir():
        return []
    return sorted(
        p for p in subs_dir.glob(f"{video_id}.*")
        if p.is_file() and p.suffix.lower() in {".vtt", ".srt", ".ass", ".ttml", ".srv3", ".srv2", ".srv1"}
    )


def _remove_archive_entry(archive_path: Path, video_id: str) -> None:
    """从 download-archive 移除 youtube <id>，便于无字幕失败后下次重试。"""
    if not video_id or not archive_path.exists():
        return
    needle = f"youtube {video_id}"
    lines = archive_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    kept = [ln for ln in lines if ln.strip() != needle]
    if len(kept) != len(lines):
        archive_path.write_text(
            ("\n".join(kept) + ("\n" if kept else "")),
            encoding="utf-8",
        )


def download_youtube_manual_subs(
    url: str,
    *,
    out_dir: Path,
    archive_path: Path,
    cookies_path: str,
    audio: YtDlpAudioOptions,
    runtime: YtDlpRuntimeOptions,
    log_path: Path,
    sub_langs: str = "",
) -> bool:
    """下载单条 YouTube 视频（音频 + 手动字幕）。

    若配置了具体 sub_langs，则必须实际写出字幕文件才算成功。
    """
    u = (url or "").strip()
    if not u:
        return False

    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", delete=False) as tf:
        tf.write(u + "\n")
        pending_file = Path(tf.name)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = {
        "platform": "youtube",
        "event": "single_download",
        "run_id": run_id,
        "ts": datetime.now().isoformat(),
        "url": u,
    }
    write_jsonl(log_path, {**base, "phase": "start"})

    cmd = build_yt_dlp_cmd(
        out_dir=out_dir,
        pending_file=pending_file,
        archive_path=archive_path,
        cookies_path=cookies_path,
        audio=audio,
        runtime=runtime,
        output_template="subtitled/youtube/audios/%(id)s.%(ext)s",
        subtitle_output_template="subtitled/youtube/subs/%(id)s.%(language)s.%(ext)s",
        extra_args=_sub_extra_args(sub_langs),
    )

    proc = None
    ok = False
    vid = extract_youtube_video_id(u)
    try:
        proc = subprocess.run(cmd, check=False)
        ok = proc.returncode == 0
        # yt-dlp 在「无请求语种字幕」时仍可能 exit 0 并只留下音频；
        # 配置了具体语种时，必须落盘字幕文件才算成功。
        require_subs = parse_sub_lang_patterns(sub_langs) is not None
        if ok and require_subs:
            sub_files = _find_subtitle_files(out_dir, vid or "")
            if not sub_files:
                ok = False
                _remove_archive_entry(archive_path, vid or "")
                write_jsonl(
                    log_path,
                    {
                        **base,
                        "phase": "missing_subtitles",
                        "sub_langs": sub_langs,
                        "video_id": vid,
                    },
                )
    except Exception as e:
        write_jsonl(log_path, {**base, "phase": "exception", "error": repr(e)})
        return False
    finally:
        pending_file.unlink(missing_ok=True)

    write_jsonl(
        log_path,
        {
            **base,
            "phase": "end",
            "ok": ok,
            "exit_code": proc.returncode if proc else -1,
            "video_id": vid,
        },
    )
    return ok
