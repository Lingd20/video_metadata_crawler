from __future__ import annotations

import json
import subprocess
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock

_print_lock = Lock()


def _tprint(*args, **kwargs):
    """Thread-safe print."""
    with _print_lock:
        print(*args, **kwargs)
        sys.stdout.flush()


def write_jsonl(path: Path, record: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def format_hms(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


def list_audio_files(root: Path, ext: str) -> set[Path]:
    if not root.exists():
        return set()
    e = ext.lower().lstrip(".")
    return {p.resolve() for p in root.rglob(f"*.{e}") if p.is_file()}


def wav_duration_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 0
            return (frames / float(rate)) if rate else 0.0
    except Exception:
        return 0.0


def load_download_archive(archive_path: Path) -> set[tuple[str, str]]:
    """解析 yt-dlp --download-archive 文件，返回 {(extractor_lower, id), ...}。"""
    keys: set[tuple[str, str]] = set()
    if not archive_path.exists():
        return keys
    for line in archive_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        keys.add((parts[0].lower(), parts[1]))
    return keys


def filter_urls_by_archive(
    urls: list[str],
    archive: set[tuple[str, str]],
    url_to_key,
) -> tuple[list[str], int]:
    """返回 (pending_urls, skipped_count)。无法解析 id 的 URL 保留在 pending，交给 yt-dlp。"""
    pending: list[str] = []
    skipped = 0
    for url in urls:
        key = url_to_key(url)
        if key is not None and key in archive:
            skipped += 1
            continue
        pending.append(url)
    return pending, skipped


@dataclass(frozen=True)
class YtDlpAudioOptions:
    audio_format: str = "wav"
    audio_sample_rate: int = 16000
    audio_sample_fmt: str = "s16"
    audio_channels: int = 1
    format: str = ""  # yt-dlp -f
    flatten_output: bool = False


PLATFORM_SLEEP_DEFAULTS: dict[str, tuple[float, float]] = {
    "youtube": (2.0, 6.0),
    "bilibili": (1.0, 3.0),
    "podcast": (0.5, 2.0),
}


@dataclass(frozen=True)
class YtDlpRuntimeOptions:
    sleep_interval: float = 2.0
    max_sleep_interval: float = 6.0


def build_yt_dlp_cmd(
    *,
    out_dir: Path,
    pending_file: Path | None = None,
    archive_path: Path,
    cookies_path: str,
    audio: YtDlpAudioOptions,
    runtime: YtDlpRuntimeOptions,
    output_template: str = "",
    subtitle_output_template: str = "",
    extra_args: list[str] | None = None,
    format_override: str = "",
) -> list[str]:
    if not output_template:
        output_template = (
            "media/%(id)s.%(ext)s" if audio.flatten_output
            else "%(extractor)s/%(id)s.%(ext)s"
        )

    cmd = [
        "yt-dlp",
        "--ignore-errors",
        "--no-progress",
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        "--sleep-interval", str(runtime.sleep_interval),
        "--max-sleep-interval", str(runtime.max_sleep_interval),
        "--download-archive", str(archive_path),
        "-P", str(out_dir),
        "-x",
        "--audio-format", str(audio.audio_format),
        "--postprocessor-args",
        f"ffmpeg:-ar {int(audio.audio_sample_rate)} -ac {int(audio.audio_channels)} -sample_fmt {audio.audio_sample_fmt}",
        "-o", output_template,
    ]
    if subtitle_output_template:
        # 使用 subtitle: 避免 Windows 路径出现 subtitles# 前缀目录
        cmd.extend(["-o", f"subtitle:{subtitle_output_template}"])
    if pending_file:
        cmd.extend(["-a", str(pending_file)])
    if cookies_path:
        ck = Path(cookies_path)
        if not ck.exists():
            raise SystemExit(f"Cookies 文件不存在: {ck}")
        cmd.extend(["--cookies", str(ck)])
    # 下载阶段固定用 android client，避免 web 字幕需要 PO Token 而导致字幕缺失。
    # cookies 仅用于鉴权，不切换 player_client。
    cmd.extend(["--extractor-args", "youtube:player_client=android"])
    cmd.extend(["--extractor-args", "youtube:skip=live,members_only"])
    fmt = format_override or audio.format or "bestaudio/best"
    cmd.extend(["-f", fmt])
    if extra_args:
        cmd.extend(extra_args)
    return cmd


# ---------------------------------------------------------------------------
# Single-process runner (unchanged logic)
# ---------------------------------------------------------------------------

def run_yt_dlp_and_log(
    *,
    cmd: list[str],
    out_dir: Path,
    log_path: Path,
    record_base: dict,
    audio_ext: str,
):
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = {**record_base, "run_id": run_id, "ts": datetime.now().isoformat(), "cmd": cmd}
    write_jsonl(log_path, {**base, "event": "start"})

    before_files = list_audio_files(out_dir, audio_ext)
    t0 = time.time()
    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        write_jsonl(log_path, {**base, "event": "keyboard_interrupt"})
        after_files = list_audio_files(out_dir, audio_ext)
        new_files = sorted(after_files - before_files)
        total_sec = sum(wav_duration_seconds(p) for p in new_files) if audio_ext == "wav" else 0.0
        print("\n检测到 Ctrl+C 中断。")
        print(f"本次新增音频数量: {len(new_files)}")
        if audio_ext == "wav":
            print(f"本次新增音频总时长: {format_hms(total_sec)}")
        else:
            print(f"本次输出格式为 {audio_ext}，未统计总时长（仅支持 wav 时长统计）。")
        raise SystemExit(130)
    except Exception as e:
        write_jsonl(log_path, {**base, "event": "exception", "error": repr(e)})
        raise
    finally:
        write_jsonl(log_path, {**base, "event": "end", "elapsed_sec": round(time.time() - t0, 3)})


# ---------------------------------------------------------------------------
# Parallel runner
# ---------------------------------------------------------------------------

def _run_worker(
    worker_id: int,
    cmd: list[str],
    platform: str,
    url_count: int,
) -> tuple[int, int | None]:
    """Run a single yt-dlp worker, prefixing every output line with worker id."""
    prefix = f"[{platform}:w{worker_id}]"
    _tprint(f"{prefix} 开始下载 {url_count} 条")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if proc.stdout:
            for line in proc.stdout:
                _tprint(f"{prefix} {line}", end="")
        proc.wait()
        rc = proc.returncode
    except Exception as e:
        _tprint(f"{prefix} 异常 — {e}")
        return worker_id, None
    _tprint(f"{prefix} 完成 (exit={rc})")
    return worker_id, rc


def run_yt_dlp_parallel(
    *,
    pending_urls: list[str],
    parallel: int,
    out_dir: Path,
    archive_path: Path,
    cookies_path: str,
    audio: YtDlpAudioOptions,
    runtime: YtDlpRuntimeOptions,
    log_path: Path,
    record_base: dict,
    audio_ext: str,
    file_prefix: str = "pending",
    output_template: str = "",
    subtitle_output_template: str = "",
    extra_args: list[str] | None = None,
    format_override: str = "",
):
    """Split pending URLs across *parallel* yt-dlp workers and run concurrently."""
    n = max(1, min(parallel, len(pending_urls)))
    chunk_size = (len(pending_urls) + n - 1) // n
    chunks = [pending_urls[i : i + chunk_size] for i in range(0, len(pending_urls), chunk_size)]

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    platform = record_base.get("platform", "unknown")
    base = {
        **record_base,
        "run_id": run_id,
        "ts": datetime.now().isoformat(),
        "parallel": n,
    }
    write_jsonl(log_path, {**base, "event": "parallel_start"})

    print(f"[{platform}] 并行下载: {len(pending_urls)} 条 URL, {len(chunks)} 个 worker")

    cmds: list[list[str]] = []
    for idx, chunk in enumerate(chunks):
        pf = out_dir / f"{file_prefix}_{idx}.txt"
        pf.write_text("\n".join(chunk) + "\n", encoding="utf-8")
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
            format_override=format_override,
        )
        cmds.append(cmd)

    before_files = list_audio_files(out_dir, audio_ext)
    t0 = time.time()
    interrupted = False

    try:
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            futures = {
                pool.submit(_run_worker, idx, cmd, platform, len(chunks[idx])): idx
                for idx, cmd in enumerate(cmds)
            }
            for fut in as_completed(futures):
                wid, rc = fut.result()
    except KeyboardInterrupt:
        interrupted = True
        print(f"\n[{platform}] 检测到 Ctrl+C 中断，等待 worker 结束…")

    elapsed = round(time.time() - t0, 3)
    after_files = list_audio_files(out_dir, audio_ext)
    new_files = sorted(after_files - before_files)
    total_sec = sum(wav_duration_seconds(p) for p in new_files) if audio_ext == "wav" else 0.0

    evt = "keyboard_interrupt" if interrupted else "parallel_end"
    write_jsonl(log_path, {**base, "event": evt, "elapsed_sec": elapsed, "new_files": len(new_files)})

    print(f"[{platform}] 本次新增音频: {len(new_files)} 个, 耗时 {format_hms(elapsed)}")
    if audio_ext == "wav" and new_files:
        print(f"[{platform}] 新增音频总时长: {format_hms(total_sec)}")

    if interrupted:
        raise SystemExit(130)


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def write_metadata_entry(
    metadata_path: Path,
    *,
    video_id: str,
    platform: str,
    url: str,
    duration_s: float | None = None,
    metainfo_extra: dict | None = None,
):
    """Append one entry to metadata.jsonl.

    metainfo 字段直接由调用方从 Excel 行映射而来（不再做 yt-dlp -j 二次抓取）。
    `platform` 和 `url` 会覆盖 metainfo_extra 中同名字段，保证权威。
    """
    metainfo: dict = {"platform": platform, "url": url}
    if metainfo_extra:
        for k, v in metainfo_extra.items():
            if k in ("platform", "url"):
                continue
            metainfo[k] = v

    entry: dict = {"id": video_id}
    if duration_s is not None:
        entry["duration_s"] = round(float(duration_s), 2)
    entry["metainfo"] = metainfo
    write_jsonl(metadata_path, entry)
