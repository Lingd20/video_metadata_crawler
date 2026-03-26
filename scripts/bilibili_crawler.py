"""Bilibili video metadata crawler.

Supports:
  - bilibili_up     : 爬取 UP主 全部投稿视频
  - bilibili_series : 爬取合集/系列 全部视频
"""

import hashlib
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from functools import reduce
from urllib.parse import urlencode

import requests

from utils import CheckpointManager, resolve_bilibili_series, resolve_bilibili_up_mid

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# WBI signature (Bilibili anti-crawl)
# ---------------------------------------------------------------------------
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# Bilibili top-level / common sub-category names
CATEGORY_MAP = {
    1: "动画", 13: "番剧", 167: "国创", 3: "音乐", 129: "舞蹈",
    4: "游戏", 36: "知识", 188: "科技", 234: "运动", 223: "汽车",
    160: "生活", 211: "美食", 217: "动物圈", 119: "鬼畜",
    155: "时尚", 5: "娱乐", 181: "影视", 177: "纪录片",
    23: "电影", 11: "电视剧", 202: "资讯",
    24: "MAD·AMV", 25: "MMD·3D", 47: "短片·手书", 257: "配音",
    27: "综合", 17: "单机游戏", 171: "电子竞技", 172: "手机游戏",
    65: "网络游戏", 136: "音游", 28: "原创音乐", 31: "翻唱",
    30: "VOCALOID", 59: "演奏", 193: "MV", 29: "音乐现场",
    20: "宅舞", 198: "街舞", 199: "明星舞蹈", 200: "中国舞",
    124: "社科·法律·心理", 228: "人文历史", 207: "财经商业",
    208: "校园学习", 209: "职业职场", 229: "设计·创意",
    122: "野生技术协会", 95: "数码", 230: "软件应用",
    138: "搞笑", 21: "日常", 250: "出行", 251: "三农",
    161: "手工", 162: "绘画", 163: "模型",
    176: "汽车生活", 224: "汽车文化",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(val, default=0):
    if isinstance(val, int):
        return val
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _parse_bilibili_duration(length_str):
    """Parse '12:34' or '1:23:45' to seconds."""
    if isinstance(length_str, (int, float)):
        return int(length_str)
    try:
        parts = str(length_str).split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        return int(length_str)
    except (ValueError, TypeError):
        return 0


def _category_name(typeid):
    tid = _safe_int(typeid)
    return CATEGORY_MAP.get(tid, str(tid) if tid else "")


# ---------------------------------------------------------------------------
# BilibiliCrawler
# ---------------------------------------------------------------------------

class BilibiliCrawler:

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
    }

    def __init__(self, config: dict, cookies_path: str = ""):
        self.session = requests.Session()
        # Avoid being affected by system proxy settings (common on Windows / corp networks)
        # Users can still explicitly proxy by setting requests' environment themselves if desired.
        self.session.trust_env = False
        self.session.headers.update(self._HEADERS)

        bc = config.get("bilibili", {})
        self.page_size = bc.get("page_size", 50)
        self.sleep = bc.get("sleep_interval", 2)
        self.error_sleep = bc.get("error_sleep_interval", 30)
        self.max_retries = bc.get("max_retries", 3)

        if cookies_path and os.path.exists(cookies_path):
            self._load_cookies(cookies_path)
        self.cookies_path = cookies_path if cookies_path and os.path.exists(cookies_path) else ""

        self._img_key = None
        self._sub_key = None
        self._wbi_ts = 0

    # -- cookies --

    def _load_cookies(self, path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    domain, _, ck_path, _, _, name, value = parts[:7]
                    self.session.cookies.set(name, value, domain=domain, path=ck_path)
        logger.info("Loaded bilibili cookies from %s", path)

    # -- WBI signing --

    def _refresh_wbi_keys(self):
        if self._img_key and (time.time() - self._wbi_ts) < 3600:
            return
        try:
            r = self.session.get("https://api.bilibili.com/x/web-interface/nav", timeout=15)
            wbi = r.json()["data"]["wbi_img"]
            self._img_key = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
            self._sub_key = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
            self._wbi_ts = time.time()
        except Exception as e:
            logger.warning("Failed to refresh WBI keys: %s", e)

    def _mixin_key(self):
        raw = self._img_key + self._sub_key
        return reduce(lambda s, i: s + raw[i], MIXIN_KEY_ENC_TAB, "")[:32]

    def _sign(self, params: dict) -> dict:
        self._refresh_wbi_keys()
        mk = self._mixin_key()
        params["wts"] = int(time.time())
        params = dict(sorted(params.items()))
        params = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
        query = urlencode(params)
        params["w_rid"] = hashlib.md5((query + mk).encode()).hexdigest()
        return params

    # -- API helpers --

    def _get(self, url, params=None, sign=False):
        for attempt in range(1, self.max_retries + 1):
            try:
                p = self._sign(dict(params)) if sign and params else params
                resp = self.session.get(url, params=p, timeout=30)
                data = resp.json()
                code = data.get("code", -1)
                if code == 0:
                    return data.get("data")
                if code == -412:
                    logger.warning("Rate-limited (attempt %d), sleeping %ds", attempt, self.error_sleep)
                    time.sleep(self.error_sleep)
                    continue
                logger.warning("API error code=%d msg=%s (attempt %d)", code, data.get("message"), attempt)
                if attempt < self.max_retries:
                    time.sleep(self.error_sleep)
            except Exception as e:
                logger.error("Request failed (attempt %d): %s", attempt, e)
                if attempt < self.max_retries:
                    time.sleep(self.error_sleep)
        return None

    # -- public crawl interface --

    def crawl(self, entry: dict, cp_mgr: CheckpointManager, save_interval: int = 50):
        """Crawl videos for one entry, returns list of video dicts."""
        etype = entry["type"]
        if etype == "bilibili_up":
            return self._crawl_up(entry, cp_mgr, save_interval)
        if etype == "bilibili_series":
            return self._crawl_series(entry, cp_mgr, save_interval)
        logger.error("Unsupported bilibili type: %s", etype)
        return []

    # -- UP主 --

    def _crawl_up(self, entry, cp_mgr, save_interval):
        mid = resolve_bilibili_up_mid(entry["id"])
        source_name = entry["name"]

        cp = cp_mgr.load(entry)
        videos = cp.get("videos", []) if cp else []
        start_page = cp.get("next_page", 1) if cp else 1
        existing_urls = {v["url"] for v in videos}
        new_count = 0

        logger.info("[bilibili_up] %s (mid=%s) — resuming from page %d, %d videos cached",
                     source_name, mid, start_page, len(videos))

        page = start_page
        while True:
            params = {"mid": mid, "ps": self.page_size, "pn": page, "order": "pubdate"}
            data = self._get("https://api.bilibili.com/x/space/wbi/arc/search",
                             params=params, sign=True)
            if data is None:
                logger.error("Failed page %d — stopping", page)
                break

            total = data.get("page", {}).get("count", 0)
            if page == start_page:
                logger.info("  Total videos on channel: %d", total)

            vlist = data.get("list", {}).get("vlist", [])
            if not vlist:
                break

            for v in vlist:
                bvid = v.get("bvid", "")
                url = f"https://www.bilibili.com/video/{bvid}"
                if url in existing_urls:
                    continue
                videos.append({
                    "source_name": source_name,
                    "source_type": "bilibili_up",
                    "platform": "bilibili",
                    "title": v.get("title", ""),
                    "url": url,
                    "duration": _parse_bilibili_duration(v.get("length", "0:00")),
                    "category": _category_name(v.get("typeid")),
                    "publish_time": (datetime.fromtimestamp(v["created"]).strftime("%Y-%m-%d %H:%M:%S")
                                     if v.get("created") else ""),
                    "view_count": _safe_int(v.get("play")),
                    "uploader": v.get("author", ""),
                })
                existing_urls.add(url)
                new_count += 1

            logger.info("  Page %d: +%d new (total %d)", page, len(vlist), len(videos))

            if new_count >= save_interval:
                cp_mgr.save(entry, {"videos": videos, "next_page": page + 1, "done": False})
                new_count = 0

            if len(vlist) < self.page_size:
                break
            page += 1
            time.sleep(self.sleep)

        cp_mgr.save(entry, {"videos": videos, "next_page": page, "done": True})
        logger.info("[bilibili_up] %s done — %d videos", source_name, len(videos))
        return videos

    # -- 合集 / 系列 --

    def _crawl_series(self, entry, cp_mgr, save_interval):
        kind, mid, series_id = resolve_bilibili_series(entry["id"])
        source_name = entry["name"]

        # For space lists (/lists/...), prefer yt-dlp which can parse reliably
        # (and matches user's CLI verification).
        if kind == "list":
            url = entry["id"] if str(entry["id"]).startswith("http") else f"https://space.bilibili.com/{mid}/lists/{series_id}"
            return self._crawl_space_list_with_ytdlp(entry, url, cp_mgr, save_interval)

        cp = cp_mgr.load(entry)
        videos = cp.get("videos", []) if cp else []
        start_page = cp.get("next_page", 1) if cp else 1
        existing_urls = {v["url"] for v in videos}
        new_count = 0
        page_size = 30

        logger.info(
            "[bilibili_series] %s (kind=%s, mid=%s, id=%s) — resuming page %d, %d cached",
            source_name,
            kind,
            mid,
            series_id,
            start_page,
            len(videos),
        )

        page = start_page
        while True:
            if kind == "season":
                params = {
                    "mid": mid,
                    "season_id": series_id,
                    "page_num": page,
                    "page_size": page_size,
                    "sort_reverse": "false",
                }
                data = self._get(
                    "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list",
                    params=params,
                    sign=False,
                )
                archives = data.get("archives", []) if data else []
                total = data.get("page", {}).get("total", 0) if data else 0
            else:
                archives, total = [], 0

            if data is None:
                logger.error("Failed page %d — stopping", page)
                break

            if not archives:
                break

            if page == start_page:
                logger.info("  Total videos in series: %d", total)

            for v in archives:
                bvid = v.get("bvid", "") or v.get("bv_id", "")
                url = f"https://www.bilibili.com/video/{bvid}"
                if url in existing_urls:
                    continue
                dur = v.get("duration", 0)
                if isinstance(dur, str):
                    dur = _parse_bilibili_duration(dur)
                videos.append({
                    "source_name": source_name,
                    "source_type": "bilibili_series",
                    "platform": "bilibili",
                    "title": v.get("title", ""),
                    "url": url,
                    "duration": dur,
                    "category": _category_name(v.get("typeid")),
                    "publish_time": (
                        datetime.fromtimestamp(v["pubdate"]).strftime("%Y-%m-%d %H:%M:%S")
                        if v.get("pubdate")
                        else ""
                    ),
                    "view_count": _safe_int(v.get("stat", {}).get("view") or v.get("cnt_info", {}).get("play")),
                    "uploader": source_name,
                })
                existing_urls.add(url)
                new_count += 1

            logger.info("  Page %d: +%d (total %d)", page, len(archives), len(videos))

            if new_count >= save_interval:
                cp_mgr.save(entry, {"videos": videos, "next_page": page + 1, "done": False})
                new_count = 0

            if len(archives) < page_size:
                break
            page += 1
            time.sleep(self.sleep)

        cp_mgr.save(entry, {"videos": videos, "next_page": page, "done": True})
        logger.info("[bilibili_series] %s done — %d videos", source_name, len(videos))
        return videos

    def _crawl_space_list_with_ytdlp(self, entry, url, cp_mgr, save_interval):
        source_name = entry["name"]
        cp = cp_mgr.load(entry)
        videos = cp.get("videos", []) if cp else []
        existing_urls = {v.get("url", "") for v in videos if v.get("url")}
        start_index = len(videos) + 1

        logger.info(
            "[bilibili_series] %s (kind=list) — url=%s, resuming from index %d (%d cached)",
            source_name,
            url,
            start_index,
            len(videos),
        )

        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--dump-json",
            "--no-download",
            "--ignore-errors",
            "--extractor-retries",
            "3",
            "--proxy",
            "",
            "--no-update",
        ]
        if self.cookies_path:
            cmd.extend(["--cookies", self.cookies_path])
        if start_index > 1:
            cmd.extend(["--playlist-start", str(start_index)])
        if self.sleep and self.sleep > 0:
            cmd.extend(["--sleep-requests", str(self.sleep)])
        cmd.append(url)

        new_count = 0
        try:
            env = os.environ.copy()
            for k in [
                "http_proxy",
                "https_proxy",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "all_proxy",
                "ALL_PROXY",
            ]:
                env[k] = ""
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
            for line in iter(proc.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue
                try:
                    info = __import__("json").loads(line)
                except Exception:
                    continue

                video = self._parse_ytdlp_video(info, source_name)
                if video is None:
                    continue
                if video["url"] in existing_urls:
                    continue

                videos.append(video)
                existing_urls.add(video["url"])
                new_count += 1

                if new_count % 50 == 0:
                    logger.info("  Fetched %d new (total %d)", new_count, len(videos))

                if new_count >= save_interval:
                    cp_mgr.save(entry, {"videos": videos, "done": False})
                    new_count = 0

            proc.wait()
            if proc.returncode not in (0, None):
                stderr = proc.stderr.read()
                if stderr:
                    logger.warning("yt-dlp stderr: %s", stderr[:500])
                # Do not mark as done; allow retry after environment/proxy fixes.
                cp_mgr.save(entry, {"videos": videos, "done": False})
                logger.warning("[bilibili_series] %s (kind=list) yt-dlp failed — checkpoint kept as not-done", source_name)
                return videos
        except KeyboardInterrupt:
            logger.warning("Interrupted — saving checkpoint")
            cp_mgr.save(entry, {"videos": videos, "done": False})
            raise

        cp_mgr.save(entry, {"videos": videos, "done": True})
        logger.info("[bilibili_series] %s (kind=list) done — %d videos", source_name, len(videos))
        return videos

    @staticmethod
    def _parse_ytdlp_video(info: dict, source_name: str):
        webpage_url = info.get("webpage_url") or info.get("url")
        if not webpage_url:
            return None

        # publish time
        publish_time = ""
        ts = info.get("timestamp")
        if ts:
            try:
                publish_time = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                publish_time = ""
        if not publish_time:
            upload_date = info.get("upload_date", "")
            if upload_date and len(upload_date) == 8:
                publish_time = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

        categories = info.get("categories") or []
        category = ", ".join(categories) if isinstance(categories, list) else str(categories or "")
        if not category:
            category = info.get("category") or ""

        return {
            "source_name": source_name,
            "source_type": "bilibili_series",
            "platform": "bilibili",
            "title": info.get("title", ""),
            "url": webpage_url,
            "duration": info.get("duration", 0) or 0,
            "category": category,
            "publish_time": publish_time,
            "view_count": info.get("view_count", 0) or 0,
            "uploader": info.get("uploader") or info.get("channel") or "",
        }
