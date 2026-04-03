"""从 Bilibili 视频 URL 获取原始 metadata 并打印。

用法:
    python utils/fetch_video_meta.py <video_url> [--cookies CONFIG_DIR/bilibili_cookies.txt]

示例:
    python utils/fetch_video_meta.py https://www.bilibili.com/video/BV1xx411c7mD
    python utils/fetch_video_meta.py BV1xx411c7mD
    python utils/fetch_video_meta.py https://www.bilibili.com/video/BV1xx411c7mD --cookies config/bilibili_cookies.txt
"""

import argparse
import hashlib
import json
import re
import sys
import time
from functools import reduce
from urllib.parse import urlencode

import requests

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


def _extract_bvid(url_or_bvid: str) -> str:
    """从 URL 或纯 BV 号中提取 bvid。"""
    m = re.search(r"(BV[\w]+)", url_or_bvid)
    if m:
        return m.group(1)
    raise ValueError(f"无法从输入中提取 BV 号: {url_or_bvid}")


def _load_cookies(session: requests.Session, path: str):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                domain, _, ck_path, _, _, name, value = parts[:7]
                session.cookies.set(name, value, domain=domain, path=ck_path)


def _get_wbi_keys(session: requests.Session):
    r = session.get("https://api.bilibili.com/x/web-interface/nav", timeout=15)
    wbi = r.json()["data"]["wbi_img"]
    img_key = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
    sub_key = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
    return img_key, sub_key


def _mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return reduce(lambda s, i: s + raw[i], MIXIN_KEY_ENC_TAB, "")[:32]


def _sign(params: dict, img_key: str, sub_key: str) -> dict:
    mk = _mixin_key(img_key, sub_key)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    params = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    query = urlencode(params)
    params["w_rid"] = hashlib.md5((query + mk).encode()).hexdigest()
    return params


def _strip_ugc_season(data: dict, bvid: str) -> dict:
    """若响应含 ugc_season，只保留与 bvid 匹配的那一集。"""
    inner = data.get("data") or {}
    ugc = inner.get("ugc_season")
    if not ugc:
        return data

    for section in ugc.get("sections", []):
        matched = [ep for ep in section.get("episodes", []) if ep.get("bvid") == bvid]
        section["episodes"] = matched

    ugc["sections"] = [s for s in ugc["sections"] if s.get("episodes")]
    return data


def fetch_video_metadata(url_or_bvid: str, cookies_path: str = "") -> dict:
    """获取单个视频的原始 API 响应，若属于合集则只保留该视频条目。"""
    bvid = _extract_bvid(url_or_bvid)
    session = requests.Session()
    session.trust_env = False
    session.headers.update(HEADERS)

    if cookies_path:
        _load_cookies(session, cookies_path)

    resp = session.get(
        "https://api.bilibili.com/x/web-interface/view",
        params={"bvid": bvid},
        timeout=30,
    )
    data = resp.json()

    if data.get("code") == 0:
        return _strip_ugc_season(data, bvid)

    # view 接口受风控时尝试 WBI 签名重试
    try:
        img_key, sub_key = _get_wbi_keys(session)
        params = _sign({"bvid": bvid}, img_key, sub_key)
        resp = session.get(
            "https://api.bilibili.com/x/web-interface/view",
            params=params,
            timeout=30,
        )
        return _strip_ugc_season(resp.json(), bvid)
    except Exception:
        return data


def main():
    parser = argparse.ArgumentParser(description="获取 Bilibili 视频原始 metadata")
    parser.add_argument("url", help="视频 URL 或 BV 号")
    parser.add_argument("--cookies", default="", help="bilibili_cookies.txt 路径")
    args = parser.parse_args()

    try:
        result = fetch_video_metadata(args.url, args.cookies)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        print(f"请求失败: {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
