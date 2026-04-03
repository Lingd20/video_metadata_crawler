"""Bilibili video metadata crawler.

Supports:
  - bilibili_up     : 爬取 UP主 全部投稿视频
  - bilibili_series : 爬取合集/系列 全部视频
"""

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from functools import reduce
from pathlib import Path
from urllib.parse import urlencode

import requests

from utils import CheckpointManager, resolve_bilibili_series, resolve_bilibili_up_mid

logger = logging.getLogger(__name__)

# 单条 URL 拉取 view 连续失败超过此次数则写入 bilibili_failed_urls，不再重试
MAX_URL_FETCH_FAILURES = 3


def _bvid_from_video_url(url: str) -> str:
    m = re.search(r"bilibili\.com/video/(BV[\w]+)/?", url, re.I)
    return m.group(1) if m else ""

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
# 相关分类名可参考：https://bilitool.timerring.com/tid.html  或  https://biliup.github.io/tid-ref.html
CATEGORY_MAP = {
    # ===== 动画 =====
    1: "动画",
    24: "MAD·AMV",
    25: "MMD·3D",
    47: "短片·手书",
    257: "配音",
    210: "手办·模玩",
    86: "特摄",
    253: "动漫杂谈",
    27: "综合",

    # ===== 番剧 =====
    13: "番剧",
    33: "连载动画",
    32: "完结动画",
    51: "资讯",
    152: "官方延伸",

    # ===== 国创 =====
    167: "国创",
    153: "国产动画",
    168: "国产原创相关",
    169: "布袋戏",
    170: "资讯",
    195: "动态漫·广播剧",

    # ===== 音乐 =====
    3: "音乐",
    28: "原创音乐",
    31: "翻唱",
    30: "VOCALOID·UTAU",
    59: "演奏",
    193: "MV",
    29: "音乐现场",
    130: "音乐综合",
    243: "乐评盘点",
    244: "音乐教学",
    194: "电音",

    # ===== 舞蹈 =====
    129: "舞蹈",
    20: "宅舞",
    154: "舞蹈综合",
    156: "舞蹈教程",
    198: "街舞",
    199: "明星舞蹈",
    200: "国风舞蹈",
    255: "手势·网红舞",

    # ===== 游戏 =====
    4: "游戏",
    17: "单机游戏",
    171: "电子竞技",
    172: "手机游戏",
    65: "网络游戏",
    173: "桌游棋牌",
    121: "GMV",
    136: "音游",
    19: "Mugen",

    # ===== 知识 =====
    36: "知识",
    201: "科学科普",
    124: "社科·法律·心理",
    228: "人文历史",
    207: "财经商业",
    208: "校园学习",
    209: "职业职场",
    229: "设计·创意",
    122: "野生技术协会",
    39: "演讲·公开课",
    96: "星海",

    # ===== 科技 =====
    188: "科技",
    95: "数码",
    230: "软件应用",
    231: "计算机技术",
    232: "科工机械",
    233: "极客DIY",

    # ===== 运动 =====
    234: "运动",
    235: "篮球",
    249: "足球",
    164: "健身",
    236: "竞技体育",
    237: "运动文化",
    238: "运动综合",

    # ===== 汽车 =====
    223: "汽车",
    258: "汽车知识科普",
    245: "赛车",
    246: "改装玩车",
    247: "新能源车",
    248: "房车",
    240: "摩托车",
    227: "购车攻略",
    176: "汽车生活",
    224: "汽车文化",
    225: "汽车极客",
    226: "智能出行",

    # ===== 生活 =====
    160: "生活",
    138: "搞笑",
    250: "出行",
    251: "三农",
    239: "家居房产",
    161: "手工",
    162: "绘画",
    21: "日常",
    254: "亲子",

    # ===== 美食 =====
    211: "美食",
    76: "美食制作",
    212: "美食侦探",
    213: "美食测评",
    214: "田园美食",
    215: "美食记录",

    # ===== 动物圈 =====
    217: "动物圈",
    218: "喵星人",
    219: "汪星人",
    220: "动物二创",
    221: "野生动物",
    222: "小宠异宠",
    75: "动物综合",

    # ===== 鬼畜 =====
    119: "鬼畜",
    22: "鬼畜调教",
    26: "音MAD",
    126: "人力VOCALOID",
    216: "鬼畜剧场",
    127: "教程演示",

    # ===== 时尚 =====
    155: "时尚",
    157: "美妆护肤",
    252: "仿妆cos",
    158: "穿搭",
    159: "时尚潮流",

    # ===== 娱乐 =====
    5: "娱乐",
    71: "综艺",
    241: "娱乐杂谈",
    242: "粉丝创作",
    137: "明星综合",

    # ===== 影视 =====
    181: "影视",
    182: "影视杂谈",
    183: "影视剪辑",
    85: "小剧场",
    184: "预告·资讯",
    256: "短片",

    # ===== 纪录片 =====
    177: "纪录片",
    37: "人文·历史",
    178: "科学·探索·自然",
    179: "军事",
    180: "社会·美食·旅行",

    # ===== 电影 =====
    23: "电影",
    147: "华语电影",
    145: "欧美电影",
    146: "日本电影",
    83: "其他国家",

    # ===== 电视剧 =====
    11: "电视剧",
    185: "国产剧",
    187: "海外剧",

    # ===== 资讯 =====
    202: "资讯",
    203: "热点",
    204: "环球",
    205: "社会",
    206: "综合",
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


def _extract_category_meta_from_item(item: dict) -> dict:
    """从列表接口条目中提取可用的分区信息（不额外请求 view）。"""
    # 常见字段：typeid / tid / type_id
    # 某些接口/结构可能包含：typename / type_name / tname、以及 *_v2 版本
    return {
        "tid": _safe_int(item.get("typeid") or item.get("tid") or item.get("type_id")),
        "tid_v2": _safe_int(item.get("typeid_v2") or item.get("tid_v2") or item.get("type_id_v2")),
        "tname": (item.get("typename") or item.get("tname") or item.get("type_name") or "").strip(),
        "tname_v2": (item.get("tname_v2") or item.get("type_name_v2") or "").strip(),
    }


def _debug_dump_category_item(cp_mgr: CheckpointManager, source: str, url: str, item: dict):
    """调试用：落盘记录实际返回的字段样例（默认关闭）。"""
    if os.environ.get("BILI_DEBUG_CATEGORY_META", "").strip() not in {"1", "true", "TRUE", "yes", "YES"}:
        return
    try:
        out_path = Path(cp_mgr.dir).parent / "aaa_category_meta_debug.jsonl"
        rec = {
            "ts": datetime.now().isoformat(),
            "source": source,
            "url": url,
            "item_keys": sorted(list(item.keys())),
            "category_meta": _extract_category_meta_from_item(item),
            "raw_item": item,
        }
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("Failed to write category meta debug: %s", e)


def _append_tname_conflict(cp_mgr: CheckpointManager, record: dict):
    """将 tname 与映射不一致的记录写入 checkpoints/aaa_tname_conflict.json。"""
    try:
        # cp_mgr.dir 形如 checkpoints/<input_stem>/，冲突日志写到其父目录 checkpoints/
        out_path = Path(cp_mgr.dir).parent / "aaa_tname_conflict.json"
        existing = []
        if out_path.exists():
            with open(out_path, "r", encoding="utf-8") as f:
                existing = json.load(f) or []
        # 以 url 去重，避免重复追加
        url = record.get("url", "")
        if url and any(r.get("url") == url for r in existing):
            return
        existing.append(record)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Failed to write tname conflict log: %s", e)


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
        self._mid_name_cache: dict[str, str] = {}

        cutoff = bc.get("cutoff_date", "")
        self.cutoff_dt: datetime | None = None
        if cutoff:
            try:
                self.cutoff_dt = datetime.strptime(str(cutoff), "%Y-%m-%d")
                logger.info("Bilibili cutoff date: %s", self.cutoff_dt.strftime("%Y-%m-%d"))
            except ValueError:
                logger.warning("Invalid cutoff_date '%s', ignored (expected YYYY-MM-DD)", cutoff)

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
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    snippet = (resp.text or "")[:500]
                    logger.warning("API 返回非 JSON (attempt %d): %s", attempt, snippet)
                    if attempt < self.max_retries:
                        time.sleep(self.error_sleep)
                    continue
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

    def _resolve_category_and_meta(self, cp_mgr: CheckpointManager, source: str, url: str, item: dict) -> tuple[str, dict]:
        """优先使用条目中自带的 tname；若与映射不一致则记录冲突。"""
        meta = _extract_category_meta_from_item(item)
        mapped = _category_name(meta.get("tid"))
        tname = meta.get("tname", "")
        category = tname or mapped
        if tname and mapped and tname != mapped:
            _append_tname_conflict(cp_mgr, {"url": url, "category_meta": meta, "mapped_category": mapped})
        _debug_dump_category_item(cp_mgr, source, url, item)
        return category, meta

    def _bilibili_ckpt_payload(
        self,
        videos: list,
        pending_urls: list[str],
        failed_urls: list[str],
        fail_counts: dict[str, int],
        done: bool,
        **extra,
    ) -> dict:
        d = {
            "videos": videos,
            "bilibili_pending_urls": pending_urls,
            "bilibili_failed_urls": failed_urls,
            "bilibili_url_fail_counts": fail_counts,
            "done": done,
        }
        d.update(extra)
        return d

    def _resolve_bilibili_pending_state(
        self, cp: dict, videos: list, refresh: bool
    ) -> tuple[list[str] | None, bool]:
        """返回 (pending_urls 或 None, 是否需要重新拉列表)。"""
        if refresh:
            return None, True
        raw = cp.get("bilibili_pending_urls")
        if isinstance(raw, list):
            pl = list(raw)
            if len(pl) == 0 and not cp.get("done", False):
                return pl, True
            return pl, False
        if cp.get("done", False):
            return [v["url"] for v in videos if v.get("url")], False
        return None, True

    def _video_record_from_view(
        self,
        view: dict,
        source_name: str,
        source_type: str,
        url: str,
        cp_mgr: CheckpointManager,
        dbg_key: str,
    ) -> dict | None:
        if not view or not view.get("bvid"):
            return None
        category, category_meta = self._resolve_category_and_meta(cp_mgr, dbg_key, url, view)
        pub = view.get("pubdate")
        publish_time = ""
        if pub:
            try:
                publish_time = datetime.fromtimestamp(int(pub)).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError):
                pass
        stat = view.get("stat") or {}
        owner = view.get("owner") or {}
        uploader = owner.get("name", "")
        return {
            "source_name": source_name,
            "source_type": source_type,
            "platform": "bilibili",
            "title": view.get("title", ""),
            "url": url,
            "duration": _safe_int(view.get("duration")),
            "category": category,
            "category_meta": category_meta,
            "publish_time": publish_time,
            "view_count": _safe_int(stat.get("view")),
            "uploader": uploader,
        }

    def _collect_up_pending_urls(self, mid: str) -> list[str]:
        urls: list[str] = []
        page = 1
        while True:
            params = {"mid": mid, "ps": self.page_size, "pn": page, "order": "pubdate"}
            data = self._get(
                "https://api.bilibili.com/x/space/wbi/arc/search",
                params=params,
                sign=True,
            )
            if data is None:
                logger.error("[bilibili_up] 列表分页 %d 失败 — 停止收集", page)
                break
            if page == 1:
                total = data.get("page", {}).get("count", 0)
                logger.info("  频道稿件总数: %d", total)
            vlist = data.get("list", {}).get("vlist", [])
            if not vlist:
                break
            cutoff_reached = False
            for v in vlist:
                if self.cutoff_dt and v.get("created"):
                    pt = datetime.fromtimestamp(v["created"])
                    if pt < self.cutoff_dt:
                        logger.info(
                            "到达 cutoff %s (稿件 %s)，停止收集列表",
                            self.cutoff_dt.strftime("%Y-%m-%d"),
                            pt.strftime("%Y-%m-%d"),
                        )
                        cutoff_reached = True
                        break
                bvid = v.get("bvid", "")
                if bvid:
                    urls.append(f"https://www.bilibili.com/video/{bvid}")
            if cutoff_reached or len(vlist) < self.page_size:
                break
            page += 1
            time.sleep(self.sleep)
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _collect_season_archives_urls(self, mid: str, season_id: str) -> list[str]:
        urls: list[str] = []
        page_size = 30
        page = 1
        while True:
            params = {
                "mid": mid,
                "season_id": season_id,
                "page_num": page,
                "page_size": page_size,
                "sort_reverse": "false",
            }
            data = self._get(
                "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list",
                params=params,
                sign=False,
            )
            if data is None:
                logger.error("[bilibili_series] season 列表页 %d 失败", page)
                break
            archives = data.get("archives", [])
            if not archives:
                break
            if page == 1:
                logger.info("  合集中视频总数: %d", data.get("page", {}).get("total", 0))
            cutoff_reached = False
            for v in archives:
                if self.cutoff_dt and v.get("pubdate"):
                    pt = datetime.fromtimestamp(v["pubdate"])
                    if pt < self.cutoff_dt:
                        logger.info(
                            "到达 cutoff %s，停止收集列表",
                            self.cutoff_dt.strftime("%Y-%m-%d"),
                        )
                        cutoff_reached = True
                        break
                bvid = v.get("bvid", "") or v.get("bv_id", "")
                if bvid:
                    urls.append(f"https://www.bilibili.com/video/{bvid}")
            if cutoff_reached or len(archives) < page_size:
                break
            page += 1
            time.sleep(self.sleep)
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _collect_space_list_urls(self, mid: str, list_id: str, list_kind: str) -> list[str]:
        urls: list[str] = []
        page_size = 30
        page = 1
        while True:
            archives, _total = self._fetch_list_page(list_kind, mid, list_id, page, page_size)
            if archives is None:
                logger.error("[bilibili_series] list 分页 %d 失败", page)
                break
            if not archives:
                break
            if page == 1:
                logger.info("  list 中视频总数: %d", _total)
            cutoff_reached = False
            for v in archives:
                if self.cutoff_dt and v.get("pubdate"):
                    pt = datetime.fromtimestamp(v["pubdate"])
                    if pt < self.cutoff_dt:
                        logger.info(
                            "到达 cutoff %s，停止收集列表",
                            self.cutoff_dt.strftime("%Y-%m-%d"),
                        )
                        cutoff_reached = True
                        break
                bvid = v.get("bvid", "") or v.get("bv_id", "")
                if bvid:
                    urls.append(f"https://www.bilibili.com/video/{bvid}")
            if cutoff_reached or len(archives) < page_size:
                break
            page += 1
            time.sleep(self.sleep)
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _bilibili_process_pending_urls(
        self,
        entry: dict,
        cp_mgr: CheckpointManager,
        save_interval: int,
        source_name: str,
        source_type: str,
        pending_urls: list[str],
        failed_urls: list[str],
        fail_counts: dict[str, int],
        videos: list,
        existing_urls: set[str],
        extra_ckpt: dict,
    ) -> None:
        failed_set = set(failed_urls)
        new_since_save = 0

        def _save_state(done: bool):
            cp_mgr.save(
                entry,
                self._bilibili_ckpt_payload(
                    videos,
                    pending_urls,
                    failed_urls,
                    fail_counts,
                    done,
                    **extra_ckpt,
                ),
            )

        for url in pending_urls:
            if url in existing_urls:
                continue
            if url in failed_set:
                continue
            bvid = _bvid_from_video_url(url)
            if not bvid:
                fail_counts[url] = fail_counts.get(url, 0) + 1
                if fail_counts[url] >= MAX_URL_FETCH_FAILURES:
                    if url not in failed_set:
                        failed_urls.append(url)
                        failed_set.add(url)
                    logger.warning("无法解析 BV 号，已加入 bilibili_failed_urls: %s", url)
                else:
                    logger.warning("无法解析 BV 号 (%d/%d): %s", fail_counts[url], MAX_URL_FETCH_FAILURES, url)
                _save_state(False)
                continue

            view = self._get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                sign=False,
            )
            if view is None:
                fail_counts[url] = fail_counts.get(url, 0) + 1
                c = fail_counts[url]
                if c >= MAX_URL_FETCH_FAILURES:
                    if url not in failed_set:
                        failed_urls.append(url)
                        failed_set.add(url)
                    logger.warning("view 连续失败 %d 次，已加入 bilibili_failed_urls: %s", c, url)
                else:
                    logger.warning("view 拉取失败 (%d/%d): %s", c, MAX_URL_FETCH_FAILURES, url)
                _save_state(False)
                continue

            fail_counts.pop(url, None)
            rec = self._video_record_from_view(
                view, source_name, source_type, url, cp_mgr, f"{source_type}.view"
            )
            if rec is None:
                fail_counts[url] = fail_counts.get(url, 0) + 1
                if fail_counts[url] >= MAX_URL_FETCH_FAILURES:
                    if url not in failed_set:
                        failed_urls.append(url)
                        failed_set.add(url)
                _save_state(False)
                continue

            videos.append(rec)
            existing_urls.add(url)
            new_since_save += 1
            if new_since_save >= save_interval:
                _save_state(False)
                new_since_save = 0
            if self.sleep:
                time.sleep(float(self.sleep))

        def _resolved(u: str) -> bool:
            return u in existing_urls or u in failed_set

        done = (not pending_urls) or all(_resolved(u) for u in pending_urls)
        _save_state(done)
        logger.info("[%s] %s — 完成，共 %d 条视频", source_type, source_name, len(videos))

    # -- public crawl interface --

    def crawl(
        self,
        entry: dict,
        cp_mgr: CheckpointManager,
        save_interval: int = 50,
        refresh_pending_urls: bool = False,
    ):
        """Crawl videos for one entry, returns list of video dicts."""
        etype = entry["type"]
        if etype == "bilibili_up":
            return self._crawl_up(entry, cp_mgr, save_interval, refresh_pending_urls)
        if etype == "bilibili_series":
            return self._crawl_series(entry, cp_mgr, save_interval, refresh_pending_urls)
        logger.error("Unsupported bilibili type: %s", etype)
        return []

    # -- UP主 --

    def _crawl_up(self, entry, cp_mgr, save_interval, refresh_pending_urls: bool = False):
        mid = resolve_bilibili_up_mid(entry["id"])
        source_name = entry["name"]
        cp = cp_mgr.load(entry) or {}
        videos = list(cp.get("videos", []))
        existing_urls = {v["url"] for v in videos}
        failed_urls = list(cp.get("bilibili_failed_urls") or [])
        fail_counts = dict(cp.get("bilibili_url_fail_counts") or {})

        pending_state, need_fetch = self._resolve_bilibili_pending_state(
            cp, videos, refresh_pending_urls
        )
        if need_fetch:
            if refresh_pending_urls:
                logger.info("[bilibili_up] %s (mid=%s) — 强制重新拉取视频列表", source_name, mid)
            else:
                logger.info("[bilibili_up] %s (mid=%s) — 拉取视频列表", source_name, mid)
            pending_urls = self._collect_up_pending_urls(mid)
            cp_mgr.save(
                entry,
                self._bilibili_ckpt_payload(videos, pending_urls, failed_urls, fail_counts, False),
            )
            logger.info("  已缓存 %d 条 URL，将逐条请求 view 补全元数据", len(pending_urls))
        else:
            pending_urls = list(pending_state or [])
            logger.info(
                "[bilibili_up] %s — 使用 checkpoint 列表 %d 条，已有 videos %d 条",
                source_name,
                len(pending_urls),
                len(videos),
            )

        if not pending_urls:
            cp_mgr.save(
                entry,
                self._bilibili_ckpt_payload(videos, [], failed_urls, fail_counts, True),
            )
            return videos

        self._bilibili_process_pending_urls(
            entry,
            cp_mgr,
            save_interval,
            source_name,
            "bilibili_up",
            pending_urls,
            failed_urls,
            fail_counts,
            videos,
            existing_urls,
            {},
        )
        return videos

    def _get_uploader_name(self, mid: str) -> str:
        """通过 mid 获取 UP 主昵称，带缓存。"""
        mid = str(mid)
        if mid in self._mid_name_cache:
            return self._mid_name_cache[mid]
        data = self._get(
            "https://api.bilibili.com/x/space/wbi/acc/info",
            params={"mid": mid},
            sign=True,
        )
        name = data.get("name", "") if data else ""
        self._mid_name_cache[mid] = name
        return name

    def _resolve_uploader(self, video_item: dict, mid: str) -> str:
        """优先用 API 返回的 author 字段，否则根据 mid 查询。"""
        author = video_item.get("author", "")
        if author:
            return author
        return self._get_uploader_name(mid)

    # -- 合集 / 系列 --

    def _crawl_series(
        self, entry, cp_mgr, save_interval, refresh_pending_urls: bool = False
    ):
        kind, mid, series_id = resolve_bilibili_series(entry["id"])
        source_name = entry["name"]

        if kind == "list":
            return self._crawl_space_list_api(
                entry, mid, series_id, cp_mgr, save_interval, refresh_pending_urls
            )

        cp = cp_mgr.load(entry) or {}
        videos = list(cp.get("videos", []))
        existing_urls = {v["url"] for v in videos}
        failed_urls = list(cp.get("bilibili_failed_urls") or [])
        fail_counts = dict(cp.get("bilibili_url_fail_counts") or {})

        pending_state, need_fetch = self._resolve_bilibili_pending_state(
            cp, videos, refresh_pending_urls
        )
        if need_fetch:
            if refresh_pending_urls:
                logger.info(
                    "[bilibili_series] %s (season, mid=%s, id=%s) — 强制重新拉取列表",
                    source_name,
                    mid,
                    series_id,
                )
            else:
                logger.info(
                    "[bilibili_series] %s (season, mid=%s, id=%s) — 拉取视频 URL 列表",
                    source_name,
                    mid,
                    series_id,
                )
            pending_urls = self._collect_season_archives_urls(mid, series_id)
            cp_mgr.save(
                entry,
                self._bilibili_ckpt_payload(videos, pending_urls, failed_urls, fail_counts, False),
            )
            logger.info("  已缓存 %d 条 URL，将逐条请求 view", len(pending_urls))
        else:
            pending_urls = list(pending_state or [])
            logger.info(
                "[bilibili_series] %s (season) — checkpoint 列表 %d 条，已有 videos %d 条",
                source_name,
                len(pending_urls),
                len(videos),
            )

        if not pending_urls:
            cp_mgr.save(
                entry,
                self._bilibili_ckpt_payload(videos, [], failed_urls, fail_counts, True),
            )
            return videos

        self._bilibili_process_pending_urls(
            entry,
            cp_mgr,
            save_interval,
            source_name,
            "bilibili_series",
            pending_urls,
            failed_urls,
            fail_counts,
            videos,
            existing_urls,
            {},
        )
        return videos

    def _crawl_space_list_api(
        self,
        entry,
        mid,
        series_id,
        cp_mgr,
        save_interval,
        refresh_pending_urls: bool = False,
    ):
        """爬取 space list（/lists/...）的视频。

        B 站的 /lists/{id} URL 可能对应「系列」或「合集」两种类型，
        使用的 API 不同。先探测类型，再分页收集 URL，最后逐条 view。
        """
        source_name = entry["name"]
        cp = cp_mgr.load(entry) or {}
        videos = list(cp.get("videos", []))
        existing_urls = {v["url"] for v in videos}
        failed_urls = list(cp.get("bilibili_failed_urls") or [])
        fail_counts = dict(cp.get("bilibili_url_fail_counts") or {})
        list_kind = cp.get("bilibili_list_kind") or cp.get("list_kind", "")

        pending_state, need_fetch = self._resolve_bilibili_pending_state(
            cp, videos, refresh_pending_urls
        )
        if need_fetch:
            list_kind = self._detect_list_kind(mid, series_id)
            logger.info(
                "[bilibili_series] %s (list, mid=%s, id=%s) — %s，类型=%s",
                source_name,
                mid,
                series_id,
                "强制刷新列表" if refresh_pending_urls else "拉取视频 URL 列表",
                list_kind,
            )
            pending_urls = self._collect_space_list_urls(mid, series_id, list_kind)
            cp_mgr.save(
                entry,
                self._bilibili_ckpt_payload(
                    videos,
                    pending_urls,
                    failed_urls,
                    fail_counts,
                    False,
                    bilibili_list_kind=list_kind,
                ),
            )
            logger.info("  已缓存 %d 条 URL，将逐条请求 view", len(pending_urls))
        else:
            pending_urls = list(pending_state or [])
            if not list_kind:
                list_kind = self._detect_list_kind(mid, series_id)
            logger.info(
                "[bilibili_series] %s (list, %s) — checkpoint 列表 %d 条，已有 videos %d 条",
                source_name,
                list_kind,
                len(pending_urls),
                len(videos),
            )

        if not pending_urls:
            cp_mgr.save(
                entry,
                self._bilibili_ckpt_payload(
                    videos,
                    [],
                    failed_urls,
                    fail_counts,
                    True,
                    bilibili_list_kind=list_kind,
                ),
            )
            return videos

        self._bilibili_process_pending_urls(
            entry,
            cp_mgr,
            save_interval,
            source_name,
            "bilibili_series",
            pending_urls,
            failed_urls,
            fail_counts,
            videos,
            existing_urls,
            {"bilibili_list_kind": list_kind},
        )
        return videos

    def _detect_list_kind(self, mid: str, list_id: str) -> str:
        """探测 /lists/{id} 是系列还是合集：先试系列 API，有数据则为 series，否则为 season。"""
        data = self._get(
            "https://api.bilibili.com/x/series/archives",
            params={"mid": mid, "series_id": list_id, "pn": 1, "ps": 1, "sort": "desc"},
            sign=False,
        )
        if data and data.get("archives"):
            return "series"
        return "season"

    def _fetch_list_page(self, list_kind: str, mid: str, list_id: str, page: int, page_size: int):
        """根据类型调用对应 API，返回 (archives, total) 或 (None, 0) 表示失败。"""
        if list_kind == "series":
            data = self._get(
                "https://api.bilibili.com/x/series/archives",
                params={"mid": mid, "series_id": list_id, "pn": page, "ps": page_size, "sort": "desc"},
                sign=False,
            )
        else:
            data = self._get(
                "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list",
                params={"mid": mid, "season_id": list_id, "page_num": page, "page_size": page_size, "sort_reverse": "false"},
                sign=False,
            )
        if data is None:
            return None, 0
        archives = data.get("archives", [])
        total = data.get("page", {}).get("total", 0)
        return archives, total
