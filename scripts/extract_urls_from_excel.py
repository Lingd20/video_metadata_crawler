#!/usr/bin/env python3
"""从 Excel 中提取“视频URL”列并输出为文本文件。

用法:
    python scripts/extract_urls_from_excel.py <excel.xlsx> <out.txt>
"""

import sys
from pathlib import Path


def main():
    if len(sys.argv) != 3:
        print("用法: python scripts/extract_urls_from_excel.py <excel.xlsx> <out.txt>", file=sys.stderr)
        raise SystemExit(2)

    xlsx = sys.argv[1]
    out = sys.argv[2]

    try:
        import openpyxl
    except Exception as e:
        raise SystemExit(f"缺少依赖 openpyxl，请先安装 requirements.txt。原始错误: {e}")

    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    ws = wb.active

    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    try:
        idx = header.index("视频URL")
    except ValueError:
        raise SystemExit(f"未找到列 '视频URL'。表头为: {header}")

    urls = []
    seen = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        url = row[idx]
        if not url:
            continue
        url = str(url).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)

    Path(out).write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    print(f"URLs: {len(urls)} -> {out}")


if __name__ == "__main__":
    main()

