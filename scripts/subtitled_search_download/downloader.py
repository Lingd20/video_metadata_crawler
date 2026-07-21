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
    """下载单条 YouTube 视频（音频 + 手动字幕）。成功返回 True。"""
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
    try:
        proc = subprocess.run(cmd, check=False)
        ok = proc.returncode == 0
    except Exception as e:
        write_jsonl(log_path, {**base, "phase": "exception", "error": repr(e)})
        return False
    finally:
        pending_file.unlink(missing_ok=True)

    write_jsonl(log_path, {**base, "phase": "end", "ok": ok, "exit_code": proc.returncode if proc else -1})
    return ok
