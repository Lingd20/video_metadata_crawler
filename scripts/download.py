#!/usr/bin/env python3
"""从 Excel 读取视频 URL 并用 yt-dlp 下载（支持断点续传）。

断点续传机制（两层，与「downloaded.txt + urls」语义一致）：
  1) 显式跳过：启动时读取 downloaded.txt（yt-dlp archive 格式，每行「extractor id」），
     从 Excel 得到的 URL 列表中解析出 (extractor, id)，已在 archive 中的 URL **不会写入
     urls_pending.txt**，也不会再交给 yt-dlp（避免每条仍去拉页面、看起来像又在下载）。
  2) yt-dlp 兜底：仍传入 --download-archive=downloaded.txt，新完成的条目会追加写入；
     若某 URL 无法被本脚本解析 id（例如仅 av 号），则仍依赖 yt-dlp 按 archive 判断。

  - yt-dlp 对「未跳过」的条目仍可做 .part 续传等（视站点而定）。
  - 任务目录内：urls.txt = 本次 Excel 中的全部 URL；urls_pending.txt = 尚未在 archive 中的待下载 URL。

每次运行顺序（与你的预期一致）：
  1) 读取 Excel → 写入 urls.txt（本批「要爬取的全量 URL 清单」，每次运行覆盖重写）
  2) 读取 downloaded.txt → 与 urls 对比 → 写入 urls_pending.txt（仅待下载，每次运行覆盖重写）
  3) yt-dlp 仅消费 urls_pending.txt，并把新完成的条目追加到 downloaded.txt

子目录名（如 BiliBili / BiliBiliBangumi）：
  来自 yt-dlp 的 -o 模板中的 %(extractor)s，即「解析该 URL 时使用的提取器名」。
  普通投稿页多为 BiliBili；番剧/剧集类链接会走 BiliBiliBangumi，因此会多出一个文件夹。
  可用 --flatten-output 将所有音频放到同一子目录 media/ 下（仅按 id 命名，跨站 id 冲突概率极低）。

用法示例：
  python scripts/download.py --excel results/yue_metadata.xlsx
  python scripts/download.py --excel results/yue_metadata.xlsx --out downloads --cookies config/bilibili_cookies.txt
  python scripts/download.py --excel results/yue_metadata.xlsx --limit 10

默认输出音频参数：
  - wav 容器
  - 16kHz 采样率
  - 16bit 位深（s16）
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import wave
from datetime import datetime
from pathlib import Path


def _extract_urls_from_excel(excel_path: str) -> list[str]:
    try:
        import openpyxl
    except Exception as e:
        raise SystemExit(f"缺少依赖 openpyxl，请先安装 requirements.txt。原始错误: {e}")

    wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
    ws = wb.active

    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    try:
        idx = header.index("视频URL")
    except ValueError:
        raise SystemExit(f"未找到列 '视频URL'。表头为: {header}")

    urls: list[str] = []
    seen: set[str] = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        url = row[idx]
        if not url:
            continue
        url = str(url).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _write_jsonl(path: Path, record: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def _format_hms(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


def _list_audio_files(root: Path, ext: str) -> set[Path]:
    if not root.exists():
        return set()
    ext = ext.lower().lstrip(".")
    return {p.resolve() for p in root.rglob(f"*.{ext}") if p.is_file()}


def _wav_duration_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 0
            return (frames / float(rate)) if rate else 0.0
    except Exception:
        return 0.0


def _load_download_archive(archive_path: Path) -> set[tuple[str, str]]:
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


def _url_to_archive_key(url: str) -> tuple[str, str] | None:
    """从 URL 推断与 downloaded.txt 一致的 (extractor, id)；无法识别则返回 None。"""
    u = url.strip()
    if not u:
        return None
    if "bilibili.com" in u:
        m = re.search(r"(BV[a-zA-Z0-9]+)", u, re.I)
        if m:
            return ("bilibili", m.group(1))
    if "youtube.com" in u or "youtu.be" in u:
        m = re.search(
            r"(?:[?&]v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})(?:[&/?#]|$)",
            u,
        )
        if m:
            return ("youtube", m.group(1))
    return None


def _filter_urls_by_archive(
    urls: list[str], archive: set[tuple[str, str]]
) -> tuple[list[str], int]:
    """返回 (pending_urls, skipped_count)。无法解析 id 的 URL 保留在 pending，交给 yt-dlp。"""
    pending: list[str] = []
    skipped = 0
    for url in urls:
        key = _url_to_archive_key(url)
        if key is not None and key in archive:
            skipped += 1
            continue
        pending.append(url)
    return pending, skipped


def main():
    parser = argparse.ArgumentParser(description="从 Excel 读取 URL 用 yt-dlp 下载（支持断点续传）")
    parser.add_argument("--excel", required=True, help="包含“视频URL”列的 Excel 路径")
    parser.add_argument("--out", default="downloads", help="输出根目录（默认: downloads）")
    parser.add_argument(
        "--task-name",
        default="",
        help="任务名（用于输出目录 downloads/<task_name>/...；默认从 xlsx 文件名推导，去掉 _metadata 后缀）",
    )
    parser.add_argument("--cookies", default="", help="Netscape cookies.txt 路径（可选）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（调试用，0 表示不限制）")
    parser.add_argument("--archive", default="", help="download-archive 文件路径（默认: <out>/downloaded.txt）")
    parser.add_argument("--sleep-interval", type=float, default=2.0, help="yt-dlp --sleep-interval（默认 2）")
    parser.add_argument("--max-sleep-interval", type=float, default=6.0, help="yt-dlp --max-sleep-interval（默认 6）")
    parser.add_argument(
        "--format",
        default="",
        help="yt-dlp -f 格式选择（默认留空让 yt-dlp 自选；默认会提取音频为 wav）",
    )
    parser.add_argument("--audio-format", default="wav", help="输出音频格式（默认 wav）")
    parser.add_argument("--audio-sample-rate", type=int, default=16000, help="采样率 Hz（默认 16000）")
    parser.add_argument(
        "--audio-sample-fmt",
        default="s16",
        help="采样格式（默认 s16=16bit；由 ffmpeg 支持，如 s16/s32/flt 等）",
    )
    parser.add_argument("--audio-channels", type=int, default=1, help="声道数（默认 1=单声道）")
    parser.add_argument(
        "--flatten-output",
        action="store_true",
        help="不按 %(extractor)s 分子目录，统一输出到 <任务目录>/media/<id>.<ext>",
    )
    args = parser.parse_args()

    excel_path = Path(args.excel)
    if not excel_path.exists():
        raise SystemExit(f"Excel 不存在: {excel_path}")

    task_name = (args.task_name or "").strip()
    if not task_name:
        stem = excel_path.stem
        task_name = stem[:-9] if stem.endswith("_metadata") else stem

    out_root = Path(args.out)
    out_dir = out_root / task_name
    out_dir.mkdir(parents=True, exist_ok=True)

    archive_path = Path(args.archive) if args.archive else (out_dir / "downloaded.txt")
    log_path = out_dir / "downloads.log.jsonl"

    urls = _extract_urls_from_excel(str(excel_path))
    if args.limit and args.limit > 0:
        urls = urls[: args.limit]

    if not urls:
        print("未从 Excel 中提取到任何 URL。")
        return

    # 1) 先写入全量 URL（来自本次 Excel），再据此做断点判定
    urls_file = out_dir / "urls.txt"
    urls_file.write_text("\n".join(urls) + "\n", encoding="utf-8")
    print(f"已写入全量 URL 列表: {urls_file}（{len(urls)} 条，每次运行覆盖）")

    archive_keys = _load_download_archive(archive_path)
    urls_pending, skipped_by_archive = _filter_urls_by_archive(urls, archive_keys)

    pending_file = out_dir / "urls_pending.txt"
    pending_file.write_text("\n".join(urls_pending) + "\n", encoding="utf-8")

    print(
        f"断点预检: downloaded.txt 中 {len(archive_keys)} 条记录，"
        f"本批 URL 共 {len(urls)} 条，已跳过 {skipped_by_archive} 条，待下载 {len(urls_pending)} 条。"
    )

    if not urls_pending:
        print("无待下载 URL，结束。")
        return

    cmd = [
        "yt-dlp",
        "--ignore-errors",
        "--no-progress",
        "--sleep-interval",
        str(args.sleep_interval),
        "--max-sleep-interval",
        str(args.max_sleep_interval),
        "--download-archive",
        str(archive_path),
        "-P",
        str(out_dir),
        # 默认提取音频并转码为 WAV 16kHz 16bit（需要系统安装 ffmpeg）
        "-x",
        "--audio-format",
        str(args.audio_format),
        "--postprocessor-args",
        f"ffmpeg:-ar {int(args.audio_sample_rate)} -ac {int(args.audio_channels)} -sample_fmt {args.audio_sample_fmt}",
        # 输出模板：默认按 yt-dlp 的 extractor 分子目录；--flatten-output 则统一到 media/
        "-o",
        ("media/%(id)s.%(ext)s" if args.flatten_output else "%(extractor)s/%(id)s.%(ext)s"),
        "-a",
        str(pending_file),
    ]
    if args.cookies:
        ck = Path(args.cookies)
        if not ck.exists():
            raise SystemExit(f"Cookies 文件不存在: {ck}")
        cmd.extend(["--cookies", str(ck)])
    if args.format:
        cmd.extend(["-f", args.format])

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    record_base = {
        "run_id": run_id,
        "ts": datetime.now().isoformat(),
        "excel": str(excel_path),
        "task_name": task_name,
        "out": str(out_dir),
        "archive": str(archive_path),
        "url_count": len(urls),
        "pending_count": len(urls_pending),
        "skipped_by_archive": skipped_by_archive,
        "cmd": cmd,
    }
    _write_jsonl(log_path, {**record_base, "event": "start"})

    print(f"准备下载: {len(urls_pending)} 条 URL（已按 downloaded.txt 预跳过 {skipped_by_archive} 条）")
    print(f"输出目录: {out_dir}")
    print(f"断点记录: {archive_path}")

    # 用“运行前文件集合”来界定“本次新增”的音频文件
    audio_ext = (args.audio_format or "wav").lower()
    before_files = _list_audio_files(out_dir, audio_ext)

    t0 = time.time()
    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        _write_jsonl(log_path, {**record_base, "event": "keyboard_interrupt"})
        after_files = _list_audio_files(out_dir, audio_ext)
        new_files = sorted(after_files - before_files)
        total_sec = sum(_wav_duration_seconds(p) for p in new_files) if audio_ext == "wav" else 0.0
        print("\n检测到 Ctrl+C 中断。")
        print(f"本次新增音频数量: {len(new_files)}")
        if audio_ext == "wav":
            print(f"本次新增音频总时长: {_format_hms(total_sec)}")
        else:
            print(f"本次输出格式为 {audio_ext}，未统计总时长（仅支持 wav 时长统计）。")
        raise SystemExit(130)
    except Exception as e:
        _write_jsonl(log_path, {**record_base, "event": "exception", "error": repr(e)})
        raise
    finally:
        _write_jsonl(
            log_path,
            {**record_base, "event": "end", "elapsed_sec": round(time.time() - t0, 3)},
        )

    print("完成。已下载条目写入 downloaded.txt；下次运行会先按该文件预跳过，再由 yt-dlp archive 兜底。")


if __name__ == "__main__":
    main()

