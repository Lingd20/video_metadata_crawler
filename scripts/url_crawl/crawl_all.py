#!/usr/bin/env python3
"""Video Metadata Crawler — 主入口脚本（已迁移到 scripts/url_crawl/）。

从 Bilibili / YouTube 爬取指定频道或播放列表的视频元数据，
保存到 Excel，支持断点续传和自动去重。
"""

import argparse
import logging
import os
import signal
import sys
import threading
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
_skip_current_youtube = threading.Event()
_skip_current_bilibili = threading.Event()

_MUSIC_CATEGORY_NAMES = {
    "音乐",
    "原创音乐",
    "翻唱",
    "VOCALOID·UTAU",
    "演奏",
    "MV",
    "音乐现场",
    "音乐综合",
    "乐评盘点",
    "音乐教学",
    "电音",
}


def _should_export_to_excel(v: dict) -> bool:
    if (v.get("platform") or "") != "bilibili":
        return True
    cat = (v.get("category") or "").strip()
    if not cat:
        return True
    return cat not in _MUSIC_CATEGORY_NAMES


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
    _skip_current_youtube.set()
    _skip_current_bilibili.set()


def _handle_skip_youtube(signum, frame):
    _skip_current_youtube.set()
    _skip_current_bilibili.set()
    logger.warning("收到「跳过当前条目」信号（将尽快结束本条目的爬取）")


def _stdin_skip_youtube_watcher():
    while True:
        try:
            line = sys.stdin.readline()
        except Exception:
            break
        if not line:
            break
        t = line.strip().lower()
        if t in ("skip", "s", "next", "skipchannel", "n"):
            _skip_current_youtube.set()
            _skip_current_bilibili.set()
            logger.warning("已请求跳过当前条目（本条目将尽快结束）")


def _start_skip_youtube_hotkey(hotkey_spec: str) -> bool:
    spec = (hotkey_spec or "").strip()
    if not spec:
        return False
    try:
        from pynput import keyboard
    except ImportError:
        logger.warning("未安装 pynput，无法注册全局热键（pip install pynput）。仍可用 stdin / SIGUSR1 等。")
        return False

    def _on_hotkey():
        _skip_current_youtube.set()
        _skip_current_bilibili.set()
        logger.warning("热键触发：跳过当前条目（本条目将尽快结束）")

    try:
        hk = keyboard.GlobalHotKeys({spec: _on_hotkey})
        hk.start()
        logger.info("已注册「跳过当前 YouTube 条目」全局热键: %s", spec)
        return True
    except Exception as e:
        logger.warning("注册全局热键失败（可改用 --skip-youtube-hotkey \"\" 关闭）: %s", e)
        return False


def main():
    parser = argparse.ArgumentParser(description="Video Metadata Crawler")
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--input", default="", help="输入文件路径（覆盖配置文件中的 input_file）")
    parser.add_argument("--output", default="", help="输出文件路径（覆盖配置文件中的输出路径）")
    parser.add_argument("--export-only", action="store_true", help="跳过爬取，仅从 checkpoint 导出 Excel")
    parser.add_argument(
        "--skip-youtube-hotkey",
        default="<ctrl>+<shift>+y",
        metavar="SPEC",
        help="pynput 全局组合键（GlobalHotKeys 语法），触发时跳过当前 YouTube 条目。",
    )
    parser.add_argument("--no-skip-youtube-hotkey", action="store_true", help="禁用全局热键")
    parser.add_argument(
        "--refresh-pending-urls",
        action="store_true",
        help="强制重新拉取各条目的待处理视频 URL 列表",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent.parent
    os.chdir(project_root)

    config = load_config(args.config)
    setup_logging()

    signal.signal(signal.SIGINT, _handle_sigint)
    if hasattr(signal, "SIGUSR1"):
        try:
            signal.signal(signal.SIGUSR1, _handle_skip_youtube)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, _handle_skip_youtube)
        except (ValueError, OSError):
            pass

    input_file = args.input or config.get("input_file", "input/channels.txt")
    entries = parse_input_file(input_file)
    if not entries:
        logger.error("No valid entries in %s", input_file)
        return

    cp_base = config.get("checkpoint", {}).get("checkpoint_dir", "checkpoints")
    save_interval = config.get("checkpoint", {}).get("save_interval", 50)
    input_stem = Path(input_file).stem
    cp_dir = str(Path(cp_base) / input_stem)
    cp_mgr = CheckpointManager(cp_dir)
    logger.info("Checkpoint 目录: %s", cp_dir)

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
        hotkey = "" if args.no_skip_youtube_hotkey else (os.environ.get("CRAWLER_SKIP_YOUTUBE_HOTKEY") or args.skip_youtube_hotkey).strip()
        if hotkey:
            _start_skip_youtube_hotkey(hotkey)
        if sys.stdin.isatty():
            threading.Thread(target=_stdin_skip_youtube_watcher, daemon=True).start()

        for i, entry in enumerate(entries, 1):
            if _interrupted:
                logger.info("用户中断，跳过剩余任务")
                break

            _skip_current_youtube.clear()
            logger.info("=" * 60)
            logger.info("[%d/%d] %s (%s) id=%s", i, len(entries), entry["name"], entry["type"], entry["id"])
            logger.info("=" * 60)

            if cp_mgr.is_done(entry):
                cached = cp_mgr.get_videos(entry)
                logger.info("已完成，从缓存加载 %d 条视频", len(cached))
                all_videos.extend(cached)
                continue

            try:
                if entry["type"].startswith("bilibili"):
                    _skip_current_bilibili.clear()
                    videos = bili_crawler.crawl(
                        entry,
                        cp_mgr,
                        save_interval,
                        refresh_pending_urls=args.refresh_pending_urls,
                        skip_event=_skip_current_bilibili,
                    )
                else:
                    videos = yt_crawler.crawl(
                        entry,
                        cp_mgr,
                        save_interval,
                        skip_event=_skip_current_youtube,
                        refresh_pending_urls=args.refresh_pending_urls,
                    )
                all_videos.extend(videos)
                logger.info("完成：%d 条视频", len(videos))
            except KeyboardInterrupt:
                logger.warning("中断，已保存 checkpoint")
                break
            except Exception:
                logger.exception("爬取失败：%s", entry["name"])

    all_videos = deduplicate_videos(all_videos)
    export_videos = [v for v in all_videos if _should_export_to_excel(v)]
    save_to_excel(export_videos, str(output_file))

    total_hours = sum(_safe_float(v.get("duration", 0), 0.0) for v in all_videos) / 3600.0
    sources_with_videos = {
        (v.get("platform", ""), v.get("source_type", ""), v.get("source_name", ""))
        for v in all_videos
        if v.get("source_name")
    }

    logger.info("=" * 60)
    logger.info(
        "全部完成！共 %d 个唯一视频（Excel导出 %d 条） → %s",
        len(all_videos),
        len(export_videos),
        output_file,
    )
    logger.info("汇总统计")
    logger.info("  总时长(小时)  %.3f", total_hours)
    logger.info("  channel数     %d", len(sources_with_videos))
    logger.info("  视频数        %d", len(all_videos))
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

