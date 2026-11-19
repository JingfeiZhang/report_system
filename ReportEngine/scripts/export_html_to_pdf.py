#!/usr/bin/env python3
"""
从已保存的 HTML 离线生成 PDF（快速验证 WeasyPrint / GTK 依赖）。

注意：预览 HTML 可能含浏览器端图表脚本，版式可能与正式 IR→PDF 略有差异。
正式交付建议优先使用 export_to_pdf.py（IR 路径）。

用法:
    python ReportEngine/scripts/export_html_to_pdf.py <报告.html> [输出.pdf]

示例:
    python ReportEngine/scripts/export_html_to_pdf.py final_reports/final_report_美伊以战争_20260706_132256.html
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

# 保证从项目根目录可导入
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ReportEngine.utils.dependency_check import check_pango_available, prepare_pango_environment


def export_html_to_pdf(html_path: str, output_pdf_path: str | None = None) -> bool:
    html_file = Path(html_path)
    if not html_file.is_absolute():
        html_file = (Path.cwd() / html_file).resolve()

    if not html_file.exists():
        logger.error(f"HTML 文件不存在: {html_file}")
        return False

    prepare_pango_environment()
    ok, message = check_pango_available()
    if not ok:
        logger.error(message)
        return False

    if output_pdf_path is None:
        pdf_file = html_file.with_suffix(".pdf")
    else:
        pdf_file = Path(output_pdf_path)
        if not pdf_file.is_absolute():
            pdf_file = (Path.cwd() / pdf_file).resolve()

    pdf_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        from weasyprint import HTML
        from weasyprint.text.fonts import FontConfiguration

        logger.info(f"读取 HTML: {html_file}")
        logger.info(f"输出 PDF: {pdf_file}")
        font_config = FontConfiguration()
        HTML(filename=str(html_file), base_url=str(html_file.parent)).write_pdf(
            str(pdf_file),
            font_config=font_config,
            presentational_hints=True,
        )
        logger.success(f"PDF 已生成: {pdf_file}")
        return True
    except Exception as e:
        logger.error(f"PDF 生成失败: {e}")
        logger.exception("详细错误:")
        return False


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    html_path = sys.argv[1]
    output_pdf = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(0 if export_html_to_pdf(html_path, output_pdf) else 1)


if __name__ == "__main__":
    main()
