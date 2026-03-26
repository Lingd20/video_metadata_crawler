#!/usr/bin/env python3
"""Video Metadata Crawler — 主入口脚本

从 Bilibili / YouTube 爬取指定频道或播放列表的视频元数据，
保存到 Excel，支持断点续传和自动去重。

- Bilibili: 通过 B 站 API（BilibiliCrawler）
- YouTube:  通过系统 yt-dlp 命令行（YouTubeCrawler）
"""

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from bilibili_crawler import BilibiliCrawler
from youtube_crawler import YouTubeCrawler
from utils import (
    CheckpointManager,
    deduplicate_videos,
    load_config,
    parse_input_file,
    save_to_excel,
    setup_logging,
)

logger = logging.getLogger(__name__)

_interrupted = False


def _safe_float(val, default=0.0):
    try:
        if val is None or val == "":
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _handle_sigint(signum, frame):
    global _interrupted
    if _interrupted:
        logger.warning("强制退出")
        sys.exit(1)
    _interrupted = True
    logger.warning("收到中断信号，当前任务完成后将安全退出...")


def main():
    parser = argparse.ArgumentParser(description="Video Metadata Crawler")
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--input", default="", help="输入文件路径（覆盖配置文件中的 input_file）")
    parser.add_argument("--output", default="", help="输出文件路径（覆盖配置文件中的输出路径）")
    parser.add_argument("--export-only", action="store_true", help="跳过爬取，仅从 checkpoint 导出 Excel")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    os.chdir(project_root)

    config = load_config(args.config)
    setup_logging()

    signal.signal(signal.SIGINT, _handle_sigint)

    input_file = args.input or config.get("input_file", "input/channels.txt")
    entries = parse_input_file(input_file)
    if not entries:
        logger.error("No valid entries in %s", input_file)
        return

    cp_dir = config.get("checkpoint", {}).get("checkpoint_dir", "checkpoints")
    save_interval = config.get("checkpoint", {}).get("save_interval", 50)
    cp_mgr = CheckpointManager(cp_dir)

    cookies_cfg = config.get("cookies", {})
    bili_crawler = BilibiliCrawler(config, cookies_path=cookies_cfg.get("bilibili", ""))
    yt_crawler = YouTubeCrawler(config, cookies_path=cookies_cfg.get("youtube", ""))

    out_cfg = config.get("output", {})
    output_dir = Path(args.output).parent if args.output else Path(out_cfg.get("output_dir", "results"))
    output_file = Path(args.output) if args.output else output_dir / out_cfg.get("filename", "video_metadata.xlsx")
    output_dir.mkdir(parents=True, exist_ok=True)

    all_videos = []

    if args.export_only:
        logger.info("导出模式：跳过爬取，从 checkpoint 加载数据")
        for i, entry in enumerate(entries, 1):
            cached = cp_mgr.get_videos(entry)
            if cached:
                logger.info("[%d/%d] %s — 加载 %d 条视频", i, len(entries), entry["name"], len(cached))
                all_videos.extend(cached)
            else:
                logger.warning("[%d/%d] %s — 无 checkpoint 数据", i, len(entries), entry["name"])
    else:
        for i, entry in enumerate(entries, 1):
            if _interrupted:
                logger.info("用户中断，跳过剩余任务")
                break

            logger.info("=" * 60)
            logger.info("[%d/%d] %s (%s) id=%s", i, len(entries), entry["name"], entry["type"], entry["id"])
            logger.info("=" * 60)

            if cp_mgr.is_done(entry):
                cached = cp_mgr.get_videos(entry)
                logger.info("已完成，从缓存加载 %d 条视频", len(cached))
                all_videos.extend(cached)
                continue

            crawler = bili_crawler if entry["type"].startswith("bilibili") else yt_crawler

            try:
                videos = crawler.crawl(entry, cp_mgr, save_interval)
                all_videos.extend(videos)
                logger.info("完成：%d 条视频", len(videos))
            except KeyboardInterrupt:
                logger.warning("中断，已保存 checkpoint")
                break
            except Exception:
                logger.exception("爬取失败：%s", entry["name"])

    all_videos = deduplicate_videos(all_videos)
    save_to_excel(all_videos, str(output_file))

    total_hours = sum(_safe_float(v.get("duration", 0), 0.0) for v in all_videos) / 3600.0
    sources_with_videos = {
        (v.get("platform", ""), v.get("source_type", ""), v.get("source_name", ""))
        for v in all_videos
        if v.get("source_name")
    }

    logger.info("=" * 60)
    logger.info("全部完成！共 %d 个唯一视频 → %s", len(all_videos), output_file)
    logger.info("汇总统计")
    logger.info("  总时长(小时)  %.3f", total_hours)
    logger.info("  channel数     %d", len(sources_with_videos))
    logger.info("  视频数        %d", len(all_videos))
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
