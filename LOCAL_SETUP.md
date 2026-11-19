# 本地运行

新增协同研讨工作台：启动后访问 <http://127.0.0.1:5000/discussion>。
支持离线演示、人机分轮讨论、上传资料 RAG 和按轮次生成报告。
详细操作及开发说明见 [DISCUSSION_DEMO.md](DISCUSSION_DEMO.md)。

在项目目录打开 PowerShell，运行：

```powershell
.\start_local.ps1
```

打开 http://127.0.0.1:5000/，在“LLM 配置”中保存配置并启动系统。
主服务运行期间保留终端窗口；Insight、Media、Query 页面分别使用 8501、8502、8503 端口。

## 当前模型

2026-11-16：Query、Media、Insight、Report、Forum、关键词优化与 MindSpider
统一使用 `qwen3.7-plus`，接口为 `https://maas.qianwenaiapi.com/compatible-mode/v1`。
密钥仅保存在本机 `.env`；已验证 Query 普通调用与 Report 流式调用成功。

## 环境复现

当前使用 Python 3.11 与项目 `.venv`，已安装依赖见 `requirements.local.lock.txt`。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.local.lock.txt
```

Streamlit 1.28.1 需要 NumPy 1.x，因此本地固定 NumPy 1.26.4 和 PyArrow 17.0.0。
较新的 PyArrow 26 在本机导入时要求 NumPy 2，不能与该版 Streamlit 混装。
模型 API 密钥保留在本地 `.env`，不要提交到版本库。

## 俄乌冲突公开资料版报告

本次通过 `scripts/generate_ukraine_public_report.py` 直接调用项目 ReportEngine。
原始公开资料与来源清单保存在 `report_inputs/ukraine_public/`。
报告结果清单为 `final_reports/ukraine_public_result.json`。
这是独立生成任务，不会出现在 Flask 内存中的三 Agent 任务列表中。

经来源核对的交付版本由 `scripts/review_ukraine_public_report.py` 导出。
网页预览：http://127.0.0.1:5000/static/generated_reports/ukraine_public.html
同目录的 `ukraine_public.pdf` 和 `ukraine_public.md` 分别为 PDF 与 Markdown 版本。
模型初稿保存在 `report_inputs/ukraine_public/original_model_ir.json`，不作为交付版本。

## 联网搜索配置

2026-11-16 已实测博查 Web Search 与安思派搜索均返回真实网页结果。
当前 Query 使用 `QUERY_SEARCH_PROVIDER=bocha`，Media 使用
`SEARCH_TOOL_TYPE=AnspireAPI`；不再要求提供 Tavily 密钥。

Query 可切换 `tavily`、`bocha` 或 `anspire`。博查 Web Search 地址使用
`QUERY_BOCHA_BASE_URL=https://api.bocha.cn/v1/web-search`，与 Media 原有的
`BOCHA_BASE_URL`（AI Search）分开。密钥存放在本机 `.env`。
修改配置后重启主服务与分析组件。

离线适配测试：

```powershell
.\.venv\Scripts\python.exe scripts/test_query_web_search_local.py
```

网页搜索返回的采集时间不作为发布时间；博查与安思派的网页接口不提供
已验证的图片结果，因此 Query 图片工具通过这两个服务调用时仅返回网页证据。

## 尚未接通的数据源

达梦数据库 5236 端口此前检查拒绝连接，仍需启动数据库并准备真实舆情样本。
上述公开资料版报告生成于搜索接通之前，没有本地舆情数据、社交平台抽样或情感分析结果。
