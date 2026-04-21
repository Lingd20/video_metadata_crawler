#!/usr/bin/env python3
"""从 xlsx 读取 URL 与行级元信息。

本阶段不再调用 yt-dlp -j 抓取 metadata，所有 metainfo 直接来自 Excel 表格
（由 url_crawl 阶段已写入）。下游脚本按 URL 索引元信息，下载完成后写入
metadata.jsonl。

Excel 表头约定（与 scripts/url_crawl/utils.py:HEADERS 对齐）：
    视频URL, 频道名, 时长(时分秒), 时长(小时),
    主题, 标题, 来源名称, 来源类型, 平台, 发布时间, 播放量
"""

from __future__ import annotations

import re
from pathlib import Path


_HMS_RE = re.compile(r"^\s*(\d+):(\d{1,2}):(\d{1,2})\s*$")


def _to_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_duration_seconds(hms: str | None, hours: float | None) -> float | None:
    """优先使用 `时长(小时)`（浮点），其次解析 `时长(时分秒)` 字符串。"""
    if hours is not None:
        try:
            return round(float(hours) * 3600.0, 2)
        except (TypeError, ValueError):
            pass
    if hms:
        m = _HMS_RE.match(str(hms))
        if m:
            h, mm, s = (int(x) for x in m.groups())
            return float(h * 3600 + mm * 60 + s)
    return None


def _row_to_metainfo(row: dict) -> dict:
    """把 Excel 一行映射成 metadata.jsonl 里 metainfo 的扁平字典。"""
    return {
        "url": _to_str(row.get("视频URL")) or "",
        "platform": _to_str(row.get("平台")),
        "channel": _to_str(row.get("频道名")),
        "title": _to_str(row.get("标题")),
        "topic": _to_str(row.get("主题")),
        "source_name": _to_str(row.get("来源名称")),
        "source_type": _to_str(row.get("来源类型")),
        "publish_date": _to_str(row.get("发布时间")),
        "view_count": _to_int(row.get("播放量")),
    }


def extract_urls_from_excels(excel_paths: list[str]) -> list[str]:
    """读取一个或多个 xlsx 的“视频URL”列，合并去重并保序返回。"""
    urls, _ = extract_rows_from_excels(excel_paths)
    return urls


def extract_rows_from_excels(
    excel_paths: list[str],
) -> tuple[list[str], dict[str, dict]]:
    """返回 (urls, metadata_by_url)。

    - urls：按出现顺序保序、URL 去重后的列表。
    - metadata_by_url[url] = {"duration_s": float|None, "metainfo": {...}}，
      其中 metainfo 已是后续写入 metadata.jsonl 的最终形态。
    """
    try:
        import openpyxl
    except Exception as e:
        raise SystemExit(f"缺少依赖 openpyxl，请先安装 requirements.txt。原始错误: {e}")

    if not excel_paths:
        return [], {}

    urls: list[str] = []
    seen: set[str] = set()
    meta_by_url: dict[str, dict] = {}

    for p in excel_paths:
        xlsx = Path(p)
        if not xlsx.exists():
            raise SystemExit(f"Excel 不存在: {xlsx}")

        wb = openpyxl.load_workbook(str(xlsx), read_only=True, data_only=True)
        ws = wb.active

        header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        try:
            url_idx = header.index("视频URL")
        except ValueError:
            raise SystemExit(f"未找到列 '视频URL'。文件: {xlsx}；表头为: {header}")

        for row in ws.iter_rows(min_row=2, values_only=True):
            url_cell = row[url_idx] if url_idx < len(row) else None
            if not url_cell:
                continue
            u = str(url_cell).strip()
            if not u or u in seen:
                continue

            row_dict = {h: (row[i] if i < len(row) else None) for i, h in enumerate(header)}
            metainfo = _row_to_metainfo(row_dict)
            metainfo["url"] = u

            seen.add(u)
            urls.append(u)
            meta_by_url[u] = {
                "duration_s": _parse_duration_seconds(
                    row_dict.get("时长(时分秒)"),
                    _to_float(row_dict.get("时长(小时)")),
                ),
                "metainfo": metainfo,
            }

    return urls, meta_by_url
