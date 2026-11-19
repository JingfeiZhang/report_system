# event_sync — 事件同步辅助程序

将列表中的事件名称同步到数据库中的 `daily_topics` 表，并可在写入完成后按配置启动爬虫。

## 功能

1. 从事件接口拉取事件列表
2. 提取每条事件的 `title`，按 `event_date` 分组
3. 合并写入数据库表 `daily_topics`（支持 MySQL / 达梦；可关）
4. 全部日期写入成功后，按 `CRAWLER_BACKEND` 启动爬虫：
   - **cli**：本地 `MindSpider/main.py --deep-sentiment`（读 `daily_topics`）
   - **http**：调用本机/外网 `mindspider-api`（`POST /api/v1/crawl/tasks`）

## 目录结构

```text
event_sync/
├── README.md
├── config.txt              # 运行配置（接口、数据库、爬虫）
├── main.py                 # 程序入口
├── api_client.py           # 事件拉取（HTTP）
├── db_writer.py            # 写入 daily_topics（MySQL / 达梦）
├── crawler_launcher.py     # 启动爬虫（cli / http）
└── fixtures/
    └── test_events.json    # 本地示例事件数据（接口不可用时使用）
```

## 运行流程

读 config.txt  
  → 从接口拉取事件  
  → 按 event_date 分组 title  
  →（可选）合并写入 daily_topics  
  → 若 CRAWLER_ENABLED=true，按日期顺序启动爬虫  
  → 日志输出到控制台与 logs/event_sync_YYYYMMDD.log

## 配置说明（config.txt）

| 配置项 | 说明 |
|--------|------|
| API_BASE | 开放事件接口前缀，如 `http://host:8250/api/v1` |
| API_TIMEOUT | HTTP 超时秒数 |
| WRITE_DAILY_TOPICS | `true`/`false`；http 模式可关，仅用标题直调 API |
| DB_* | 数据库连接；cli 模式或写库时需要 |
| CRAWLER_ENABLED | 是否在写库后启动爬虫 |
| CRAWLER_BACKEND | `cli` 或 `http` |
| MINDSPIDER_API_BASE | http 模式：如 `http://127.0.0.1:8082/api/v1` |
| CRAWLER_POLL_INTERVAL / CRAWLER_POLL_TIMEOUT | http 轮询间隔与最长等待（秒） |
| CRAWLER_LOGIN_TYPE / CRAWLER_PACK_FORMAT | 登录类型；打包：`dir`（推荐，仅文件夹）/ `zip` / `tar.gz` |
| CRAWLER_DOWNLOAD / CRAWLER_DOWNLOAD_DIR | 成功后是否保存结果及目录 |
| CRAWLER_LOCAL_EXPORTS_DIR | 同机 API `exports` 路径；优先复制文件夹 |
| CRAWLER_KEEP_ARCHIVE | `false`（默认）解压后删 zip |
| CRAWLER_PLATFORMS | 平台列表，逗号分隔 |
| CRAWLER_MAX_KEYWORDS | cli：`--max-keywords`；http：每日期最多提交几个 title |
| CRAWLER_MAX_NOTES | 每关键词最大内容数 |

### 与 MindSpider 根目录 .env 的关系（仅 cli）

- 本程序写 daily_topics：只用 `event_sync/config.txt` 的 `DB_*`
- MindSpider CLI 读关键词 / 写结果：用项目根目录 `.env`
- 启用 **cli** 爬虫时，二者必须指向同一库

### http 模式注意

- 不依赖本机 `MindSpider/main.py`，依赖已启动的 `mindspider-api`（健康检查：`GET .../health`）
- 关键词直接来自当日事件 `title`，不强制依赖 `daily_topics`
- 结果默认落到 **`源代码/exports/event_sync/{年}/{月}/{日}/{YYYYMMDD}-{事件id}/`**（在 BettaFish_Modify **上一级**，避免打镜像时带上爬取数据）。配置项：`CRAWLER_DOWNLOAD_DIR=../../exports/event_sync`

## 运行

```text
cd event_sync
conda activate env_name

# 只同步写库（默认 CRAWLER_ENABLED=false）
python main.py --start-date 2024-01-01 --end-date 2024-01-31

# 接源码 MindSpider
#   CRAWLER_ENABLED=true
#   CRAWLER_BACKEND=cli
#   WRITE_DAILY_TOPICS=true，且 .env 与 config DB 一致

# 接 mindspider-api :8082
#   CRAWLER_ENABLED=true
#   CRAWLER_BACKEND=http
#   MINDSPIDER_API_BASE=http://127.0.0.1:8082/api/v1
#   无库时可 WRITE_DAILY_TOPICS=false
```

main.py 参数：

- `--config`：配置文件路径，默认本目录 `config.txt`
- `--from-file`：本地事件 JSON
- `--start-date` / `--end-date`：接口模式日期范围（成对）
- `--topic-id` / `--region` / `--keyword`：过滤
- `--dry-run`：只拉取分组，不写库、不启爬

## 同日事件合并规则

- 事件名称取 `item.title`
- 同一 `event_date` 的多个 title 合并为一条 `daily_topics`
- `topic_id` 格式：`summary_YYYYMMDD`；`keywords` 为 JSON 字符串数组
- 若该日已有记录：与已有 keywords 增量合并去重

## 数据库

- MySQL：`DB_DIALECT=mysql`，表结构见 `MindSpider/schema/mindspider_tables.sql`
- 达梦：`DB_DIALECT=dm`，端口通常 5236；依赖 `MindSpider/utils/dm_connection.py`

## 日志

`logs/event_sync_YYYYMMDD.log`（同时输出到控制台）

## 其他

Windows 控制台默认 GBK，emoji 可能异常；运行前可设 `set PYTHONIOENCODING=utf-8`。
