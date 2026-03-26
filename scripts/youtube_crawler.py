"""YouTube video metadata crawler (via system yt-dlp command).

Supports:
  - youtube_channel  : 爬取频道全部公开视频
  - youtube_playlist : 爬取播放列表全部视频

使用系统 yt-dlp 命令行，自动继承终端代理设置。
"""

import json
import logging
import os
import re
import subprocess
from datetime import datetime

from utils import CheckpointManager

logger = logging.getLogger(__name__)


def _resolve_youtube_channel(raw_id: str) -> str:
    if raw_id.startswith("http"):
        url = raw_id.rstrip("/")
        if not url.endswith("/videos"):
            url += "/videos"
        return url
    if raw_id.startswith("@"):
        return f"https://www.youtube.com/{raw_id}/videos"
    if raw_id.startswith("UC"):
        return f"https://www.youtube.com/channel/{raw_id}/videos"
    return f"https://www.youtube.com/{raw_id}/videos"


def _resolve_youtube_playlist(raw_id: str) -> str:
    if raw_id.startswith("http"):
        return raw_id
    return f"https://www.youtube.com/playlist?list={raw_id}"


class YouTubeCrawler:

    def __init__(self, config: dict, cookies_path: str = ""):
        yc = config.get("youtube", {})
        self.sleep = yc.get("sleep_interval", 3)
        self.cookies_path = cookies_path

    def crawl(self, entry: dict, cp_mgr: CheckpointManager, save_interval: int = 50):
        """爬取一个 youtube_channel 或 youtube_playlist，返回视频字典列表。"""
        etype = entry["type"]
        if etype == "youtube_channel":
            url = _resolve_youtube_channel(entry["id"])
        elif etype == "youtube_playlist":
            url = _resolve_youtube_playlist(entry["id"])
        else:
            logger.error("Unsupported youtube type: %s", etype)
            return []

        source_name = entry["name"]
        cp = cp_mgr.load(entry) or {}
        videos = cp.get("videos", [])
        existing_urls = {v["url"] for v in videos}
        start_index = len(videos) + 1

        logger.info(
            "[%s] %s — url=%s, resuming from index %d (%d cached)",
            etype, source_name, url, start_index, len(videos),
        )

        cmd = self._build_cmd(url, start_index)
        new_count = 0

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

            for line in iter(proc.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue
                try:
                    info = json.loads(line)
                except json.JSONDecodeError:
                    continue

                video = self._parse(info, source_name, etype)
                if video is None or video["url"] in existing_urls:
                    continue

                videos.append(video)
                existing_urls.add(video["url"])
                new_count += 1

                title = video.get("title", "")
                if len(title) > 60:
                    title = title[:57] + "..."
                logger.info("  [%d] %s", len(videos), title)

                if new_count >= save_interval:
                    cp_mgr.save(entry, {"videos": videos, "done": False})
                    new_count = 0

            proc.wait()
            if proc.returncode not in (0, None):
                stderr = proc.stderr.read() if proc.stderr else ""
                if stderr:
                    logger.warning("yt-dlp stderr: %s", stderr[:500])
                cp_mgr.save(entry, {"videos": videos, "done": False})
                logger.warning("[%s] %s — yt-dlp non-zero exit, checkpoint kept as not-done", etype, source_name)
                return videos

        except KeyboardInterrupt:
            logger.warning("Interrupted — saving checkpoint")
            cp_mgr.save(entry, {"videos": videos, "done": False})
            raise

        cp_mgr.save(entry, {"videos": videos, "done": True})
        logger.info("[%s] %s done — %d videos", etype, source_name, len(videos))
        return videos

    def _build_cmd(self, url: str, start_index: int = 1) -> list[str]:
        cmd = [
            "yt-dlp",
            "--dump-json",
            "--no-download",
            "--ignore-errors",
            "--extractor-retries", "3",
            "--no-update",
            "--js-runtimes", "node",
            "--no-cache-dir",
            "--remote-components", "ejs:github",
        ]
        if self.cookies_path:
            cmd.extend(["--cookies", self.cookies_path])
        if start_index > 1:
            cmd.extend(["--playlist-start", str(start_index)])
        if self.sleep and self.sleep > 0:
            cmd.extend(["--sleep-requests", str(self.sleep)])
        cmd.append(url)
        return cmd

    @staticmethod
    def _parse(info: dict, source_name: str, source_type: str) -> dict | None:
        webpage_url = info.get("webpage_url") or info.get("url")
        if not webpage_url:
            return None

        publish_time = ""
        ts = info.get("timestamp")
        if ts:
            try:
                publish_time = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        if not publish_time:
            ud = info.get("upload_date", "")
            if ud and len(ud) == 8:
                publish_time = f"{ud[:4]}-{ud[4:6]}-{ud[6:]}"

        categories = info.get("categories") or []
        category = ", ".join(categories) if isinstance(categories, list) else str(categories or "")
        if not category:
            category = info.get("category") or ""

        uploader = info.get("channel") or info.get("uploader") or ""

        channel_url = info.get("channel_url") or ""
        uploader_id = info.get("uploader_id") or ""
        handle = ""
        if channel_url:
            m = re.search(r"youtube\.com/@([^/?#]+)", channel_url, re.I)
            if m:
                handle = "@" + m.group(1)
        if not handle and uploader_id:
            if uploader_id.startswith("@"):
                handle = uploader_id
            elif not (uploader_id.startswith("UC") and len(uploader_id) == 24):
                handle = "@" + uploader_id.lstrip("@")

        if handle and uploader and uploader != handle and uploader != handle.lstrip("@"):
            display_name = f"{handle} {uploader}"
        else:
            display_name = handle or uploader

        return {
            "source_name": source_name,
            "source_type": source_type,
            "platform": "youtube",
            "title": info.get("title", ""),
            "url": webpage_url,
            "duration": info.get("duration", 0) or 0,
            "category": category,
            "publish_time": publish_time,
            "view_count": info.get("view_count", 0) or 0,
            "uploader": display_name,
        }
