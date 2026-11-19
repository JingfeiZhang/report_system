# -*- coding: utf-8 -*-
"""生成智能体梳理 Word 文档"""
from pathlib import Path

try:
    from docx import Document
    from docx.shared import Pt, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
except ImportError:
    import subprocess
    import sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "python-docx", "-q"])
    from docx import Document
    from docx.shared import Pt, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn


def set_cell_text(cell, text, bold=False):
    cell.text = ""
    p = cell.paragraphs[0]
    run = p.add_run(text)
    run.font.size = Pt(10.5)
    run.font.name = "宋体"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    run.bold = bold


def add_table(doc, headers, rows, col_widths=None):
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = "Table Grid"
    hdr = table.rows[0].cells
    for i, h in enumerate(headers):
        set_cell_text(hdr[i], h, bold=True)
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            set_cell_text(table.rows[ri + 1].cells[ci], val)
    if col_widths:
        for row in table.rows:
            for i, w in enumerate(col_widths):
                row.cells[i].width = Cm(w)
    doc.add_paragraph()
    return table


def main():
    root = Path(__file__).resolve().parent.parent
    out = root / "智能体模块说明.docx"

    doc = Document()
    doc.styles["Normal"].font.name = "宋体"
    doc.styles["Normal"]._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    doc.styles["Normal"].font.size = Pt(10.5)

    title = doc.add_heading("BettaFish_Modify 多智能体梳理表（报送稿）", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    meta = doc.add_paragraph()
    meta.add_run("课题方向：美伊以战争舆情分析\n")
    meta.add_run("整理人：（填写）          整理日期：2026-05-31\n")
    meta.add_run("报送对象：卫鼎 → 佛计")

    doc.add_heading("一、报送说明", level=1)
    doc.add_paragraph(
        "本文档按任务要求梳理：各模块显性/隐性智能体、各智能体所调数据库与知识库、"
        "已开发算法工具，以及当前完成度。数据与检索以达梦数据库 schema「mindspider_war」"
        "为主；公网检索已离线化为 mock_search_api。"
    )

    doc.add_heading("二、智能体清单总表", level=1)
    add_table(
        doc,
        ["所属模块", "智能体名称", "类型", "主要职责", "调用的数据库/知识库", "完成度"],
        [
            [
                "InsightEngine",
                "Insight Agent\n(DeepSearchAgent)",
                "显性",
                "库内舆情深度挖掘：检索、情感分析、分段反思，输出 Insight 专题报告",
                "达梦 mindspider_war（直连）；检索结果缓存于引擎 state",
                "已打通",
            ],
            [
                "InsightEngine",
                "关键词优化智能体\n(KeywordOptimizer)",
                "隐性",
                "将 Agent 检索词优化为更贴近舆情库的关键词",
                "不直连库；调用 LLM API；输入来自 Insight 检索上下文",
                "已开发",
            ],
            [
                "MediaEngine",
                "Media Agent\n(DeepSearchAgent)",
                "显性",
                "媒体向内容分析：检索、结构化归纳、分段研究，输出 Media 专题报告",
                "mock_search_api → 达梦 mindspider_war；引擎 state",
                "已打通",
            ],
            [
                "QueryEngine",
                "Query Agent\n(DeepSearchAgent)",
                "显性",
                "新闻/信息检索视角：多轮搜索与反思，输出 Query 专题报告",
                "mock_search_api → 达梦 mindspider_war；引擎 state",
                "已打通",
            ],
            [
                "ForumEngine",
                "论坛主持人智能体\n(ForumHost)",
                "隐性",
                "读取三引擎运行日志，生成主持人引导语，促进协作",
                "logs/forum.log（论坛日志）；不直连业务库",
                "已开发",
            ],
            [
                "ForumEngine",
                "论坛监控智能体\n(LogMonitor)",
                "隐性",
                "监控 Insight/Media/Query 日志并写入论坛记录",
                "三引擎运行日志；forum.log",
                "已开发",
            ],
            [
                "ReportEngine",
                "Report Agent\n(ReportAgent)",
                "显性",
                "汇总三引擎报告与论坛内容，生成 HTML 综合报告",
                "三引擎 .md 报告；forum.log；章节 IR/JSON；可选 GraphRAG 图谱",
                "基本可用",
            ],
            [
                "ReportEngine",
                "GraphRAG 查询智能体\n（可选）",
                "隐性",
                "基于三引擎 state 构建图谱，章节生成前多轮图查询增强",
                "三引擎 state_*.json；forum.log；图谱本地存储",
                "已开发\n默认关闭",
            ],
        ],
        col_widths=[2.8, 3.2, 1.2, 4.5, 4.5, 1.5],
    )
    doc.add_paragraph(
        "说明：显性智能体为 Insight、Media、Query、Report 及论坛主持人；"
        "隐性智能体为嵌入流水线的 LLM 或监控组件。"
        "三引擎内部的报告结构/检索/总结/格式化等处理节点属算法流水线，不单列为独立智能体。"
    )

    doc.add_heading("三、数据库与知识库调用关系", level=1)
    add_table(
        doc,
        ["数据源", "内容说明", "调用方"],
        [
            [
                "达梦 DM\nmindspider_war",
                "schema: mindspider_war；战争舆情业务表",
                "Insight（直连）；Media/Query（经 mock）；mock_search_api",
            ],
            [
                "mock_search_api\n(8787)",
                "离线模拟 Tavily/Anspire，统一转发达梦检索",
                "Media Agent、Query Agent",
            ],
            [
                "引擎运行态知识",
                "state_*.json、deep_search_report_*.md",
                "Report Agent；GraphRAG（可选）",
            ],
            [
                "论坛协作知识",
                "logs/forum.log",
                "Forum 主持人/监控；Report Agent",
            ],
            [
                "本地情感模型知识",
                "WeiboMultilingualSentiment 预训练权重",
                "Insight Agent（情感分析）",
            ],
            [
                "外部 LLM API",
                "百炼/aihubmix 等 OpenAI 兼容接口",
                "各 Agent 推理与隐性智能体",
            ],
        ],
        col_widths=[3.5, 6.5, 5.5],
    )

    doc.add_heading("四、已开发算法与工具清单（按模块）", level=1)
    add_table(
        doc,
        ["模块", "已开发算法/工具"],
        [
            [
                "InsightEngine",
                "MediaCrawlerDB（热点/全局/平台/日期检索、评论抓取、热度排序）；"
                "KeywordOptimizer（LLM）；WeiboMultilingualSentimentAnalyzer（本地模型）；"
                "深度检索流水线节点（结构生成、检索、总结、格式化）；文本处理与 DB 工具",
            ],
            [
                "MediaEngine",
                "Anspire/Bocha 风格搜索工具集（经 mock 离线化）；时效检索；"
                "深度检索流水线节点；文本处理工具",
            ],
            [
                "QueryEngine",
                "Tavily 风格新闻搜索工具集（经 mock 离线化）；深度检索流水线节点；文本处理工具",
            ],
            [
                "ReportEngine",
                "模板解析、布局规划、篇幅预算、章节生成与校验；图表/表格/JSON 处理；"
                "HTML/PDF/Markdown 渲染；GraphRAG（可选）；report_engine_only.py",
            ],
            [
                "ForumEngine",
                "LogMonitor；ForumHost（LLM）；forum_reader",
            ],
            [
                "mock_search_api",
                "达梦数据库服务；Tavily/Anspire 兼容路由；检索结果封装（配套，非智能体）",
            ],
            [
                "流水线编排",
                "fastapi_integration（无头全流程）；app.py（Web 控制台）（配套，非智能体）",
            ],
        ],
        col_widths=[3.0, 12.5],
    )

    doc.add_heading("五、当前完成度汇总", level=1)
    add_table(
        doc,
        ["类别", "内容"],
        [
            [
                "已完成",
                "五大显性智能体 + 隐性协作组件均已实现并可联调；"
                "达梦 mindspider_war 对接、mock 离线检索、本机与 server2 Docker 部署；"
                "可产出战争舆情三引擎 .md 及 HTML 终稿",
            ],
            [
                "待完善",
                "报告信息溯源（终稿尚不能逐条反查达梦原始记录）；"
                "报告生成耗时较长；Report 偶发因模型输出过短导致成稿失败",
            ],
        ],
        col_widths=[2.5, 13.0],
    )

    doc.add_heading("六、下一步计划", level=1)
    add_table(
        doc,
        ["序号", "计划内容"],
        [
            ["1", "报告信息溯源：终稿补充可核查来源，支持反查达梦库原始舆情记录"],
            ["2", "缩短报告生成时间：优化三引擎与 Report 全流程耗时，便于演示与迭代"],
        ],
        col_widths=[1.5, 14.0],
    )

    doc.add_heading("七、一句话摘要", level=1)
    doc.add_paragraph(
        "BettaFish_Modify 共 5 个显性智能体（Insight/Media/Query/Report + 论坛主持人）"
        "及若干隐性智能体（关键词优化、论坛监控、可选 GraphRAG）；核心库为达梦 mindspider_war，"
        "Media/Query 经 mock_search_api 间接访问；已配套检索、情感分析、报告生成、图谱增强等"
        "算法工具。当前能稳定出报告，下一步做溯源与提速。"
    )

    doc.save(out)
    print(f"已生成: {out}")


if __name__ == "__main__":
    main()
