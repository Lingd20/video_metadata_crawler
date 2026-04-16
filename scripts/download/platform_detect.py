from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UrlItem:
    url: str
    platform: str  # "youtube" | "bilibili" | "unknown"


def detect_platform(url: str) -> str:
    u = (url or "").strip().lower()
    if not u:
        return "unknown"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    return "unknown"


def split_urls_by_platform(urls: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"youtube": [], "bilibili": [], "unknown": []}
    for u in urls:
        p = detect_platform(u)
        out.setdefault(p, []).append(u)
    return out

