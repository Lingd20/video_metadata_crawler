"""YouTube video metadata crawler (via yt-dlp subprocess).

Supports:
  - youtube_channel  : 爬取频道全部公开视频
  - youtube_playlist : 爬取播放列表全部视频
"""

import json
import logging
import os
import subprocess
import sys
import time

from utils import (
    CheckpointManager,
    resolve_youtube_channel,
    resolve_youtube_playlist,
)

logger = logging.getLogger(__name__)


class YoutubeCrawler:

    def __init__(self, config: dict, cookies_path: str = ""):
        yc = config.get("youtube", {})
        self.sleep = yc.get("sleep_interval", 3)
        self.error_sleep = yc.get("error_sleep_interval", 60)
        self.cookies_path = cookies_path

    # -- public interface --

    def crawl(self, entry: dict, cp_mgr: CheckpointManager, save_interval: int = 50):
        etype = entry["type"]
        if etype == "youtube_channel":
            url = resolve_youtube_channel(entry["id"])
        elif etype == "youtube_playlist":
            url = resolve_youtube_playlist(entry["id"])
        else:
            logger.error("Unsupported youtube type: %s", etype)
            return []

        source_name = entry["name"]
        cp = cp_mgr.load(entry)
        videos = cp.get("videos", []) if cp else []
        existing_urls = {v["url"] for v in videos}
        start_index = len(videos) + 1

        logger.info("[%s] %s — url=%s, resuming from index %d (%d cached)",
                     etype, source_name, url, start_index, len(videos))

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
                env={**os.environ, **{k: "" for k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]}},
            )

            for line in iter(proc.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue
                try:
                    info = json.loads(line)
                except json.JSONDecodeError:
                    continue

                video = self._parse(info, source_name, entry["type"])
                if video is None:
                    continue
                if video["url"] in existing_urls:
                    continue

                videos.append(video)
                existing_urls.add(video["url"])
                new_count += 1

                if new_count % 20 == 0:
                    logger.info("  Fetched %d new (total %d)", new_count, len(videos))

                if new_count >= save_interval:
                    cp_mgr.save(entry, {"videos": videos, "done": False})
                    new_count = 0

            proc.wait()
            if proc.returncode not in (0, None):
                stderr = proc.stderr.read()
                if stderr:
                    logger.warning("yt-dlp stderr: %s", stderr[:500])

        except KeyboardInterrupt:
            logger.warning("Interrupted — saving checkpoint")
            cp_mgr.save(entry, {"videos": videos, "done": False})
            raise

        cp_mgr.save(entry, {"videos": videos, "done": True})
        logger.info("[%s] %s done — %d videos", entry["type"], source_name, len(videos))
        return videos

    # -- internals --

    def _build_cmd(self, url, start_index=1):
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--dump-json", "--no-download",
            "--ignore-errors", "--no-check-certificates",
            "--extractor-retries", "3",
            "--proxy", "",
            "--no-update",
        ]
        if self.cookies_path:
            cmd.extend(["--cookies", self.cookies_path])
        if start_index > 1:
            cmd.extend(["--playlist-start", str(start_index)])
        if self.sleep > 0:
            cmd.extend(["--sleep-requests", str(self.sleep)])
        cmd.append(url)
        return cmd

    @staticmethod
    def _parse(info: dict, source_name: str, source_type: str):
        """Extract relevant fields from a yt-dlp JSON entry."""
        webpage_url = info.get("webpage_url") or info.get("url")
        if not webpage_url:
            return None

        categories = info.get("categories") or []
        upload_date = info.get("upload_date", "")
        if upload_date and len(upload_date) == 8:
            upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

        return {
            "source_name": source_name,
            "source_type": source_type,
            "platform": "youtube",
            "title": info.get("title", ""),
            "url": webpage_url,
            "duration": info.get("duration", 0) or 0,
            "category": ", ".join(categories),
            "publish_time": upload_date,
            "view_count": info.get("view_count", 0) or 0,
            "uploader": info.get("channel") or info.get("uploader", ""),
        }
