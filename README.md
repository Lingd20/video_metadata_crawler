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
| `youtube_channel` | YouTube 频道 | `@3blue1brown` 或 `UCYO_jab_esuFRV4b17AJtAw` 或 `https://www.youtube.com/@3blue1brown` |
| `youtube_playlist` | YouTube 播放列表 | `PLzZol22jG6Oeal6priaLn9vUzfQ3l6ZaI` 或 `https://www.youtube.com/playlist?list=PLzZol22jG6Oeal6priaLn9vUzfQ3l6ZaI` |

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
  cutoff_date: ""           # 留空不限制，或填 "2024-01-01" 只爬该日期之后的视频

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

**如何导出 Cookies：(以youtube为例，bilibili同理)**

1. 建议使用 **Google Chrome** 浏览器
2. 登录 [youtube.com](https://www.youtube.com) 
3. 跳转到 <https://www.youtube.com/robots.txt>（确保当前处于已登录状态）
4. 使用浏览器插件 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) 导出当前页面 Cookies，保存为 **Netscape 格式**文件 `cookies.txt`
5. 将导出的文件覆盖到 `config/youtube_cookies.txt`
6. **立刻退出 YouTube 账号**，否则 Cookies 会自动刷新导致导出的文件失效

### 常见问题

**Q: yt-dlp 报错视频 not available / 超出速率限制 / 其他异常？**

请按以下顺序尝试：

1. **更换 Cookies**：重新按上述步骤导出，注意导出完成后**立即退出账号**且不要再登录
2. **更换 IP 节点**：切换代理/VPN 节点，并检查 `curl -I youtube.com` 是否正常返回
3. **升级 yt-dlp**：`pip install -U yt-dlp`
4. **更换 YouTube 账号**：如果以上都无效，尝试使用新的 Google 账号导出 Cookies

## 断点续传

- Checkpoint 文件按输入文件名存储在独立子目录：`checkpoints/{input_stem}/`
- 例如 `input/input_yue.txt` → `checkpoints/input_yue/`
- 重新运行时自动检测已完成的条目并跳过
- 如需重新爬取某个条目，删除对应的 checkpoint JSON 文件即可
