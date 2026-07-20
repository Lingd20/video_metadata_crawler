from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
import tempfile

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


def load_failed_urls(path: Path) -> set[str]:
    """读取失败 URL 列表（每行一个 URL），返回小集合用于预跳过。"""
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = (line or "").strip()
        if not s or s.startswith("#"):
            continue
        out.add(s)
    return out


def append_failed_urls(path: Path, urls: list[str]) -> int:
    """追加失败 URL（去重后追加），返回本次新增写入条数。"""
    if not urls:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_failed_urls(path)
    new = [u.strip() for u in urls if u and u.strip() and u.strip() not in existing]
    if not new:
        return 0
    with path.open("a", encoding="utf-8") as f:
        for u in new:
            f.write(u + "\n")
    return len(new)


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
    has_manual_subs: bool | None = None,
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
    if has_manual_subs is not None:
        entry["has_manual_subs"] = bool(has_manual_subs)
    entry["metainfo"] = metainfo
    write_jsonl(metadata_path, entry)


# ---------------------------------------------------------------------------
# Subtitle mode helpers
# ---------------------------------------------------------------------------

# 1) audio_only  : 不检查字幕，全部只下音频
# 2) manual_only : 只下载有手动字幕的（音频+字幕）
# 3) mixed       : 有手动字幕 → 音频+字幕；否则只下音频
SUBTITLE_MODES = ("audio_only", "manual_only", "mixed")


def normalize_subtitle_mode(mode: str | None) -> str:
    m = (mode or "audio_only").strip().lower()
    aliases = {
        "1": "audio_only",
        "audio": "audio_only",
        "none": "audio_only",
        "2": "manual_only",
        "manual": "manual_only",
        "subs_only": "manual_only",
        "3": "mixed",
        "auto": "mixed",
    }
    m = aliases.get(m, m)
    if m not in SUBTITLE_MODES:
        raise SystemExit(
            f"未知 subtitle_mode={mode!r}，可选: audio_only | manual_only | mixed"
        )
    return m


# B 站把弹幕也放进 subtitles["danmaku"]，不算手动字幕
_NON_MANUAL_SUB_KEYS = frozenset({"danmaku"})


def has_manual_subtitles(info: dict | None) -> bool:
    """yt-dlp info 中是否存在手动字幕（不含 automatic_captions / danmaku）。"""
    if not info:
        return False
    subs = info.get("subtitles")
    if not isinstance(subs, dict) or not subs:
        return False
    return any(k for k in subs if str(k).lower() not in _NON_MANUAL_SUB_KEYS)


def batch_probe_manual_subtitles(
    urls: list[str],
    cookies_path: str = "",
    *,
    batch_size: int = 50,
) -> dict[str, bool]:
    """批量探测 URL 是否有手动字幕。

    返回 {url: has_manual_subs}。探测失败的 URL 记为 False（视为无字幕）。

    注意：B 站 extractor 在纯 `-j --skip-download` 时经常不填充 subtitles；
    需加 `--write-subs` 才会把手动字幕写入 JSON（配合 --skip-download 不会落盘）。
    """
    result: dict[str, bool] = {u: False for u in urls if (u or "").strip()}
    if not result:
        return {}

    pending = list(result.keys())
    bs = max(1, int(batch_size or 50))

    for i in range(0, len(pending), bs):
        chunk = pending[i : i + bs]
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".txt", delete=False,
            ) as tf:
                for u in chunk:
                    tf.write(u + "\n")
                tmp_path = tf.name

            # 注意：
            # - B 站需 --write-subs 才会在 -j 中填充 subtitles（配合 --skip-download 不落盘）
            # - 探测阶段不加 --js-runtimes（部分 yt-dlp 版本不支持，会导致整批失败）
            cmd = [
                "yt-dlp",
                "--ignore-config",
                "-j",
                "--skip-download",
                "--write-subs",
                "--no-write-auto-subs",
                "--sub-langs", "all",
                "--ignore-errors",
                "--ignore-no-formats",
                "--extractor-args", "youtube:player_client=android",
                "--extractor-args", "youtube:skip=live,members_only",
                "-a", tmp_path,
            ]
            if cookies_path:
                ck = Path(cookies_path)
                if ck.exists():
                    cmd.extend(["--cookies", str(ck)])

            # 建 id/url 反查：优先 webpage_url 精确匹配，否则用 id 子串匹配
            id_to_urls: dict[str, list[str]] = {}
            for u in chunk:
                # youtube 11-char / bilibili BV
                m = re.search(
                    r"(?:[?&]v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})(?:[&/?#]|$)",
                    u,
                )
                if m:
                    id_to_urls.setdefault(m.group(1), []).append(u)
                    continue
                m = re.search(r"(BV[a-zA-Z0-9]+)", u, re.I)
                if m:
                    id_to_urls.setdefault(m.group(1), []).append(u)

            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    info = json.loads(line)
                except json.JSONDecodeError:
                    continue
                flag = has_manual_subtitles(info)
                webpage = info.get("webpage_url") or info.get("original_url") or ""
                vid = str(info.get("id") or "")
                matched = False
                if webpage and webpage in result:
                    result[webpage] = flag
                    matched = True
                if not matched and vid and vid in id_to_urls:
                    for u in id_to_urls[vid]:
                        result[u] = flag
                    matched = True
                if not matched and vid:
                    for u in chunk:
                        if vid in u:
                            result[u] = flag
        except Exception as e:
            print(f"  [subtitle-probe] batch 失败: {e}")
        finally:
            if tmp_path:
                with suppress(Exception):
                    Path(tmp_path).unlink(missing_ok=True)

        done = min(i + bs, len(pending))
        print(f"  [subtitle-probe] 进度 {done}/{len(pending)}")

    return result
