#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def extract_urls_from_excels(excel_paths: list[str]) -> list[str]:
    """读取一个或多个 xlsx 的“视频URL”列，合并去重并保序返回。"""
    try:
        import openpyxl
    except Exception as e:
        raise SystemExit(f"缺少依赖 openpyxl，请先安装 requirements.txt。原始错误: {e}")

    if not excel_paths:
        return []

    urls: list[str] = []
    seen: set[str] = set()

    for p in excel_paths:
        xlsx = Path(p)
        if not xlsx.exists():
            raise SystemExit(f"Excel 不存在: {xlsx}")

        wb = openpyxl.load_workbook(str(xlsx), read_only=True, data_only=True)
        ws = wb.active

        header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        try:
            idx = header.index("视频URL")
        except ValueError:
            raise SystemExit(f"未找到列 '视频URL'。文件: {xlsx}；表头为: {header}")

        for row in ws.iter_rows(min_row=2, values_only=True):
            url = row[idx]
            if not url:
                continue
            u = str(url).strip()
            if not u or u in seen:
                continue
            seen.add(u)
            urls.append(u)

    return urls

