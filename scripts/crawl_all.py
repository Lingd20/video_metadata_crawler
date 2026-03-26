#!/usr/bin/env python3
"""Video Metadata Crawler — 主入口脚本

从 Bilibili / YouTube 爬取指定频道或播放列表的视频元数据，
保存到 Excel，支持断点续传和自动去重。
"""

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from utils import (
    CheckpointManager,
    deduplicate_videos,
    parse_ytdlp_video,
    resolve_bilibili_series,
    resolve_bilibili_up_mid,
    resolve_youtube_channel,
    resolve_youtube_playlist,
    load_config,
    parse_input_file,
    save_to_excel,
    setup_logging,
    ytdlp_flat_urls,
    ytdlp_iter_json,
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
    yt_cookies = cookies_cfg.get("youtube", "")
    bili_cookies = cookies_cfg.get("bilibili", "")

    yc = config.get("youtube", {})
    youtube_sleep = yc.get("sleep_interval", 3)
    bc = config.get("bilibili", {})
    bilibili_sleep = bc.get("sleep_interval", 2)

    out_cfg = config.get("output", {})
    output_dir = Path(args.output).parent if args.output else Path(out_cfg.get("output_dir", "results"))
    output_file = Path(args.output) if args.output else output_dir / out_cfg.get("filename", "video_metadata.xlsx")
    output_dir.mkdir(parents=True, exist_ok=True)

    all_videos = []

    for i, entry in enumerate(entries, 1):
        if _interrupted:
            logger.info("用户中断，跳过剩余任务")
            break

        logger.info("=" * 60)
        logger.info("[%d/%d] %s (%s) id=%s", i, len(entries), entry["name"], entry["type"], entry["id"])
        logger.info("=" * 60)

        cp = cp_mgr.load(entry) or {}
        if cp_mgr.is_done(entry):
            cached = cp.get("videos", [])
            logger.info("已完成，从缓存加载 %d 条视频", len(cached))
            all_videos.extend(cached)
            continue

        source_name = entry["name"]
        source_type = entry["type"]

        # -----------------------------
        # Phase A: fetch URL list
        # -----------------------------
        urls = cp.get("urls", [])
        if not cp.get("urls_done", False) or not urls:
            try:
                if source_type.startswith("youtube"):
                    base_url = entry["id"]
                    platform = "youtube"
                    if source_type == "youtube_channel":
                        base_url = resolve_youtube_channel(entry["id"])
                    else:
                        base_url = resolve_youtube_playlist(entry["id"])
                    urls = ytdlp_flat_urls(base_url, platform=platform, cookies_path=yt_cookies, sleep_requests=youtube_sleep)
                else:
                    platform = "bilibili"
                    if source_type == "bilibili_up":
                        mid = resolve_bilibili_up_mid(entry["id"])
                        base_url = f"https://space.bilibili.com/{mid}/upload/video"
                        urls = ytdlp_flat_urls(base_url, platform=platform, cookies_path=bili_cookies, sleep_requests=bilibili_sleep)
                    else:
                        kind, mid, sid = resolve_bilibili_series(entry["id"])
                        if kind == "list":
                            base_url = entry["id"] if str(entry["id"]).startswith("http") else f"https://space.bilibili.com/{mid}/lists/{sid}"
                        else:
                            base_url = f"https://space.bilibili.com/{mid}/channel/collectiondetail?sid={sid}"
                        urls = ytdlp_flat_urls(base_url, platform=platform, cookies_path=bili_cookies, sleep_requests=bilibili_sleep)

                urls = list(dict.fromkeys(urls))  # keep order, dedupe
                cp_mgr.save(entry, {**cp, "urls": urls, "urls_done": True, "done": False})
                logger.info("URL清单获取完成：%d 条", len(urls))
            except Exception:
                logger.exception("获取URL清单失败：%s", source_name)
                cp_mgr.save(entry, {**cp, "urls": urls, "urls_done": False, "done": False})
                continue
        else:
            logger.info("URL清单已存在：%d 条", len(urls))

        # -----------------------------
        # Phase B: fetch metadata
        # -----------------------------
        videos = cp.get("videos", [])
        existing_urls = {v.get("url") for v in videos if v.get("url")}
        remaining = [u for u in urls if u not in existing_urls]

        if not remaining:
            cp_mgr.save(entry, {**cp, "urls": urls, "urls_done": True, "videos": videos, "done": True})
            all_videos.extend(videos)
            logger.info("metadata 已齐全：%d 条", len(videos))
            continue

        logger.info("开始拉取 metadata：剩余 %d / %d", len(remaining), len(urls))

        cookies_path = yt_cookies if source_type.startswith("youtube") else bili_cookies
        sleep_requests = youtube_sleep if source_type.startswith("youtube") else bilibili_sleep
        platform = "youtube" if source_type.startswith("youtube") else "bilibili"

        new_count = 0
        try:
            for u in remaining:
                if _interrupted:
                    break
                # B站存在“一个BV包含很多子分P/分集”的情况，yt-dlp 可能会把单视频当成 playlist 展开，导致卡住。
                # 这里强制按“单视频”取 metadata，避免展开子条目。
                no_playlist = platform == "bilibili"
                playlist_items = "1" if platform == "bilibili" else ""
                for info in ytdlp_iter_json(
                    u,
                    cookies_path=cookies_path,
                    flat=False,
                    sleep_requests=sleep_requests,
                    no_playlist=no_playlist,
                    playlist_items=playlist_items,
                ):
                    v = parse_ytdlp_video(info, platform=platform, source_name=source_name, source_type=source_type)
                    if not v or not v.get("url") or v["url"] in existing_urls:
                        continue
                    videos.append(v)
                    existing_urls.add(v["url"])
                    new_count += 1
                    if new_count >= save_interval:
                        cp_mgr.save(entry, {**cp, "urls": urls, "urls_done": True, "videos": videos, "done": False})
                        new_count = 0
            if _interrupted:
                cp_mgr.save(entry, {**cp, "urls": urls, "urls_done": True, "videos": videos, "done": False})
                logger.warning("中断，已保存 checkpoint")
                break
        except KeyboardInterrupt:
            cp_mgr.save(entry, {**cp, "urls": urls, "urls_done": True, "videos": videos, "done": False})
            logger.warning("中断，已保存 checkpoint")
            break
        except Exception:
            logger.exception("拉取 metadata 失败：%s", source_name)
            cp_mgr.save(entry, {**cp, "urls": urls, "urls_done": True, "videos": videos, "done": False})
            continue

        prev_count = len(cp.get("videos", [])) if isinstance(cp.get("videos", []), list) else 0
        cp_mgr.save(entry, {**cp, "urls": urls, "urls_done": True, "videos": videos, "done": True})
        logger.info("完成：新增 %d 条，累计 %d 条", len(videos) - prev_count, len(videos))
        all_videos.extend(videos)

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
