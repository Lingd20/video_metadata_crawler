# Video Metadata Crawler

从 Bilibili / YouTube 批量爬取视频元数据（标题、时长、分类、播放量等），输出为 Excel 文件。支持断点续传、自动去重和多种输入格式。

## 功能特性

- **Bilibili**：通过 B 站 API 爬取，支持 UP 主全部投稿 (`bilibili_up`) 和合集/系列 (`bilibili_series`)
- **YouTube**：通过系统 `yt-dlp` 命令行爬取，支持频道 (`youtube_channel`) 和播放列表 (`youtube_playlist`)
- **断点续传**：每获取 N 条视频自动保存 checkpoint，中断后重启自动从上次位置继续
- **两阶段抓取（YouTube / Bilibili）**：先缓存完整「待处理视频 URL 列表」，再按列表逐条拉取详情；续跑时默认不重新拉列表
- **失败熔断**：单条 URL 连续失败达到阈值后写入失败列表并跳过，避免无限重试；`--refresh-pending-urls` 重拉列表时**不会**清空失败列表与失败计数
- **按输入文件隔离 checkpoint**：不同输入文件的 checkpoint 存储在独立子目录下
- **导出模式**：可跳过爬取，直接从已有 checkpoint 导出 Excel
- **截止日期**：Bilibili 和 YouTube 均可配置截止日期，爬取到早于该日期的视频时自动停止
- **关键词过滤（可选）**：在 input 第 4 列填写 `keyword_filter`，爬取时根据「标题或简介」命中关键词且不命中负向关键词才会写入结果

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
├── docs/
│   └── pt_dialect.md       # 巴西葡语口音区划分
├── scripts/
│   ├── youtube_dialect.py  # 按口音区检索 YouTube，并导出视频元数据
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

创建输入文件（TSV 格式），每行至少 3 个字段用 Tab 分隔，**可选**增加第 4 列 `keyword_filter`：

```
名称	类型	ID	keyword_filter(可选)
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

# 关键词过滤（可选）
# input 第 4 列填写 keyword_filter（profile 名称），然后用该 profile 在这里配置：
keyword_filters: {}

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

# 强制重新拉取各条目的「待处理视频 URL 列表」（详见下文「断点续传」）
./run.sh --refresh-pending-urls
```

## 命令行参数

| 参数 | 说明 |
|------|------|
| `--config FILE` | 配置文件路径（默认 `config/config.yaml`） |
| `--input FILE` | 输入文件路径（覆盖配置文件中的 `input_file`） |
| `--output FILE` | 输出文件路径（覆盖配置文件中的输出路径） |
| `--export-only` | 跳过爬取，仅从已有 checkpoint 导出 Excel |
| `--refresh-pending-urls` | 强制重新拉取待处理 URL 列表；**保留** `*_failed_urls` 与 `*_url_fail_counts`（见下文） |

`run.sh` 会将 `--refresh-pending-urls` 原样传给 `url_crawl/crawl_all.py`；直接使用 Python 时：`python scripts/url_crawl/crawl_all.py --config ... --refresh-pending-urls`。

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

## 巴西葡语方言检索

脚本 `scripts/youtube_dialect.py` 按 `docs/pt_dialect.md` 的口音区搜索 YouTube，先发现频道和视频，再拉取与 `run.sh` 相同的视频元数据。默认地区是东南 **Sudeste**（SP、RJ、MG、ES），不限制发布日期。

检索词覆盖街采、地方议会、本地谈话，以及方言标记词（如 `uai sô`、`gírias cariocas`、`sotaque caipira`）和内陆城市。每个检索词先取 100 条，丢掉关键词匹配度低的结果；整页命中率 ≥ 0.45，或本页最后 20 条命中率 ≥ 0.30，则继续翻页，每种排序最多 500 条。视频搜索默认按相关性和上传日期各跑一遍。

### 运行

在仓库根目录执行。需要已安装的 `yt-dlp`、Node.js，以及 `config/youtube_cookies.txt`（没有该文件时仍会跑，但更容易被 YouTube 限流）。

```bash
python scripts/youtube_dialect.py --region Sudeste --scheme geo5 --output results/dialect_Sudeste_channels.csv --metadata-out results/dialect_Sudeste_metadata.xlsx --video-urls-out results/dialect_Sudeste_video_urls.txt --channels-tsv-out results/dialect_Sudeste_channels.tsv
```

列出可用地区后退出：

```bash
python scripts/youtube_dialect.py --list-regions
```

其他地区把 `--region` 换成 `Norte`、`Nordeste`、`Sul`、`Centro-Oeste`，或中文「东南」，或州码（`--states SP RJ`）。`--scheme accent7` 时检索范围按 7 类口音区，但元数据表里的「五类地区」列始终按 5 类行政大区填写。

不要另加 `--max-per-query 40` 或 `--days-back 90`，否则会盖掉当前默认（起始 100 条、不限日期）。

### 输出

| 文件 | 内容 |
|------|------|
| `results/dialect_<region>_metadata.xlsx` | 视频元数据。列为上文 Excel 的 11 列，末尾另加 **州**、**五类地区** |
| `results/dialect_<region>_metadata.partial.jsonl` | 元数据检查点。中断后重跑会跳过其中已有 URL |
| `results/dialect_<region>_channels.csv` | 频道汇总，含命中检索词和州 |
| `results/dialect_<region>_video_urls.txt` | 进入 Excel 的视频 URL |
| `results/dialect_<region>_channels.tsv` | `crawl_all` 可用的频道列表：`name<TAB>youtube_channel<TAB>url` |

**州** 来自命中该视频的检索词（多个州写成 `ES+SP`）。**五类地区** 由州映射到 Norte / Nordeste / Sudeste / Sul / Centro-Oeste。

这一步只拉元数据，不下载视频。日志里若出现 `Video unavailable` 或 `No video formats found`，通常是 YouTube 没有给出可播放格式，标题、时长和播放量仍会写入。

### 常用参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--region` | `Sudeste` | 地区名、中文别名或单个州码 |
| `--scheme` | `geo5` | `geo5` 为 5 类行政大区，`accent7` 为 7 类口音区 |
| `--max-per-query` | `100` | 每种排序的起始页大小 |
| `--max-results` | `500` | 每种排序的条数上限；`0` 表示只按匹配率停（内部上限 2000） |
| `--hit-rate` / `--tail-rate` | `0.45` / `0.30` | 整页或页尾命中率达到该值则继续翻页 |
| `--min-keyword-ratio` | `0.5` | 标题或频道名至少覆盖检索词实词的比例 |
| `--search-sorts` | `relevance,date` | 还可加 `views`；按播放量更容易召回全国热门 |
| `--days-back` | `0` | 发布日期窗口（天）；`0` 为不限制 |
| `--min-duration` / `--max-duration` | `20` / `14400` | 时长过滤（秒）。搜索结果里没有时长时，等完整元数据返回后再滤一次 |
| `--skip-metadata` | 关 | 只发现 URL，不拉时长、播放量和发布时间 |
| `--cookies` | `config/youtube_cookies.txt` | 文件存在时自动使用 |
| `--proxy` | 空 | 例如 `http://127.0.0.1:7993`。未传时 yt-dlp 仍会读取环境变量里的代理 |

### 下载音频

元数据 Excel 可直接交给 `download.sh`：

```bash
./download.sh results/dialect_Sudeste_metadata.xlsx --out /e/datasets/pt_dialect --task-name sudeste --cookies-youtube config/youtube_cookies.txt --subtitle-mode audio_only -j 4
```

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

### 待处理 URL 列表（YouTube / Bilibili 一致）

1. **首次或需要拉列表时**：把该频道/播放列表/合集下的视频页 URL 完整写入 checkpoint（YouTube：`youtube_pending_urls`；Bilibili：`bilibili_pending_urls`）。
2. **之后每次运行**：默认**沿用** checkpoint 中的列表，按顺序补全 `videos`；已在 `videos` 中的 URL 会快速跳过。
3. **何时会重新拉列表**：无列表字段、或列表为空且该条目尚未标记完成时；或使用 **`--refresh-pending-urls`** 强制刷新（例如频道上新视频后希望更新列表）。
4. **使用 `--refresh-pending-urls` 时**：会替换 `*_pending_urls`，但**不会清空** `youtube_failed_urls` / `youtube_url_fail_counts` 或 B 站侧 `bilibili_failed_urls` / `bilibili_url_fail_counts`（仍失败的 URL 会继续跳过）。

### 单条失败与失败列表

- 某条视频 URL 拉取元数据（YouTube：`yt-dlp -j`；Bilibili：`view` 接口）**连续失败达到 3 次**后，会记入失败列表（YouTube：`youtube_failed_urls`；Bilibili：`bilibili_failed_urls`），后续运行不再对该 URL 重试。
- 判定「条目是否完成」时：待处理列表中的每条 URL 须**要么**已出现在 `videos` 中，**要么**在失败列表中（用户跳过、触发截止日期等例外以代码中的 `done` 逻辑为准）。

### 历史字段说明

- 旧版 checkpoint 中的 **`youtube_list_cursor`** 等游标字段**当前代码已不再读取**，可视为无效；以 `youtube_pending_urls` 为准。
