"""YouTube video metadata crawler (via system yt-dlp command).

Supports:
  - youtube_channel  : 爬取频道全部公开视频
  - youtube_playlist : 爬取播放列表全部视频

流程（断点续传）：
  1) 首次对该条目（或列表为空且未完成）：用 yt-dlp --flat-playlist 拉取全部视频 URL，写入 youtube_pending_urls。
  2) 之后再次运行：默认不重新拉列表；按 youtube_pending_urls 顺序逐条拉元数据；已在 videos 或 youtube_failed_urls 中的 URL 跳过。
  3) 单条 URL 连续失败达到阈值则写入 youtube_failed_urls，避免重复失败
  4) 命令行 --refresh-pending-urls 可强制重新拉列表；不清理 youtube_failed_urls / 失败计数。
  5) skip_event 置位或到达 cutoff 时中止，done=False；列表中每条 URL 均已出现在 videos 或失败列表则 done=True。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from datetime import datetime

from utils import CheckpointManager

logger = logging.getLogger(__name__)

# 单条 URL 拉取元数据连续失败超过此次数则写入 failed_urls，不再重试
MAX_URL_FETCH_FAILURES = 3


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


def _flat_entry_to_watch_url(entry: dict) -> str | None:
    u = entry.get("url") or entry.get("webpage_url")
    if isinstance(u, str) and u.startswith("http"):
        return u.split("&")[0] if "youtube.com" in u else u
    if isinstance(u, str) and u.startswith("/watch"):
        return "https://www.youtube.com" + u.split("&")[0]
    vid = entry.get("id")
    if isinstance(vid, str) and len(vid) == 11 and re.match(r"^[\w-]{11}$", vid):
        return f"https://www.youtube.com/watch?v={vid}"
    return None


class YouTubeCrawler:

    def __init__(self, config: dict, cookies_path: str = ""):
        yc = config.get("youtube", {})
        self.sleep = yc.get("sleep_interval", 3)
        self.cookies_path = cookies_path
        cutoff = yc.get("cutoff_date", "")
        self.cutoff_dt: datetime | None = None
        if cutoff:
            try:
                self.cutoff_dt = datetime.strptime(str(cutoff), "%Y-%m-%d")
                logger.info("YouTube cutoff date: %s", self.cutoff_dt.strftime("%Y-%m-%d"))
            except ValueError:
                logger.warning("Invalid cutoff_date '%s', ignored (expected YYYY-MM-DD)", cutoff)

    def crawl(
        self,
        entry: dict,
        cp_mgr: CheckpointManager,
        save_interval: int = 50,
        skip_event: threading.Event | None = None,
        refresh_pending_urls: bool = False,
    ):
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
        videos: list = list(cp.get("videos", []))
        existing_urls = {v["url"] for v in videos}

        raw_pending = cp.get("youtube_pending_urls")
        pending_urls: list[str] | None = list(raw_pending) if isinstance(raw_pending, list) else None
        # 旧 checkpoint：已 done 且无列表字段时，用已有 videos 的 url 作为列表，避免每次重拉 flat-playlist
        if pending_urls is None and cp.get("done", False):
            pending_urls = [v["url"] for v in videos if v.get("url")]
        failed_urls: list[str] = list(cp.get("youtube_failed_urls") or [])
        failed_set = set(failed_urls)
        fail_counts: dict[str, int] = dict(cp.get("youtube_url_fail_counts") or {})

        def _should_fetch_list() -> bool:
            if refresh_pending_urls:
                return True
            if pending_urls is None:
                return True
            if len(pending_urls) == 0 and not cp.get("done", False):
                return True
            return False

        if _should_fetch_list():
            if refresh_pending_urls:
                logger.info("[%s] %s — 强制重新拉取视频列表（flat-playlist）…", etype, source_name)
            else:
                logger.info("[%s] %s — 拉取视频列表（flat-playlist）…", etype, source_name)
            pending_urls = self._fetch_flat_playlist_urls(url)
            if not pending_urls:
                logger.warning("[%s] %s — 未获取到任何视频 URL", etype, source_name)
                cp_mgr.save(
                    entry,
                    {
                        "videos": videos,
                        "youtube_pending_urls": [],
                        "youtube_failed_urls": failed_urls,
                        "youtube_url_fail_counts": fail_counts,
                        "done": True,
                    },
                )
                return videos

            cp_mgr.save(
                entry,
                {
                    "videos": videos,
                    "youtube_pending_urls": pending_urls,
                    "youtube_failed_urls": failed_urls,
                    "youtube_url_fail_counts": fail_counts,
                    "done": False,
                },
            )
            already = sum(1 for u in pending_urls if u in existing_urls)
            logger.info(
                "[%s] %s — 列表已缓存 %d 条 URL（videos 中已覆盖 %d 条，将按列表顺序补全）",
                etype,
                source_name,
                len(pending_urls),
                already,
            )
        else:
            pending_urls = pending_urls or []
            already = sum(1 for u in pending_urls if u in existing_urls)
            logger.info(
                "[%s] %s — 使用 checkpoint 中列表 %d 条，videos 已匹配其中 %d 条，失败放弃 %d 条",
                etype,
                source_name,
                len(pending_urls),
                already,
                len(failed_set),
            )

        new_count = 0
        skipped_by_user = False
        cutoff_reached = False

        def _save_partial():
            cp_mgr.save(
                entry,
                {
                    "videos": videos,
                    "youtube_pending_urls": pending_urls,
                    "youtube_failed_urls": failed_urls,
                    "youtube_url_fail_counts": fail_counts,
                    "done": False,
                },
            )

        try:
            for pos, watch_url in enumerate(pending_urls, start=1):
                if skip_event is not None and skip_event.is_set():
                    logger.warning("[%s] %s — 收到跳过信号，已保存进度", etype, source_name)
                    skipped_by_user = True
                    break

                if watch_url in existing_urls:
                    continue
                if watch_url in failed_set:
                    continue

                info = self._fetch_single_video_json(watch_url)
                if info is None:
                    fail_counts[watch_url] = fail_counts.get(watch_url, 0) + 1
                    c = fail_counts[watch_url]
                    if c >= MAX_URL_FETCH_FAILURES:
                        if watch_url not in failed_set:
                            failed_urls.append(watch_url)
                            failed_set.add(watch_url)
                        logger.warning(
                            "[%s] 连续失败 %d 次，已加入 youtube_failed_urls: %s",
                            etype,
                            c,
                            watch_url,
                        )
                    else:
                        logger.warning("[%s] 拉取失败 (%d/%d): %s", etype, c, MAX_URL_FETCH_FAILURES, watch_url)
                    _save_partial()
                    continue

                fail_counts.pop(watch_url, None)

                video = self._parse(info, source_name, etype)
                if video is None:
                    continue
                canon = video["url"]
                if canon in existing_urls:
                    existing_urls.add(watch_url)
                    continue

                videos.append(video)
                existing_urls.add(canon)
                existing_urls.add(watch_url)
                new_count += 1

                title = video.get("title", "")
                if len(title) > 60:
                    title = title[:57] + "..."
                logger.info("  [%d/%d] %s", pos, len(pending_urls), title)

                if new_count >= save_interval:
                    _save_partial()
                    new_count = 0

                if self.cutoff_dt and video.get("publish_time"):
                    try:
                        pt = datetime.strptime(video["publish_time"][:10], "%Y-%m-%d")
                        if pt < self.cutoff_dt:
                            logger.info(
                                "Reached cutoff date %s (video: %s), stopping",
                                self.cutoff_dt.strftime("%Y-%m-%d"),
                                video["publish_time"][:10],
                            )
                            cutoff_reached = True
                            break
                    except ValueError:
                        pass

                if self.sleep and self.sleep > 0:
                    time.sleep(float(self.sleep))
        except KeyboardInterrupt:
            logger.warning("[%s] %s — KeyboardInterrupt，已保存进度", etype, source_name)
            _save_partial()
            raise

        def _url_resolved(u: str) -> bool:
            return u in existing_urls or u in failed_set

        all_list_in_videos = len(pending_urls) == 0 or all(_url_resolved(u) for u in pending_urls)
        done = (
            (not skipped_by_user)
            and (not cutoff_reached)
            and all_list_in_videos
        )
        cp_mgr.save(
            entry,
            {
                "videos": videos,
                "youtube_pending_urls": pending_urls,
                "youtube_failed_urls": failed_urls,
                "youtube_url_fail_counts": fail_counts,
                "done": done,
            },
        )
        if done:
            logger.info("[%s] %s done — %d videos", etype, source_name, len(videos))
        elif skipped_by_user:
            logger.info("[%s] %s 已暂停 — %d videos（下次将续跑）", etype, source_name, len(videos))
        return videos

    def _fetch_flat_playlist_urls(self, list_url: str) -> list[str]:
        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "-j",
            "--ignore-errors",
            "--js-runtimes",
            "node",
            "--remote-components",
            "ejs:github",
            "--extractor-args",
            "youtube:player_client=android",
            "--extractor-args",
            "youtube:skip=live,members_only",
        ]
        if self.cookies_path:
            cmd.extend(["--cookies", self.cookies_path])
        cmd.append(list_url)

        urls: list[str] = []
        stderr_lines: list[str] = []
        proc: subprocess.Popen | None = None

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            def _read_err():
                if proc.stderr:
                    for line in proc.stderr:
                        stderr_lines.append(line)

            t = threading.Thread(target=_read_err, daemon=True)
            t.start()

            if proc.stdout:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    wu = _flat_entry_to_watch_url(entry)
                    if wu:
                        urls.append(wu)

            proc.wait(timeout=7200)
            t.join(timeout=5)
        except Exception as e:
            logger.error("flat-playlist 失败: %s", e)
            if stderr_lines:
                logger.warning("yt-dlp stderr: %s", "".join(stderr_lines[-50:]))
            return []

        if proc is not None and proc.returncode != 0 and not urls:
            logger.warning("yt-dlp flat-playlist exit=%s", proc.returncode)
            if stderr_lines:
                logger.warning("yt-dlp stderr: %s", "".join(stderr_lines[-80:]))

        # 去重且保序
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _fetch_single_video_json(self, watch_url: str) -> dict | None:
        cmd = [
            "yt-dlp",
            "--match-filter",
            "availability = public",
            "-j",
            "--ignore-errors",
            "--js-runtimes",
            "node",
            "--remote-components",
            "ejs:github",
            "--extractor-args",
            "youtube:player_client=android",
            "--extractor-args",
            "youtube:skip=live,members_only",
        ]
        if self.cookies_path:
            cmd.extend(["--cookies", self.cookies_path])
        cmd.append(watch_url)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            logger.warning("单视频元数据超时: %s", watch_url)
            return None
        if r.returncode != 0 and not r.stdout.strip():
            if r.stderr:
                logger.debug("yt-dlp: %s", r.stderr[-500:])
            return None
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

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
