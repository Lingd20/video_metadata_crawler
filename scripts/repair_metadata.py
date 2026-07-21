#!/usr/bin/env python3
"""修复 metadata Excel：平台归一化、补全来源字段、统一发布时间格式。"""

from __future__ import annotations

import argparse
import re
import shutil
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import openpyxl

# 与 scripts/url_crawl/utils.py HEADERS 对齐
COL_URL = "视频URL"
COL_CHANNEL = "频道名"
COL_SOURCE_NAME = "来源名称"
COL_SOURCE_TYPE = "来源类型"
COL_PLATFORM = "平台"
COL_PUBLISH = "发布时间"

_VALID_PLATFORMS = frozenset({"youtube", "bilibili", "podcast"})

_DASH_DT_RE = re.compile(
    r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?\s*$"
)
_SLASH_DT_RE = re.compile(
    r"^\s*(\d{4})/(\d{1,2})/(\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?\s*$"
)


def _is_blank(v) -> bool:
    if v is None:
        return True
    return str(v).strip() == ""


def normalize_platform(raw) -> str:
    """平台仅允许 youtube / bilibili / podcast。"""
    s = str(raw or "").strip()
    if s == "B站":
        return "bilibili"
    sl = s.lower()
    if sl == "bilibili":
        return "bilibili"
    if sl == "youtube":
        return "youtube"
    return "podcast"


def default_source_type(platform: str) -> str:
    if platform == "bilibili":
        return "bilibili_up"
    if platform == "youtube":
        return "youtube_up"
    return "podcast"


def _dt_from_parts(y: int, mo: int, d: int, h: int, mi: int, s: int) -> datetime:
    return datetime(y, mo, d, h, mi, s)


def parse_publish_datetime(raw) -> datetime | None:
    if raw is None or _is_blank(raw):
        return None
    if isinstance(raw, datetime):
        return raw

    s = str(raw).strip()
    if not s:
        return None

    if isinstance(raw, (int, float)):
        try:
            from openpyxl.utils.datetime import from_excel

            return from_excel(float(raw))
        except Exception:
            pass

    m = _DASH_DT_RE.match(s)
    if m:
        y, mo, d, h, mi, sec = m.groups()
        return _dt_from_parts(
            int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(sec or 0)
        )

    m = _SLASH_DT_RE.match(s)
    if m:
        y, mo, d, h, mi, sec = m.groups()
        return _dt_from_parts(
            int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(sec or 0)
        )

    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def format_publish_datetime(dt: datetime) -> str:
    """目标格式示例：2024/5/17 21:07:14（月/日/时不补前导零，分秒两位）。"""
    return f"{dt.year}/{dt.month}/{dt.day} {dt.hour}:{dt.minute:02d}:{dt.second:02d}"


def repair_workbook(ws) -> dict[str, int]:
    header = [c.value for c in ws[1]]
    col = {h: i + 1 for i, h in enumerate(header) if h}

    required = [COL_PLATFORM, COL_CHANNEL, COL_SOURCE_NAME, COL_SOURCE_TYPE, COL_PUBLISH]
    missing = [c for c in required if c not in col]
    if missing:
        raise SystemExit(f"缺少列: {missing}；当前表头: {header}")

    stats = {
        "rows": 0,
        "platform_changed": 0,
        "source_name_filled": 0,
        "source_type_filled": 0,
        "publish_formatted": 0,
        "publish_parse_failed": 0,
    }

    for row_idx in range(2, ws.max_row + 1):
        url = ws.cell(row=row_idx, column=col[COL_URL]).value if COL_URL in col else None
        if _is_blank(url):
            continue

        stats["rows"] += 1

        plat_cell = ws.cell(row=row_idx, column=col[COL_PLATFORM])
        old_plat = plat_cell.value
        new_plat = normalize_platform(old_plat)
        if str(old_plat or "").strip() != new_plat:
            plat_cell.value = new_plat
            stats["platform_changed"] += 1

        ch = ws.cell(row=row_idx, column=col[COL_CHANNEL]).value
        sn_cell = ws.cell(row=row_idx, column=col[COL_SOURCE_NAME])
        if _is_blank(sn_cell.value) and not _is_blank(ch):
            sn_cell.value = str(ch).strip()
            stats["source_name_filled"] += 1

        st_cell = ws.cell(row=row_idx, column=col[COL_SOURCE_TYPE])
        if _is_blank(st_cell.value):
            st_cell.value = default_source_type(new_plat)
            stats["source_type_filled"] += 1

        pub_cell = ws.cell(row=row_idx, column=col[COL_PUBLISH])
        raw_pub = pub_cell.value
        if _is_blank(raw_pub):
            continue

        dt = parse_publish_datetime(raw_pub)
        if dt is None:
            stats["publish_parse_failed"] += 1
            continue

        formatted = format_publish_datetime(dt)
        if str(raw_pub).strip() != formatted:
            pub_cell.value = formatted
            stats["publish_formatted"] += 1

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="修复 metadata Excel 平台/来源/发布时间字段")
    parser.add_argument(
        "excel",
        nargs="?",
        default="results/yue_0415.xlsx",
        help="待修复的 xlsx 路径（默认: results/yue_0415.xlsx）",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="",
        help="输出路径（默认覆盖原文件）",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="覆盖原文件时不创建 .bak 备份",
    )
    args = parser.parse_args()

    src = Path(args.excel)
    if not src.exists():
        raise SystemExit(f"文件不存在: {src}")

    out = Path(args.output) if args.output else src
    in_place = out.resolve() == src.resolve()

    if in_place and not args.no_backup:
        bak = src.with_suffix(src.suffix + ".bak")
        shutil.copy2(src, bak)
        print(f"已备份: {bak}")

    wb = openpyxl.load_workbook(str(src))
    ws = wb.active
    stats = repair_workbook(ws)
    wb.save(str(out))

    print(f"已写入: {out}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
