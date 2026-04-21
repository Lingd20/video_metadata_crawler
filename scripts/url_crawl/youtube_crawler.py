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

from datetime import datetime
from urllib.parse import parse_qs, urlparse

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


def _video_id_from_watch_url(url: str) -> str | None:
    """Extract 11-char video id from common YouTube watch URLs."""
    u = (url or "").strip()
    if not u:
        return None
    # fast path: v=...
    try:
        parsed = urlparse(u)
        if parsed.query:
            qs = parse_qs(parsed.query)
            v = qs.get("v", [None])[0]
            if isinstance(v, str) and len(v) == 11 and re.match(r"^[\w-]{11}$", v):
                return v
    except Exception:
        pass
    m = re.search(r"(?:youtu\.be/|/watch\?v=|/shorts/)([\w-]{11})(?:[&/?#]|$)", u)
    if m:
        return m.group(1)
    return None


class YouTubeCrawler:

    def __init__(self, config: dict, cookies_path: str = ""):
        yc = config.get("youtube", {})
        self.sleep = yc.get("sleep_interval", 3)
        self.cookies_path = cookies_path
        self.keyword_filters: dict = config.get("keyword_filters", {}) or {}
        # youtube_channel 超大时采用分段拉取（每段 1000 条）
        self.channel_batch_size = yc.get("channel_batch_size", 1000)
        self.metadata_batch_size = int(yc.get("metadata_batch_size", 50) or 50)
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

        filtered_urls: list[str] = list(cp.get("youtube_filtered_urls") or [])
        filtered_set = set(filtered_urls)

        keyword_filter_name = entry.get("keyword_filter")
        profile = None
        if keyword_filter_name:
            profile = self.keyword_filters.get(keyword_filter_name)
            if not profile:
                logger.warning(
                    "[youtube] %s — keyword_filter '%s' 未在 config.keyword_filters 中找到，跳过过滤",
                    source_name,
                    keyword_filter_name,
                    keyword_filter_name,
                )

        positive_keywords = []
        negative_keywords = []
        if profile:
            positive_keywords = (
                profile.get("keywords")
                or profile.get("positive_keywords")
                or profile.get("include")
                or []
            )
            negative_keywords = (
                profile.get("negative_keywords")
                or profile.get("negative")
                or profile.get("exclude")
                or []
            )
        positive_keywords = [str(k).strip().lower() for k in positive_keywords if str(k).strip()]
        negative_keywords = [str(k).strip().lower() for k in negative_keywords if str(k).strip()]
        apply_keyword_filter = bool(profile and (positive_keywords or negative_keywords))

        def _accept_by_keywords(text_title: str, text_desc: str) -> bool:
            if not positive_keywords and not negative_keywords:
                return True
            content = (text_title or "") + "\n" + (text_desc or "")
            content_l = content.lower()
            if negative_keywords and any(neg in content_l for neg in negative_keywords):
                return False
            if positive_keywords:
                return any(pos in content_l for pos in positive_keywords)
            # 只有负向关键词时：只要没命中负向即可通过
            return True

        def _extract_filter_title_desc(info: dict) -> tuple[str, str]:
            """Extract (title, description) text for keyword filtering."""
            title_for_filter = info.get("title") or info.get("fulltitle") or ""
            desc_for_filter = info.get("description") or info.get("full_description") or ""
            return title_for_filter, desc_for_filter

        # youtube_channel：特别大的频道采用分段拉取 pending list + 获取 metadata
        youtube_pending_urls_done = cp.get("youtube_pending_urls_done", None)
        youtube_list_cursor = int(cp.get("youtube_list_cursor") or 0)

        use_segmented_channel = (
            etype == "youtube_channel"
            and (
                refresh_pending_urls
                or youtube_pending_urls_done is False
                or pending_urls is None
                or (isinstance(pending_urls, list) and len(pending_urls) == 0 and not cp.get("done", False))
            )
        )

        if use_segmented_channel:
            if refresh_pending_urls:
                logger.info("[%s] %s — 启用分段抓取（刷新 pending urls 列表）", etype, source_name)
                youtube_list_cursor = 0
                youtube_pending_urls_done = False
                pending_urls = None
            else:
                # 若 checkpoint 中没有字段，默认认为仍在分段进行中
                youtube_pending_urls_done = bool(youtube_pending_urls_done) if youtube_pending_urls_done is not None else False

            def _url_resolved(u: str) -> bool:
                return u in existing_urls or u in failed_set or u in filtered_set

            def _save_partial_segment():
                cp_mgr.save(
                    entry,
                    {
                        "videos": videos,
                        "youtube_pending_urls": pending_urls or [],
                        "youtube_failed_urls": failed_urls,
                        "youtube_url_fail_counts": fail_counts,
                        "youtube_filtered_urls": filtered_urls,
                        "youtube_pending_urls_done": youtube_pending_urls_done,
                        "youtube_list_cursor": youtube_list_cursor,
                        "done": False,
                    },
                )

            skipped_by_user = False
            cutoff_reached = False
            new_count = 0

            try:
                while True:
                    # 需要拉取下一段 pending_urls
                    if pending_urls is None or len(pending_urls) == 0:
                        if youtube_pending_urls_done:
                            break

                        start = youtube_list_cursor
                        end = youtube_list_cursor + int(self.channel_batch_size) - 1
                        pending_urls = self._fetch_flat_playlist_urls_range(url, start, end)

                        if pending_urls is None:
                            logger.warning(
                                "[%s] %s — 分段拉取视频列表失败（疑似网络/连接问题），将保持 done=False",
                                etype,
                                source_name,
                            )
                            _save_partial_segment()
                            return videos
                        if not pending_urls:
                            youtube_pending_urls_done = True
                            cp_mgr.save(
                                entry,
                                {
                                    "videos": videos,
                                    "youtube_pending_urls": [],
                                    "youtube_failed_urls": failed_urls,
                                    "youtube_url_fail_counts": fail_counts,
                                    "youtube_filtered_urls": filtered_urls,
                                    "youtube_pending_urls_done": True,
                                    "youtube_list_cursor": youtube_list_cursor,
                                    "done": (not skipped_by_user and True),
                                },
                            )
                            break

                        # 拉到一段 pending_urls 后，开始抓这一段的 metadata
                        cp_mgr.save(
                            entry,
                            {
                                "videos": videos,
                                "youtube_pending_urls": pending_urls,
                                "youtube_failed_urls": failed_urls,
                                "youtube_url_fail_counts": fail_counts,
                                "youtube_filtered_urls": filtered_urls,
                                "youtube_pending_urls_done": False,
                                "youtube_list_cursor": youtube_list_cursor,
                                "done": False,
                            },
                        )

                    seg_batch_size = self.metadata_batch_size if self.metadata_batch_size > 0 else 50
                    seg_batch: list[str] = [
                        u for u in pending_urls
                        if u not in existing_urls and u not in failed_set and u not in filtered_set
                    ]

                    seg_pos = 0
                    while seg_pos < len(seg_batch) and not skipped_by_user and not cutoff_reached:
                        chunk = seg_batch[seg_pos:seg_pos + seg_batch_size]
                        seg_pos += len(chunk)
                        seg_base = seg_pos - len(chunk)
                        if not chunk:
                            break

                        seg_id_to_url: dict[str, str] = {}
                        seg_ordered_ids: list[str] = []
                        seg_idx_by_id: dict[str, int] = {}
                        for ci, u in enumerate(chunk):
                            vid = _video_id_from_watch_url(u)
                            if vid:
                                seg_id_to_url[vid] = u
                                seg_ordered_ids.append(vid)
                                seg_idx_by_id[vid] = ci
                            else:
                                seg_ordered_ids.append("")

                        seg_seen_ids: set[str] = set()
                        seg_ok = 0
                        seg_filtered = 0
                        seg_dup = 0
                        seg_batch_fail = 0
                        seg_watermark = -1
                        seg_total = len(pending_urls) if pending_urls else 0

                        logger.info("[%s] %s — 批量拉取 %d 条视频元数据…", etype, source_name, len(chunk))
                        seg_cmd = self._build_metadata_cmd(chunk)
                        seg_stderr: list[str] = []

                        seg_proc = subprocess.Popen(
                            seg_cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            bufsize=1,
                        )

                        def _read_seg_err():
                            if seg_proc.stderr:
                                for _l in seg_proc.stderr:
                                    seg_stderr.append(_l)

                        seg_err_t = threading.Thread(target=_read_seg_err, daemon=True)
                        seg_err_t.start()

                        def _seg_flush_failures(up_to_idx: int):
                            nonlocal seg_watermark, seg_batch_fail
                            for k in range(seg_watermark + 1, up_to_idx):
                                sk_id = seg_ordered_ids[k]
                                if not sk_id or sk_id in seg_seen_ids:
                                    continue
                                sk_url = chunk[k]
                                if sk_url in existing_urls or sk_url in failed_set:
                                    continue
                                fail_counts[sk_url] = fail_counts.get(sk_url, 0) + 1
                                c = fail_counts[sk_url]
                                seg_batch_fail += 1
                                seen = seg_base + (seg_ok + seg_filtered + seg_dup + seg_batch_fail)
                                if c >= MAX_URL_FETCH_FAILURES:
                                    if sk_url not in failed_set:
                                        failed_urls.append(sk_url)
                                        failed_set.add(sk_url)
                                    logger.warning("  [%d/%d] %s — 拉取失败(已放弃 %d/%d)", seen, seg_total, sk_url, c, MAX_URL_FETCH_FAILURES)
                                else:
                                    logger.warning("  [%d/%d] %s — 拉取失败(%d/%d)", seen, seg_total, sk_url, c, MAX_URL_FETCH_FAILURES)

                        if seg_proc.stdout:
                            while True:
                                raw_line = seg_proc.stdout.readline()
                                if not raw_line:
                                    break

                                if skip_event is not None and skip_event.is_set():
                                    skipped_by_user = True
                                    break

                                raw_line = raw_line.strip()
                                if not raw_line:
                                    continue
                                try:
                                    info = json.loads(raw_line)
                                except json.JSONDecodeError:
                                    continue

                                vid = info.get("id")
                                if not isinstance(vid, str) or not vid:
                                    continue

                                seg_seen_ids.add(vid)
                                cur_idx = seg_idx_by_id.get(vid)

                                if cur_idx is not None and cur_idx > seg_watermark:
                                    _seg_flush_failures(cur_idx)
                                    seg_watermark = cur_idx

                                watch_url = seg_id_to_url.get(vid)

                                _title = info.get("title") or info.get("fulltitle") or vid
                                if len(_title) > 60:
                                    _title = _title[:57] + "..."

                                if not watch_url:
                                    logger.warning(
                                        "  [%d/%d] %s — 跳过(ID未映射到URL)", len(videos), seg_total, _title,
                                    )
                                    continue

                                fail_counts.pop(watch_url, None)

                                if apply_keyword_filter:
                                    title_for_filter, desc_for_filter = _extract_filter_title_desc(info)
                                    if not _accept_by_keywords(title_for_filter, desc_for_filter):
                                        seg_filtered += 1
                                        if watch_url not in failed_set:
                                            failed_urls.append(watch_url)
                                            failed_set.add(watch_url)
                                        if watch_url not in filtered_set:
                                            filtered_urls.append(watch_url)
                                        filtered_set.add(watch_url)
                                        logger.info(
                                            "  [%d/%d] %s — 关键词过滤",
                                            seg_base + (seg_ok + seg_filtered + seg_dup + seg_batch_fail),
                                            seg_total,
                                            _title,
                                        )
                                        continue

                                video = self._parse(info, source_name, etype)
                                if video is None:
                                    logger.warning(
                                        "  [%d/%d] %s — 解析失败",
                                        (seg_ok + seg_filtered + seg_dup + seg_batch_fail),
                                        seg_total,
                                        _title,
                                    )
                                    continue
                                canon = video["url"]
                                if canon in existing_urls:
                                    seg_dup += 1
                                    existing_urls.add(watch_url)
                                    logger.info(
                                        "  [%d/%d] %s — 已存在，跳过",
                                        seg_base + (seg_ok + seg_filtered + seg_dup + seg_batch_fail),
                                        seg_total,
                                        _title,
                                    )
                                    continue

                                videos.append(video)
                                existing_urls.add(canon)
                                existing_urls.add(watch_url)
                                new_count += 1
                                seg_ok += 1

                                seen = seg_base + (seg_ok + seg_filtered + seg_dup + seg_batch_fail)
                                pct = seen / seg_total * 100 if seg_total else 0
                                dur_s = video.get("duration", 0) or 0
                                dur_str = f"{dur_s // 3600}h{(dur_s % 3600) // 60:02d}m" if dur_s >= 3600 else f"{dur_s // 60}m{dur_s % 60:02d}s"
                                logger.info(
                                    "  [%d/%d] (%.1f%%) %s  [%s]",
                                    seen, seg_total, pct, _title, dur_str,
                                )

                                if new_count >= save_interval:
                                    _save_partial_segment()
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

                        if skipped_by_user or cutoff_reached:
                            try:
                                seg_proc.terminate()
                            except Exception:
                                pass

                        seg_proc.wait(timeout=30)
                        seg_err_t.join(timeout=5)

                        _seg_flush_failures(len(chunk))

                        seen = min(seg_total, seg_base + len(chunk))
                        logger.info(
                            "[%s] %s — batch 完成: 新增 %d, 过滤 %d, 重复 %d, 失败 %d | 进度 %d/%d (%.1f%%)",
                            etype, source_name, seg_ok, seg_filtered, seg_dup, seg_batch_fail,
                            seen, seg_total,
                            seen / seg_total * 100 if seg_total else 0,
                        )

                        _save_partial_segment()

                    if skipped_by_user:
                        _save_partial_segment()
                        return videos

                    if cutoff_reached:
                        youtube_pending_urls_done = True
                        cp_mgr.save(
                            entry,
                            {
                                "videos": videos,
                                "youtube_pending_urls": [],
                                "youtube_failed_urls": failed_urls,
                                "youtube_url_fail_counts": fail_counts,
                                "youtube_filtered_urls": filtered_urls,
                                "youtube_pending_urls_done": True,
                                "youtube_list_cursor": youtube_list_cursor,
                                "done": True,
                            },
                        )
                        return videos

                    segment_completed = all(_url_resolved(u) for u in pending_urls)
                    if not segment_completed:
                        # 该段仍有 URL 未成功写入 videos/failed/filter，需续跑本段
                        _save_partial_segment()
                        return videos

                    # 当前段已全部完成：推进游标，清空 pending_urls，并进入下一段
                    youtube_list_cursor += len(pending_urls)
                    pending_urls = []
                    cp_mgr.save(
                        entry,
                        {
                            "videos": videos,
                            "youtube_pending_urls": [],
                            "youtube_failed_urls": failed_urls,
                            "youtube_url_fail_counts": fail_counts,
                            "youtube_filtered_urls": filtered_urls,
                            "youtube_pending_urls_done": False,
                            "youtube_list_cursor": youtube_list_cursor,
                            "done": False,
                        },
                    )
                    new_count = 0

            except KeyboardInterrupt:
                logger.warning("[%s] %s — KeyboardInterrupt，已保存进度", etype, source_name)
                _save_partial_segment()
                raise

            done = (not skipped_by_user) and (cutoff_reached or youtube_pending_urls_done)
            if done:
                logger.info("[%s] %s done — %d videos", etype, source_name, len(videos))
            return videos

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
            if pending_urls is None:
                logger.warning("[%s] %s — 拉取视频列表失败（疑似网络/连接问题），将保持 done=False", etype, source_name)
                cp_mgr.save(
                    entry,
                    {
                        "videos": videos,
                        "youtube_pending_urls": [],
                        "youtube_failed_urls": failed_urls,
                        "youtube_url_fail_counts": fail_counts,
                        "youtube_filtered_urls": filtered_urls,
                        "done": False,
                    },
                )
                return videos
            if not pending_urls:
                logger.warning("[%s] %s — 未获取到任何视频 URL", etype, source_name)
                cp_mgr.save(
                    entry,
                    {
                        "videos": videos,
                        "youtube_pending_urls": [],
                        "youtube_failed_urls": failed_urls,
                        "youtube_url_fail_counts": fail_counts,
                        "youtube_filtered_urls": filtered_urls,
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
                    "youtube_filtered_urls": filtered_urls,
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
                    "youtube_filtered_urls": filtered_urls,
                    "done": False,
                },
            )

        try:
            batch_size = self.metadata_batch_size if self.metadata_batch_size > 0 else 50

            pos = 0
            while pos < len(pending_urls):
                if skip_event is not None and skip_event.is_set():
                    logger.warning("[%s] %s — 收到跳过信号，已保存进度", etype, source_name)
                    skipped_by_user = True
                    break

                batch: list[str] = []
                while pos < len(pending_urls) and len(batch) < batch_size:
                    watch_url = pending_urls[pos]
                    pos += 1
                    if watch_url in existing_urls or watch_url in failed_set or watch_url in filtered_set:
                        continue
                    batch.append(watch_url)

                if not batch:
                    continue

                batch_id_to_url: dict[str, str] = {}
                batch_ordered_ids: list[str] = []
                batch_idx_by_id: dict[str, int] = {}
                for i, u in enumerate(batch):
                    vid = _video_id_from_watch_url(u)
                    if vid:
                        batch_id_to_url[vid] = u
                        batch_ordered_ids.append(vid)
                        batch_idx_by_id[vid] = i
                    else:
                        batch_ordered_ids.append("")

                seen_ids_in_batch: set[str] = set()
                batch_ok = 0
                batch_filtered = 0
                batch_dup = 0
                batch_fail = 0
                watermark = -1

                total_pending = len(pending_urls)
                logger.info("[%s] %s — 批量拉取 %d 条视频元数据…", etype, source_name, len(batch))
                cmd = self._build_metadata_cmd(batch)
                stderr_lines: list[str] = []

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

                err_thread = threading.Thread(target=_read_err, daemon=True)
                err_thread.start()

                def _flush_failures_up_to(up_to_idx: int):
                    """Log failures for batch positions between watermark and up_to_idx."""
                    nonlocal watermark, batch_fail
                    for k in range(watermark + 1, up_to_idx):
                        sk_id = batch_ordered_ids[k]
                        if not sk_id or sk_id in seen_ids_in_batch:
                            continue
                        sk_url = batch[k]
                        if sk_url in existing_urls or sk_url in failed_set:
                            continue
                        fail_counts[sk_url] = fail_counts.get(sk_url, 0) + 1
                        c = fail_counts[sk_url]
                        batch_fail += 1
                        seen = (batch_ok + batch_filtered + batch_dup + batch_fail)
                        if c >= MAX_URL_FETCH_FAILURES:
                            if sk_url not in failed_set:
                                failed_urls.append(sk_url)
                                failed_set.add(sk_url)
                            logger.warning("  [%d/%d] %s — 拉取失败(已放弃 %d/%d)", seen, total_pending, sk_url, c, MAX_URL_FETCH_FAILURES)
                        else:
                            logger.warning("  [%d/%d] %s — 拉取失败(%d/%d)", seen, total_pending, sk_url, c, MAX_URL_FETCH_FAILURES)

                if proc.stdout:
                    while True:
                        line = proc.stdout.readline()
                        if not line:
                            break

                        if skip_event is not None and skip_event.is_set():
                            skipped_by_user = True
                            break

                        line = line.strip()
                        if not line:
                            continue
                        try:
                            info = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        vid = info.get("id")
                        if not isinstance(vid, str) or not vid:
                            continue

                        seen_ids_in_batch.add(vid)
                        cur_idx = batch_idx_by_id.get(vid)

                        if cur_idx is not None and cur_idx > watermark:
                            _flush_failures_up_to(cur_idx)
                            watermark = cur_idx

                        watch_url = batch_id_to_url.get(vid)

                        _title = info.get("title") or info.get("fulltitle") or vid
                        if len(_title) > 60:
                            _title = _title[:57] + "..."

                        if not watch_url:
                            logger.warning(
                                "  [%d/%d] %s — 跳过(ID未映射到URL)", len(videos), total_pending, _title,
                            )
                            continue

                        fail_counts.pop(watch_url, None)

                        if apply_keyword_filter:
                            title_for_filter, desc_for_filter = _extract_filter_title_desc(info)
                            if not _accept_by_keywords(title_for_filter, desc_for_filter):
                                batch_filtered += 1
                                if watch_url not in failed_set:
                                    failed_urls.append(watch_url)
                                    failed_set.add(watch_url)
                                if watch_url not in filtered_set:
                                    filtered_urls.append(watch_url)
                                filtered_set.add(watch_url)
                                canon = info.get("webpage_url") or info.get("url")
                                if isinstance(canon, str) and canon and canon not in filtered_set:
                                    filtered_urls.append(canon)
                                    filtered_set.add(canon)
                                logger.info(
                                    "  [%d/%d] %s — 关键词过滤", len(videos), total_pending, _title,
                                )
                                continue

                        video = self._parse(info, source_name, etype)
                        if video is None:
                            logger.warning(
                                "  [%d/%d] %s — 解析失败", len(videos), total_pending, _title,
                            )
                            continue
                        canon = video["url"]
                        if canon in existing_urls:
                            batch_dup += 1
                            existing_urls.add(watch_url)
                            logger.info(
                                "  [%d/%d] %s — 已存在，跳过", len(videos), total_pending, _title,
                            )
                            continue

                        videos.append(video)
                        existing_urls.add(canon)
                        existing_urls.add(watch_url)
                        new_count += 1
                        batch_ok += 1

                        pct = len(videos) / total_pending * 100 if total_pending else 0
                        dur_s = video.get("duration", 0) or 0
                        dur_str = f"{dur_s // 3600}h{(dur_s % 3600) // 60:02d}m" if dur_s >= 3600 else f"{dur_s // 60}m{dur_s % 60:02d}s"
                        logger.info(
                            "  [%d/%d] (%.1f%%) %s  [%s]",
                            len(videos), total_pending, pct, _title, dur_str,
                        )

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

                if skipped_by_user or cutoff_reached:
                    try:
                        proc.terminate()
                    except Exception:
                        pass

                proc.wait(timeout=30)
                err_thread.join(timeout=5)

                _flush_failures_up_to(len(batch))

                logger.info(
                    "[%s] %s — batch 完成: 新增 %d, 过滤 %d, 重复 %d, 失败 %d | 累计 %d/%d (%.1f%%)",
                    etype, source_name, batch_ok, batch_filtered, batch_dup, batch_fail,
                    len(videos), total_pending,
                    len(videos) / total_pending * 100 if total_pending else 0,
                )

                _save_partial()
                if cutoff_reached or skipped_by_user:
                    break
        except KeyboardInterrupt:
            logger.warning("[%s] %s — KeyboardInterrupt，已保存进度", etype, source_name)
            _save_partial()
            raise

        def _url_resolved(u: str) -> bool:
            return u in existing_urls or u in failed_set or u in filtered_set

        all_list_in_videos = len(pending_urls) == 0 or all(_url_resolved(u) for u in pending_urls)
        done = (not skipped_by_user) and (cutoff_reached or all_list_in_videos)
        cp_mgr.save(
            entry,
            {
                "videos": videos,
                "youtube_pending_urls": pending_urls,
                "youtube_failed_urls": failed_urls,
                "youtube_url_fail_counts": fail_counts,
                "youtube_filtered_urls": filtered_urls,
                "done": done,
            },
        )
        if done:
            logger.info("[%s] %s done — %d videos", etype, source_name, len(videos))
        elif skipped_by_user:
            logger.info("[%s] %s 已暂停 — %d videos（下次将续跑）", etype, source_name, len(videos))
        return videos

    def _fetch_flat_playlist_urls(self, list_url: str) -> list[str] | None:
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
            return None

        if proc is not None and proc.returncode != 0 and not urls:
            logger.warning("yt-dlp flat-playlist exit=%s", proc.returncode)
            if stderr_lines:
                logger.warning("yt-dlp stderr: %s", "".join(stderr_lines[-80:]))
            return None

        # 去重且保序
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _fetch_flat_playlist_urls_range(
        self, list_url: str, start_index: int, end_index: int
    ) -> list[str] | None:
        """Fetch flat-playlist URLs for a 0-based [start_index, end_index] range."""
        # yt-dlp playlist-start/end are 1-based, inclusive.
        start_n = max(0, int(start_index)) + 1
        end_n = max(0, int(end_index)) + 1

        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "--playlist-start",
            str(start_n),
            "--playlist-end",
            str(end_n),
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
            logger.error("flat-playlist range 失败: %s", e)
            if stderr_lines:
                logger.warning("yt-dlp stderr: %s", "".join(stderr_lines[-50:]))
            return None

        if proc is not None and proc.returncode != 0 and not urls:
            logger.warning("yt-dlp flat-playlist range exit=%s", proc.returncode)
            if stderr_lines:
                logger.warning("yt-dlp stderr: %s", "".join(stderr_lines[-80:]))
            return None

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
            "--match-filter", "availability = public",
            "-j",
            "--ignore-errors",
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
            "--extractor-args", "youtube:player_client=android",
            "--extractor-args", "youtube:skip=live,members_only",
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

    def _build_metadata_cmd(self, watch_urls: list[str]) -> list[str]:
        """Build yt-dlp command for streaming metadata of multiple URLs."""
        cmd = [
            "yt-dlp",
            "--match-filter", "availability = public",
            "-j",
            "--ignore-errors",
            "--remote-components", "ejs:github",
            "--extractor-args", "youtube:player_client=android",
            "--extractor-args", "youtube:skip=live,members_only",
        ]
        if self.sleep and float(self.sleep) > 0:
            cmd.extend(["--sleep-interval", str(int(float(self.sleep)))])
        if self.cookies_path:
            cmd.extend(["--cookies", self.cookies_path])
        cmd.extend(watch_urls)
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

