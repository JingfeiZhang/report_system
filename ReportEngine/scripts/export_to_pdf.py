#!/usr/bin/env python3
"""
PDF导出工具 - 从已保存的 Document IR 离线生成 PDF（无需重跑分析）。

用法:
    python ReportEngine/scripts/export_to_pdf.py <报告IR JSON文件> [输出PDF路径]

示例（今天「美伊以战争」任务）:
    python ReportEngine/scripts/export_to_pdf.py final_reports/ir/report_ir_美伊以战争_20260706_132256.json
    python ReportEngine/scripts/export_to_pdf.py final_reports/ir/report_ir_美伊以战争_20260706_132256.json final_reports/pdf/test.pdf

依赖检查:
    python -m ReportEngine.utils.dependency_check
"""

import sys
import json
from pathlib import Path
from loguru import logger

# 保证从项目根目录可导入
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ReportEngine.utils.dependency_check import check_pango_available, prepare_pango_environment
from ReportEngine.renderers import PDFRenderer


def export_to_pdf(ir_json_path: str, output_pdf_path: str = None):
    """
    从IR JSON文件生成PDF

    参数:
        ir_json_path: Document IR JSON文件路径
        output_pdf_path: 输出PDF路径（可选，默认为同名.pdf）
    """
    ir_path = Path(ir_json_path)
    if not ir_path.is_absolute():
        ir_path = (Path.cwd() / ir_path).resolve()

    if not ir_path.exists():
        logger.error(f"文件不存在: {ir_path}")
        return False

    prepare_pango_environment()
    ok, message = check_pango_available()
    if not ok:
        logger.error(message)
        return False

    # 读取IR数据
    logger.info(f"读取报告 IR: {ir_path}")
    with open(ir_path, 'r', encoding='utf-8') as f:
        document_ir = json.load(f)

    if output_pdf_path is None:
        pdf_dir = PROJECT_ROOT / "final_reports" / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        output_pdf_path = pdf_dir / f"{ir_path.stem}.pdf"
    else:
        output_pdf_path = Path(output_pdf_path)
        if not output_pdf_path.is_absolute():
            output_pdf_path = (Path.cwd() / output_pdf_path).resolve()
        output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # 生成PDF
    logger.info("开始生成 PDF（正式 IR 渲染链路）...")
    renderer = PDFRenderer()

    try:
        renderer.render_to_pdf(
            document_ir,
            output_pdf_path,
            optimize_layout=True,
            ir_file_path=str(ir_path),
        )
        logger.success(f"✓ PDF已生成: {output_pdf_path}")
        return True
    except Exception as e:
        logger.error(f"✗ PDF生成失败: {e}")
        logger.exception("详细错误信息:")
        return False


def main():
    """主函数"""
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    ir_json_path = sys.argv[1]
    output_pdf_path = sys.argv[2] if len(sys.argv) > 2 else None

    success = export_to_pdf(ir_json_path, output_pdf_path)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
