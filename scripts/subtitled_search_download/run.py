#!/usr/bin/env python3
"""YouTube 手动字幕视频：搜索 + 即时下载（单阶段流水线）。

与 scripts/url_crawl 使用相同的 config.yaml 与 input 文件格式，
但只处理 YouTube 条目；发现手动字幕后立即下载，无需先导出 Excel 再下载。

用法:
  python scripts/subtitled_search_download/run.py \\
      --config config/config.yaml \\
      --input input/input_yue.txt
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "url_crawl"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "download"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "subtitled_search_download"))

from downloader import download_youtube_manual_subs, extract_youtube_video_id
from utils import (
    CheckpointManager,
    deduplicate_videos,
    load_config,
    parse_input_file,
    save_to_excel,
    setup_logging,
)
from youtube_crawler import YouTubeCrawler
from yt_dlp_common import (
    PLATFORM_SLEEP_DEFAULTS,
    YtDlpAudioOptions,
    YtDlpRuntimeOptions,
    has_manual_subtitles,
    load_download_archive,
    write_metadata_entry,
)

logger = logging.getLogger(__name__)

YOUTUBE_TYPES = {"youtube_channel", "youtube_playlist"}

_interrupted = False
_skip_current = threading.Event()


def _handle_sigint(signum, frame):
    global _interrupted
    if _interrupted:
        logger.warning("强制退出")
        sys.exit(1)
    _interrupted = True
    logger.warning("收到中断信号，当前任务完成后将安全退出…")
    _skip_current.set()


class SubtitledPipeline:
    def __init__(
        self,
        *,
        out_dir: Path,
        archive_path: Path,
        metadata_path: Path,
        log_path: Path,
        cookies_path: str,
        audio: YtDlpAudioOptions,
        runtime: YtDlpRuntimeOptions,
        sub_langs: str = "",
    ):
        self.out_dir = out_dir
        self.archive_path = archive_path
        self.metadata_path = metadata_path
        self.log_path = log_path
        self.cookies_path = cookies_path
        self.audio = audio
        self.runtime = runtime
        self.sub_langs = sub_langs
        self._archive_keys = load_download_archive(archive_path)
        self.stats = {
            "no_manual_subs": 0,
            "already_archived": 0,
            "downloaded": 0,
            "download_failed": 0,
        }

    def _reload_archive(self):
        self._archive_keys = load_download_archive(self.archive_path)

    def _video_to_metainfo(self, video: dict) -> dict:
        return {
            "channel": video.get("uploader") or "",
            "title": video.get("title") or "",
            "publish_date": (video.get("publish_time") or "")[:10],
            "source_name": video.get("source_name") or "",
            "source_type": video.get("source_type") or "",
            "category": video.get("category") or "",
            "view_count": video.get("view_count") or 0,
            "url": video.get("url") or "",
        }

    def _write_metadata(self, video: dict, *, has_manual_subs: bool):
        vid = extract_youtube_video_id(video.get("url") or "")
        if not vid:
            return
        write_metadata_entry(
            self.metadata_path,
            video_id=vid,
            platform="youtube",
            url=video.get("url") or "",
            duration_s=video.get("duration"),
            metainfo_extra=self._video_to_metainfo(video),
            has_manual_subs=has_manual_subs,
        )

    def make_hook(self):
        def hook(info: dict, watch_url: str, video: dict) -> str:
            title = video.get("title") or watch_url
            if not has_manual_subtitles(info, self.sub_langs):
                self.stats["no_manual_subs"] += 1
                lang_hint = (self.sub_langs or "").strip() or "all"
                logger.info(
                    "  无匹配手动字幕（要求 langs=%s），跳过: %s",
                    lang_hint,
                    title,
                )
                return "skip"

            vid = extract_youtube_video_id(watch_url)
            key = ("youtube", vid) if vid else None
            if key and key in self._archive_keys:
                self.stats["already_archived"] += 1
                logger.info("  已在下载记录中，跳过下载: %s", title)
                self._write_metadata(video, has_manual_subs=True)
                return "add"

            logger.info(
                "  发现匹配手动字幕（langs=%s），开始下载: %s",
                (self.sub_langs or "").strip() or "all",
                title,
            )
            ok = download_youtube_manual_subs(
                watch_url,
                out_dir=self.out_dir,
                archive_path=self.archive_path,
                cookies_path=self.cookies_path,
                audio=self.audio,
                runtime=self.runtime,
                log_path=self.log_path,
                sub_langs=self.sub_langs,
            )
            self._reload_archive()

            if not ok:
                self.stats["download_failed"] += 1
                logger.warning("  下载失败，下次重试: %s", title)
                return "retry"

            self.stats["downloaded"] += 1
            self._write_metadata(video, has_manual_subs=True)
            logger.info("  下载完成: %s", title)
            return "add"

        return hook


def _resolve_paths(config: dict, args) -> tuple[Path, Path, Path, Path, str]:
    sd_cfg = config.get("subtitled_download") or {}
    input_file = args.input or config.get("input_file", "input/channels.txt")
    input_stem = Path(input_file).stem

    out_root = Path(args.out or sd_cfg.get("output_dir") or "downloads")
    task_name = (args.task_name or sd_cfg.get("task_name") or input_stem).strip()
    out_dir = out_root / task_name

    archive = args.archive or sd_cfg.get("archive") or ""
    archive_path = Path(archive) if archive else out_dir / "downloaded.txt"

    metadata_path = out_dir / "metadata.jsonl"
    log_path = out_dir / "downloads.log.jsonl"

    cookies_cfg = config.get("cookies") or {}
    cookies_path = args.cookies_youtube or cookies_cfg.get("youtube") or ""

    return out_dir, archive_path, metadata_path, log_path, cookies_path


def main():
    parser = argparse.ArgumentParser(
        description="YouTube 手动字幕：搜索通过后即时下载（单阶段）",
    )
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--input", default="", help="输入文件（覆盖 config 中的 input_file）")
    parser.add_argument("--out", default="", help="下载根目录（默认 downloads）")
    parser.add_argument("--task-name", default="", help="任务子目录名（默认 input 文件名 stem）")
    parser.add_argument("--archive", default="", help="yt-dlp download-archive 路径")
    parser.add_argument("--cookies-youtube", default="", help="YouTube cookies.txt")
    parser.add_argument("--sub-langs", default="", help="字幕语种（逗号分隔；留空=all）")
    parser.add_argument(
        "--output-excel",
        default="",
        help="可选：将已下载视频元数据导出为 Excel（默认不写）",
    )
    parser.add_argument("--refresh-pending-urls", action="store_true", help="强制重新拉取视频 URL 列表")
    parser.add_argument("--audio-format", default="wav", help="音频格式（默认 wav）")
    parser.add_argument("--audio-sample-rate", type=int, default=16000)
    parser.add_argument("--audio-channels", type=int, default=1)
    parser.add_argument("--sleep-youtube", type=float, default=0, help="yt-dlp sleep-interval（默认 2.0）")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging()

    signal.signal(signal.SIGINT, _handle_sigint)

    input_file = args.input or config.get("input_file", "input/channels.txt")
    entries = parse_input_file(input_file)
    if not entries:
        logger.error("输入文件无有效条目: %s", input_file)
        return

    yt_entries = [e for e in entries if e["type"] in YOUTUBE_TYPES]
    skipped = len(entries) - len(yt_entries)
    if skipped:
        logger.info("已跳过 %d 条非 YouTube 条目（Bilibili 等后续支持）", skipped)
    if not yt_entries:
        logger.error("没有可处理的 YouTube 条目")
        return

    out_dir, archive_path, metadata_path, log_path, cookies_path = _resolve_paths(config, args)
    out_dir.mkdir(parents=True, exist_ok=True)

    sd_cfg = config.get("subtitled_download") or {}
    sub_langs = args.sub_langs or sd_cfg.get("sub_langs") or ""

    yt_defaults = PLATFORM_SLEEP_DEFAULTS["youtube"]
    sleep = float(args.sleep_youtube) if args.sleep_youtube > 0 else float(
        sd_cfg.get("sleep_interval") or yt_defaults[0]
    )
    max_sleep = sleep * 3

    audio = YtDlpAudioOptions(
        audio_format=args.audio_format,
        audio_sample_rate=int(args.audio_sample_rate),
        audio_channels=int(args.audio_channels),
    )
    runtime = YtDlpRuntimeOptions(sleep_interval=sleep, max_sleep_interval=max_sleep)

    cp_base = config.get("checkpoint", {}).get("checkpoint_dir", "checkpoints")
    input_stem = Path(input_file).stem
    cp_dir = str(Path(cp_base + "_subtitled") / input_stem)
    cp_mgr = CheckpointManager(cp_dir)
    save_interval = config.get("checkpoint", {}).get("save_interval", 50)

    cookies_cfg = config.get("cookies") or {}
    yt_crawler = YouTubeCrawler(
        config,
        cookies_path=cookies_path or cookies_cfg.get("youtube") or "",
    )

    pipeline = SubtitledPipeline(
        out_dir=out_dir,
        archive_path=archive_path,
        metadata_path=metadata_path,
        log_path=log_path,
        cookies_path=cookies_path,
        audio=audio,
        runtime=runtime,
        sub_langs=sub_langs,
    )

    logger.info("=" * 60)
    logger.info("YouTube 手动字幕 · 搜索+下载")
    logger.info("输入: %s | YouTube 条目: %d", input_file, len(yt_entries))
    logger.info("输出: %s", out_dir)
    logger.info("字幕语种要求: %s", (sub_langs or "").strip() or "all")
    logger.info("Checkpoint: %s", cp_dir)
    logger.info("=" * 60)

    all_videos = []
    hook = pipeline.make_hook()

    for i, entry in enumerate(yt_entries, 1):
        if _interrupted:
            logger.info("用户中断，跳过剩余任务")
            break

        _skip_current.clear()
        logger.info("[%d/%d] %s (%s)", i, len(yt_entries), entry["name"], entry["type"])

        if cp_mgr.is_done(entry):
            cached = cp_mgr.get_videos(entry)
            logger.info("  已完成，从 checkpoint 加载 %d 条", len(cached))
            all_videos.extend(cached)
            continue

        try:
            videos = yt_crawler.crawl(
                entry,
                cp_mgr,
                save_interval=save_interval,
                skip_event=_skip_current,
                refresh_pending_urls=args.refresh_pending_urls,
                post_metadata_hook=hook,
            )
            all_videos.extend(videos)
            logger.info(
                "  条目完成: %d 条已收录 | 本条目统计 no_sub=%d archived=%d dl=%d fail=%d",
                len(videos),
                pipeline.stats["no_manual_subs"],
                pipeline.stats["already_archived"],
                pipeline.stats["downloaded"],
                pipeline.stats["download_failed"],
            )
        except KeyboardInterrupt:
            logger.warning("中断，已保存 checkpoint")
            break
        except Exception:
            logger.exception("处理失败: %s", entry["name"])

    all_videos = deduplicate_videos(all_videos)
    logger.info(
        "全部完成: 收录 %d 条 | 无字幕跳过 %d | 已归档跳过 %d | 新下载 %d | 下载失败 %d",
        len(all_videos),
        pipeline.stats["no_manual_subs"],
        pipeline.stats["already_archived"],
        pipeline.stats["downloaded"],
        pipeline.stats["download_failed"],
    )
    logger.info("metadata → %s", metadata_path)
    logger.info("download-archive → %s", archive_path)

    excel_path = args.output_excel or sd_cfg.get("output_excel") or ""
    if excel_path:
        save_to_excel(all_videos, excel_path)
        logger.info("Excel → %s", excel_path)


if __name__ == "__main__":
    main()
