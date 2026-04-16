#!/usr/bin/env python3
"""从一个或多个 Excel 读取 URL，合并去重后按平台调用下载脚本（yt-dlp）。"""

from __future__ import annotations

import argparse
from pathlib import Path

from bilibili_download import download_bilibili
from excel_urls import extract_urls_from_excels
from platform_detect import split_urls_by_platform
from youtube_download import download_youtube
from yt_dlp_common import YtDlpAudioOptions, YtDlpRuntimeOptions


def _default_task_name(excel_paths: list[str]) -> str:
    if len(excel_paths) == 1:
        stem = Path(excel_paths[0]).stem
        return stem[:-9] if stem.endswith("_metadata") else stem
    return "merged"


def main():
    parser = argparse.ArgumentParser(description="从一个或多个 Excel 读取 URL，用 yt-dlp 下载（按平台分发）")
    parser.add_argument(
        "--excel",
        action="append",
        default=[],
        help="包含“视频URL”列的 Excel 路径；可重复传参指定多个",
    )
    parser.add_argument(
        "--excels",
        nargs="+",
        default=[],
        help="包含“视频URL”列的多个 Excel 路径（等价于多次 --excel）",
    )
    parser.add_argument("--out", default="downloads", help="输出根目录（默认: downloads）")
    parser.add_argument(
        "--task-name",
        default="",
        help="任务名（用于输出目录 downloads/<task_name>/...；默认：单 xlsx 用文件名推导，多 xlsx 用 merged）",
    )
    parser.add_argument("--cookies-bilibili", default="", help="B 站 cookies.txt（可选）")
    parser.add_argument("--cookies-youtube", default="", help="YouTube cookies.txt（可选）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（调试用，0 表示不限制）")
    parser.add_argument("--archive", default="", help="download-archive 文件路径（默认: <任务目录>/downloaded.txt）")
    parser.add_argument("--sleep-interval", type=float, default=2.0, help="yt-dlp --sleep-interval（默认 2）")
    parser.add_argument("--max-sleep-interval", type=float, default=6.0, help="yt-dlp --max-sleep-interval（默认 6）")
    parser.add_argument("--format", default="", help="yt-dlp -f 格式选择（默认留空让 yt-dlp 自选）")
    parser.add_argument("--audio-format", default="wav", help="输出音频格式（默认 wav）")
    parser.add_argument("--audio-sample-rate", type=int, default=16000, help="采样率 Hz（默认 16000）")
    parser.add_argument("--audio-sample-fmt", default="s16", help="采样格式（默认 s16=16bit）")
    parser.add_argument("--audio-channels", type=int, default=1, help="声道数（默认 1=单声道）")
    parser.add_argument(
        "--flatten-output",
        action="store_true",
        help="不按 %%(extractor)s 分子目录，统一输出到 <任务目录>/media/<id>.<ext>",
    )
    parser.add_argument(
        "-j",
        "--parallel",
        type=int,
        default=2,
        help="并行下载数（同时运行的 yt-dlp 进程数，默认 2；设为 1 禁用并行）",
    )
    args = parser.parse_args()

    excel_paths = [*args.excel, *args.excels]
    excel_paths = [str(Path(p)) for p in excel_paths if str(p).strip()]
    if not excel_paths:
        raise SystemExit("请至少指定一个 Excel：--excel a.xlsx 或 --excels a.xlsx b.xlsx ...")

    urls = extract_urls_from_excels(excel_paths)
    if args.limit and args.limit > 0:
        urls = urls[: args.limit]
    if not urls:
        print("未从 Excel 中提取到任何 URL。")
        return

    task_name = (args.task_name or "").strip() or _default_task_name(excel_paths)
    out_root = Path(args.out)
    out_dir = out_root / task_name
    out_dir.mkdir(parents=True, exist_ok=True)

    # 记录全量 URL（合并去重后的）
    urls_file = out_dir / "urls.txt"
    urls_file.write_text("\n".join(urls) + "\n", encoding="utf-8")
    print(f"已写入合并去重后的全量 URL: {urls_file}（{len(urls)} 条）")

    archive_path = Path(args.archive) if args.archive else (out_dir / "downloaded.txt")
    log_path = out_dir / "downloads.log.jsonl"

    audio = YtDlpAudioOptions(
        audio_format=args.audio_format,
        audio_sample_rate=int(args.audio_sample_rate),
        audio_sample_fmt=str(args.audio_sample_fmt),
        audio_channels=int(args.audio_channels),
        format=str(args.format or ""),
        flatten_output=bool(args.flatten_output),
    )
    runtime = YtDlpRuntimeOptions(
        sleep_interval=float(args.sleep_interval),
        max_sleep_interval=float(args.max_sleep_interval),
    )

    by_platform = split_urls_by_platform(urls)
    if by_platform.get("unknown"):
        unknown_file = out_dir / "unknown_urls.txt"
        unknown_file.write_text("\n".join(by_platform["unknown"]) + "\n", encoding="utf-8")
        print(f"发现无法识别平台的 URL（{len(by_platform['unknown'])} 条），已写入: {unknown_file}")

    # 按平台分别运行下载逻辑（共享同一个 archive）
    if by_platform.get("bilibili"):
        download_bilibili(
            urls=by_platform["bilibili"],
            out_dir=out_dir,
            archive_path=archive_path,
            cookies_path=args.cookies_bilibili,
            audio=audio,
            runtime=runtime,
            log_path=log_path,
            task_name=task_name,
            parallel=args.parallel,
        )
    if by_platform.get("youtube"):
        download_youtube(
            urls=by_platform["youtube"],
            out_dir=out_dir,
            archive_path=archive_path,
            cookies_path=args.cookies_youtube,
            audio=audio,
            runtime=runtime,
            log_path=log_path,
            task_name=task_name,
            parallel=args.parallel,
        )

    print("完成。已下载条目写入 downloaded.txt；下次运行会先按该文件预跳过，再由 yt-dlp archive 兜底。")


if __name__ == "__main__":
    main()

