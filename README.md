# Video Metadata Crawler

从 Bilibili / YouTube 批量爬取视频元数据（标题、时长、分类、播放量等），输出为 Excel 文件。支持断点续传、自动去重和多种输入格式。

## 功能特性

- **Bilibili**：通过 B 站 API 爬取，支持 UP 主全部投稿 (`bilibili_up`) 和合集/系列 (`bilibili_series`)
- **YouTube**：通过系统 `yt-dlp` 命令行爬取，支持频道 (`youtube_channel`) 和播放列表 (`youtube_playlist`)
- **断点续传**：每获取 N 条视频自动保存 checkpoint，中断后重启自动从上次位置继续
- **按输入文件隔离 checkpoint**：不同输入文件的 checkpoint 存储在独立子目录下
- **导出模式**：可跳过爬取，直接从已有 checkpoint 导出 Excel
- **截止日期**：Bilibili 和 YouTube 均可配置截止日期，爬取到早于该日期的视频时自动停止

## 项目结构

```
video_metadata_crawler/
├── run.sh                  # 启动脚本 (bash)
├── run.bat                 # 启动脚本 (Windows)
├── requirements.txt        # Python 依赖
├── config/
│   ├── config.yaml         # 主配置文件
│   ├── bilibili_cookies.txt
│   └── youtube_cookies.txt
├── input/
│   ├── example_input.txt   # 输入文件格式说明与示例
│   └── *.txt               # 实际输入文件
├── scripts/
│   ├── crawl_all.py        # 主入口，调度 Bilibili/YouTube 爬虫
│   ├── bilibili_crawler.py # Bilibili API 爬虫
│   ├── youtube_crawler.py  # YouTube yt-dlp 爬虫
│   └── utils.py            # 公共工具（配置、输入解析、checkpoint、Excel 输出）
├── checkpoints/            # 断点续传数据（按输入文件名分子目录）
│   └── {input_stem}/       # 例如 input_yue/
└── results/                # 输出的 Excel 文件
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

YouTube 爬取还需要系统安装 `yt-dlp` 和 `Node.js`：

```bash
# yt-dlp（推荐 pip 安装）
pip install yt-dlp

# Node.js（用于 YouTube JS challenge 解析）
# 从 https://nodejs.org/ 安装
```

### 2. 准备输入文件

创建输入文件（TSV 格式），每行三个字段用 Tab 分隔：

```
名称	类型	ID
```

支持的类型和 ID 格式：

| 类型 | 说明 | ID 示例 |
|------|------|---------|
| `bilibili_up` | B站 UP 主全部投稿 | `39627524` 或 `https://space.bilibili.com/39627524/upload/video` |
| `bilibili_series` | B站合集/系列 | `https://space.bilibili.com/95515699/lists/279423?type=season` |
| `youtube_channel` | YouTube 频道 | `@3blue1brown` 或 `UCYO_jab_esuFRV4b17AJtAw` |
| `youtube_playlist` | YouTube 播放列表 | `PLhQjrBD2T381WAHyx1pq-sBfykqMBI7V4` |

详细格式参见 `input/example_input.txt`。

### 3. 配置

编辑 `config/config.yaml`：

```yaml
input_file: "input/your_input.txt"

output:
  output_dir: "results"
  filename: "video_metadata.xlsx"

cookies:
  youtube: "config/youtube_cookies.txt"
  bilibili: "config/bilibili_cookies.txt"

bilibili:
  page_size: 50
  sleep_interval: 2
  max_retries: 3
  cutoff_date: ""           # 留空不限制，或填 "2025-06-30" 只爬该日期之后的视频

youtube:
  sleep_interval: 8
  cutoff_date: ""           # 同上

checkpoint:
  checkpoint_dir: "checkpoints"
  save_interval: 50
```

### 4. 运行

```bash
# 默认运行
./run.sh

# 指定输入/输出
./run.sh --input input/input_yue.txt --output results/yue_metadata.xlsx

# 仅从 checkpoint 导出 Excel（不爬取）
./run.sh --export-only
```

## 命令行参数

| 参数 | 说明 |
|------|------|
| `--config FILE` | 配置文件路径（默认 `config/config.yaml`） |
| `--input FILE` | 输入文件路径（覆盖配置文件中的 `input_file`） |
| `--output FILE` | 输出文件路径（覆盖配置文件中的输出路径） |
| `--export-only` | 跳过爬取，仅从已有 checkpoint 导出 Excel |

## 输出 Excel 列说明

| 列 | 内容 |
|----|------|
| 视频URL | 视频链接 |
| 频道名 | UP 主名 / YouTube @handle |
| 时长(时分秒) | HH:MM:SS 格式 |
| 时长(小时) | 小数形式 |
| 主题 | 视频分类（B站分区 / YouTube category） |
| 标题 | 视频标题 |
| 来源名称 | 输入文件中定义的名称 |
| 来源类型 | bilibili_up / bilibili_series / youtube_channel / youtube_playlist |
| 平台 | bilibili / youtube |
| 发布时间 | YYYY-MM-DD HH:MM:SS |
| 播放量 | 播放/观看次数 |

## Cookies 配置

### Bilibili

导出浏览器 Cookies 为 Netscape 格式，保存到 `config/bilibili_cookies.txt`。用于访问需要登录才能获取的 API 数据（如 WBI 签名）。

### YouTube

导出浏览器 Cookies 为 Netscape 格式，保存到 `config/youtube_cookies.txt`。用于绕过 YouTube 的 bot 检测。

可使用浏览器扩展（如 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)）导出。

## 断点续传

- Checkpoint 文件按输入文件名存储在独立子目录：`checkpoints/{input_stem}/`
- 例如 `input/input_yue.txt` → `checkpoints/input_yue/`
- 重新运行时自动检测已完成的条目并跳过
- 如需重新爬取某个条目，删除对应的 checkpoint JSON 文件即可
