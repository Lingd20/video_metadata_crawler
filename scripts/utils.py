"""Shared utilities for Video Metadata Crawler.

Contains:
  - Configuration & logging
  - Input file parsing
  - Bilibili ID / URL resolution
  - Checkpoint manager
  - Video deduplication
  - Excel output
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import openpyxl
import yaml
from openpyxl.styles import Alignment, Font, PatternFill

logger = logging.getLogger(__name__)

CHECKPOINT_VERSION = 3


# ---------------------------------------------------------------------------
# Configuration & logging
# ---------------------------------------------------------------------------

def setup_logging(log_level="INFO"):
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_config(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

VALID_TYPES = {"bilibili_up", "bilibili_series", "youtube_channel", "youtube_playlist"}


def parse_input_file(input_path):
    """Parse input file.

    Preferred format: TSV (name\\ttype\\tid).
    Also tolerates whitespace-separated lines (common editing mistakes).
    """
    entries = []
    with open(input_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                parts = [p.strip() for p in line.split("\t") if p.strip() != ""]
            else:
                parts = line.split()
            if len(parts) < 3:
                logger.warning(
                    "Line %d: expected 3 fields (TSV preferred), got %d — skipping",
                    line_no,
                    len(parts),
                )
                continue
            name = " ".join(parts[:-2]).strip()
            type_ = parts[-2].strip()
            id_ = parts[-1].strip()
            if type_ not in VALID_TYPES:
                logger.warning("Line %d: unknown type '%s', valid: %s — skipping", line_no, type_, VALID_TYPES)
                continue
            entries.append({"name": name, "type": type_, "id": id_})
    logger.info("Loaded %d entries from %s", len(entries), input_path)
    return entries


# ---------------------------------------------------------------------------
# Bilibili ID / URL resolution
# ---------------------------------------------------------------------------

def resolve_bilibili_up_mid(raw_id: str) -> str:
    """Extract numeric mid from a raw ID or space.bilibili.com URL."""
    if raw_id.isdigit():
        return raw_id
    parsed = urlparse(raw_id)
    if "bilibili.com" in parsed.netloc:
        parts = [p for p in parsed.path.split("/") if p]
        for p in parts:
            if p.isdigit():
                return p
    raise ValueError(f"Cannot extract bilibili mid from: {raw_id}")


def resolve_bilibili_series(raw_id: str):
    """Return (kind, mid, series_id) from:

    - 'mid/season_id' for 合集/系列 (seasons_archives_list)
    - space list URL: https://space.bilibili.com/{mid}/lists/{list_id}
    - collectiondetail URL: ...collectiondetail?sid=123
    """
    if "/" in raw_id and not raw_id.startswith("http"):
        mid, sid = raw_id.split("/", 1)
        return "season", mid.strip(), sid.strip()
    parsed = urlparse(raw_id)
    if "bilibili.com" in parsed.netloc:
        parts = [p for p in parsed.path.split("/") if p]
        nums = [p for p in parts if p.isdigit()]
        mid = nums[0] if nums else None
        if "lists" in parts:
            try:
                idx = parts.index("lists")
                list_id = parts[idx + 1] if idx + 1 < len(parts) else None
                if mid and list_id and str(list_id).isdigit():
                    qs = parse_qs(parsed.query)
                    url_type = qs.get("type", [None])[0]
                    if url_type == "season":
                        return "season", mid, str(list_id)
                    return "list", mid, str(list_id)
            except ValueError:
                pass
        qs = parse_qs(parsed.query)
        sid = qs.get("sid", [None])[0]
        if mid and sid:
            return "season", mid, sid
    raise ValueError(
        f"Cannot extract bilibili series/list id from: {raw_id}. "
        f"Use 'mid/season_id' or space list URL '/{mid}/lists/{'{list_id}'}'."
    )


# ---------------------------------------------------------------------------
# Checkpoint manager
# ---------------------------------------------------------------------------

class CheckpointManager:
    """JSON-file-based checkpoint for resume support."""

    def __init__(self, checkpoint_dir):
        self.dir = Path(checkpoint_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, entry):
        safe = re.sub(r'[\\/:*?"<>|]', "_", f"{entry['type']}_{entry['name']}_{entry['id']}")
        return self.dir / f"{safe}.json"

    def load(self, entry):
        p = self._path(entry)
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def save(self, entry, data):
        data["checkpoint_version"] = CHECKPOINT_VERSION
        data["last_updated"] = datetime.now().isoformat()
        p = self._path(entry)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(p)

    def is_done(self, entry):
        cp = self.load(entry)
        if not cp:
            return False
        if cp.get("checkpoint_version") != CHECKPOINT_VERSION:
            return False
        return cp.get("done", False)

    def get_videos(self, entry):
        cp = self.load(entry)
        return cp.get("videos", []) if cp else []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate_videos(videos):
    """Deduplicate by video URL, preserving first occurrence order."""
    seen = set()
    result = []
    for v in videos:
        url = v.get("url", "")
        if url and url not in seen:
            seen.add(url)
            result.append(v)
    return result


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------

def format_duration_hms(seconds):
    """Always HH:MM:SS for Excel column 时长(时分秒)."""
    try:
        s = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


def excel_channel_display_name(v: dict) -> str:
    """B站：uploader；YouTube：直接使用 uploader（已在 crawler 中格式化）。"""
    return (v.get("uploader") or "").strip()


# ---------------------------------------------------------------------------
# Excel output
# ---------------------------------------------------------------------------

HEADERS = [
    "视频URL",
    "频道名",
    "时长(时分秒)",
    "时长(小时)",
    "主题",
    "来源名称",
    "来源类型",
    "平台",
    "分类",
    "发布时间",
    "播放量",
]

_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center")


def save_to_excel(videos, output_path):
    """Write video list to a styled Excel workbook."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "视频列表"

    for col, h in enumerate(HEADERS, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN

    for row, v in enumerate(videos, 2):
        source_name = v.get("source_name", "")
        source_type = v.get("source_type", "")
        uploader = v.get("uploader", "")
        if source_type == "bilibili_series" and source_name and uploader:
            suffix = f"_{uploader}"
            if not str(source_name).endswith(suffix):
                source_name = f"{source_name}{suffix}"

        duration_sec = v.get("duration", "")
        try:
            hours = round(float(duration_sec) / 3600.0, 3) if duration_sec != "" else ""
        except (TypeError, ValueError):
            hours = ""

        ws.cell(row=row, column=1, value=v.get("url", ""))
        ws.cell(row=row, column=2, value=excel_channel_display_name(v))
        ws.cell(row=row, column=3, value=format_duration_hms(duration_sec))
        ws.cell(row=row, column=4, value=hours)
        ws.cell(row=row, column=5, value=v.get("title", ""))
        ws.cell(row=row, column=6, value=source_name)
        ws.cell(row=row, column=7, value=source_type)
        ws.cell(row=row, column=8, value=v.get("platform", ""))
        ws.cell(row=row, column=9, value=v.get("category", ""))
        ws.cell(row=row, column=10, value=v.get("publish_time", ""))
        ws.cell(row=row, column=11, value=v.get("view_count", ""))

    preferred_widths = [50, 28, 14, 12, 40, 18, 16, 8, 14, 20, 12]
    for i, w in enumerate(preferred_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    logger.info("Saved %d videos → %s", len(videos), output_path)
