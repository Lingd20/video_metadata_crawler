from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UrlItem:
    url: str
    platform: str  # "youtube" | "bilibili" | "podcast" | "unknown"


# 播客/音频平台域名（子串匹配，全小写）
_PODCAST_DOMAINS: tuple[str, ...] = (
    # SBS Australia
    "sbs.com.au",
    "streamguys1.com",
    # Spotify / Anchor
    "anchor.fm",
    "podcasters.spotify.com",
    # 喜马拉雅
    "ximalaya.com",
    # Podbean
    "podbean.com",
    # 小宇宙FM
    "xiaoyuzhoufm.com",
    # SoundOn
    "soundon.fm",
    # Buzzsprout
    "buzzsprout.com",
    # Omny FM
    "omny.fm",
    # Subsplash
    "subsplash.com",
    # Wavpub
    "wavpub.com",
    # Acast
    "acast.com",
    # SoundCloud
    "soundcloud.com",
    # RTHK 香港电台
    "rthk.hk",
    # Firstory
    "firstory.me",
    "fstry.me",
    # 荔枝FM
    "lizhi.fm",
    # Pod.co
    "pod.co",
    # RedCircle
    "redcircle.com",
    # Podtrac (redirect proxy)
    "podtrac.com",
)


def detect_platform(url: str) -> str:
    u = (url or "").strip().lower()
    if not u:
        return "unknown"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    if any(d in u for d in _PODCAST_DOMAINS):
        return "podcast"
    return "unknown"


def split_urls_by_platform(urls: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"youtube": [], "bilibili": [], "podcast": [], "unknown": []}
    for u in urls:
        p = detect_platform(u)
        out.setdefault(p, []).append(u)
    return out

