#!/usr/bin/env python3
"""合并两个及以上 metadata Excel（results 同款结构），按视频URL去重并统计重合条目。

参考格式（results/*.xlsx）：
  工作表名: 视频列表
  列: 视频URL | 频道名 | 时长(时分秒) | 时长(小时) | 主题 | 标题 |
       来源名称 | 来源类型 | 平台 | 发布时间 | 播放量

用法:
  python scripts/merge_metadata_xlsx.py \\
    --inputs results/ae_metadata.xlsx results/ae_2_metadata.xlsx results/ae_3_meatadata.xlsx \\
    --output results/ae_merged.xlsx

说明:
  - 去重键: 视频URL（空 URL 行会被跳过）
  - 保留首次出现的行（按 --inputs 顺序）
  - 输出重合统计: 出现在 ≥2 个输入文件中的 URL 数，以及两两交集
  - 输出时长统计: 每个读入文件与最终合并结果的总时长（小时）
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

HEADERS = [
    "视频URL",
    "频道名",
    "时长(时分秒)",
    "时长(小时)",
    "主题",
    "标题",
    "来源名称",
    "来源类型",
    "平台",
    "发布时间",
    "播放量",
]

_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center")
_CENTER_ALIGN = Alignment(horizontal="center", vertical="center")
_COL_WIDTHS = [60, 28, 15, 15, 35, 75, 50, 30, 15, 30, 12]


def _normalize_url(url: object) -> str:
    return str(url or "").strip()


def _parse_hms_to_hours(hms: object) -> float:
    """Parse H:MM:SS / HH:MM:SS / M:SS into hours."""
    s = str(hms or "").strip()
    if not s:
        return 0.0
    parts = s.split(":")
    try:
        if len(parts) == 3:
            h, m, sec = int(parts[0]), int(parts[1]), int(float(parts[2]))
            return (h * 3600 + m * 60 + sec) / 3600.0
        if len(parts) == 2:
            m, sec = int(parts[0]), int(float(parts[1]))
            return (m * 60 + sec) / 3600.0
    except (TypeError, ValueError):
        return 0.0
    return 0.0


def _row_hours(row: tuple) -> float:
    """Prefer 时长(小时) column; fallback to 时长(时分秒)."""
    # HEADERS: [URL, channel, hms, hours, ...]
    hours_val = row[3] if len(row) > 3 else ""
    try:
        if hours_val != "" and hours_val is not None:
            return float(hours_val)
    except (TypeError, ValueError):
        pass
    return _parse_hms_to_hours(row[2] if len(row) > 2 else "")


def _sum_hours(rows: list[tuple]) -> float:
    return sum(_row_hours(r) for r in rows)


def _fmt_hours(h: float) -> str:
    return f"{h:.3f}h"


def _read_xlsx_rows(path: Path) -> list[tuple]:
    """读取工作表行（不含表头），返回 list[tuple]（按 HEADERS 对齐，不足补空）。"""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = next(rows_iter)
    except StopIteration:
        wb.close()
        return []

    header_list = [str(h or "").strip() for h in (header or ())]
    # 若表头与标准一致，直接按列序；否则尽量按列名映射
    if header_list[: len(HEADERS)] == HEADERS:
        col_map = {i: i for i in range(len(HEADERS))}
    else:
        name_to_idx = {name: i for i, name in enumerate(header_list)}
        col_map = {}
        for i, name in enumerate(HEADERS):
            if name in name_to_idx:
                col_map[i] = name_to_idx[name]
            else:
                col_map[i] = None

    out: list[tuple] = []
    for raw in rows_iter:
        raw = raw or ()
        mapped = []
        for i in range(len(HEADERS)):
            src_i = col_map.get(i)
            if src_i is None or src_i >= len(raw):
                mapped.append("")
            else:
                mapped.append("" if raw[src_i] is None else raw[src_i])
        url = _normalize_url(mapped[0])
        if not url:
            continue
        mapped[0] = url
        out.append(tuple(mapped))
    wb.close()
    return out


def _write_xlsx(rows: list[tuple], output_path: Path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "视频列表"

    for col, h in enumerate(HEADERS, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN

    for ridx, row in enumerate(rows, 2):
        for cidx, val in enumerate(row, 1):
            cell = ws.cell(row=ridx, column=cidx, value=val)
            cell.alignment = _CENTER_ALIGN

    for i, w in enumerate(_COL_WIDTHS, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)


def main():
    ap = argparse.ArgumentParser(description="合并多个 metadata xlsx，按视频URL去重并统计重合")
    ap.add_argument(
        "--inputs",
        nargs="+",
        required=True,
        help="两个及以上待合并的 xlsx 路径",
    )
    ap.add_argument(
        "--output",
        required=True,
        help="合并去重后的输出 xlsx 路径",
    )
    args = ap.parse_args()

    paths = [Path(p) for p in args.inputs]
    if len(paths) < 2:
        raise SystemExit("请至少提供两个 xlsx：--inputs a.xlsx b.xlsx ...")

    for p in paths:
        if not p.exists():
            raise SystemExit(f"文件不存在: {p}")

    # url -> 首次出现的行
    first_row: dict[str, tuple] = {}
    # url -> 出现过的文件名集合
    url_sources: dict[str, set[str]] = defaultdict(set)
    # 每个文件内的唯一 URL 集合
    per_file_urls: dict[str, set[str]] = {}
    # 每个文件的行（用于时长统计）
    per_file_rows: dict[str, list[tuple]] = {}

    total_raw = 0
    for p in paths:
        rows = _read_xlsx_rows(p)
        total_raw += len(rows)
        file_urls: set[str] = set()
        for row in rows:
            url = row[0]
            file_urls.add(url)
            url_sources[url].add(p.name)
            if url not in first_row:
                first_row[url] = row
        per_file_urls[p.name] = file_urls
        per_file_rows[p.name] = rows
        hours = _sum_hours(rows)
        print(f"[读入] {p.name}: {len(rows)} 条，总时长 {_fmt_hours(hours)}")

    merged = list(first_row.values())
    out = Path(args.output)
    _write_xlsx(merged, out)
    merged_hours = _sum_hours(merged)

    # ---- 重合统计 ----
    overlap_urls = [u for u, srcs in url_sources.items() if len(srcs) >= 2]
    only_one = len(url_sources) - len(overlap_urls)
    overlap_hours = _sum_hours([first_row[u] for u in overlap_urls if u in first_row])

    print()
    print("=" * 60)
    print("合并结果")
    print("=" * 60)
    print(f"输入文件数       : {len(paths)}")
    print(f"读入总行数       : {total_raw}")
    print(f"去重后条目数     : {len(merged)}")
    print(f"去重后总时长     : {_fmt_hours(merged_hours)}")
    print(f"因重复剔除       : {total_raw - len(merged)}")
    print(f"仅出现在 1 个文件: {only_one}")
    print(f"重合条目数(≥2文件): {len(overlap_urls)}")
    print(f"重合条目总时长   : {_fmt_hours(overlap_hours)}")
    print(f"输出             : {out}")

    print()
    print("各文件统计（条数 / 总时长）:")
    for name, urls in per_file_urls.items():
        hours = _sum_hours(per_file_rows[name])
        print(f"  - {name}: {len(urls)} 条, {_fmt_hours(hours)}")
    print(f"  - [合并结果] {out.name}: {len(merged)} 条, {_fmt_hours(merged_hours)}")

    if len(paths) >= 2:
        print()
        print("两两交集（重合 URL 数 / 重合时长）:")
        names = [p.name for p in paths]
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                inter_urls = per_file_urls[a] & per_file_urls[b]
                inter_hours = _sum_hours([first_row[u] for u in inter_urls if u in first_row])
                print(f"  - {a} ∩ {b}: {len(inter_urls)} 条, {_fmt_hours(inter_hours)}")

    # 出现在全部输入文件中的 URL
    if len(paths) >= 2:
        all_inter = set.intersection(*(per_file_urls[p.name] for p in paths))
        all_inter_hours = _sum_hours([first_row[u] for u in all_inter if u in first_row])
        print()
        print(
            f"出现在全部 {len(paths)} 个文件中的 URL 数: {len(all_inter)}，"
            f"总时长 {_fmt_hours(all_inter_hours)}"
        )


if __name__ == "__main__":
    main()
