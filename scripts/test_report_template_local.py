"""Report template contract tests that require no external services."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TEMPLATES = [
    ("flexible_assistant", True),
    ("structured_four_section", False),
    ("brief_bullets", False),
    ("qa_conversational", False),
    ("decision_memo", False),
    ("situation_brief", False),
    ("risk_assessment", False),
    ("response_brief", False),
]
PREVIEW_BODY_LENGTHS = {
    "flexible_assistant": (330, 400),
    "structured_four_section": (440, 540),
    "brief_bullets": (190, 240),
    "qa_conversational": (270, 340),
    "decision_memo": (360, 450),
    "situation_brief": (270, 340),
    "risk_assessment": (330, 400),
    "response_brief": (270, 340),
}
PREVIEW_NOTICE = (
    "真实事件版式示例，仅用于展示报告结构与表达方式；"
    "示例内容不作为实时研判结论，正式报告以本次检索和会话材料为准。"
)


def _sse_data(line: str) -> dict:
    data_line = next(part for part in line.splitlines() if part.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


def _preview_body_length(content: str) -> int:
    """Count report body characters without headings, notice, or Markdown marks."""
    body_lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ">")):
            continue
        line = re.sub(r"^[-+*]\s*", "", line)
        line = re.sub(r"^\d+[.、]\s*", "", line)
        line = re.sub(r"[`*_>#\[\]()]", "", line)
        body_lines.append(line)
    return len(re.sub(r"\s+", "", "".join(body_lines)))


class TestReportTemplateLoader(unittest.TestCase):
    def test_frontend_template_catalog_matches_manifest(self):
        html = (ROOT / "vue_html_demo.html").read_text(encoding="utf-8")
        match = re.search(r"const TEMPLATES = \[(.*?)\n\]", html, re.S)
        self.assertIsNotNone(match, "前端缺少 TEMPLATES 常量")
        frontend_ids = re.findall(r"id:\s*'([^']+)'", match.group(1))
        self.assertEqual(
            frontend_ids,
            [template_id for template_id, _ in EXPECTED_TEMPLATES],
        )

    def test_all_templates_use_one_unified_md(self):
        from singleAgent.report_template_loader import (
            list_template_ids,
            load_all_report_templates,
            load_report_template,
            split_unified_template_md,
        )

        manifest = json.loads(
            (ROOT / "report_templates" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["version"], 5)
        self.assertEqual(
            list_template_ids(ROOT),
            [template_id for template_id, _ in EXPECTED_TEMPLATES],
        )

        entries = {entry["id"]: entry for entry in manifest["templates"]}
        loaded = load_all_report_templates(project_root=ROOT)
        self.assertEqual([template.id for template in loaded], list(entries))

        for template_id, recommended in EXPECTED_TEMPLATES:
            with self.subTest(template_id=template_id):
                entry = entries[template_id]
                self.assertNotIn("content_files", entry)
                md_path = ROOT / "report_templates" / "user" / f"{template_id}.md"
                self.assertTrue(md_path.is_file(), md_path)
                self.assertEqual(entry["content_file"], f"user/{template_id}.md")
                raw = md_path.read_text(encoding="utf-8").strip()
                contents = split_unified_template_md(raw)
                self.assertIsNotNone(contents)
                assert contents is not None
                self.assertEqual(
                    contents["preview"].splitlines()[0],
                    f"## {entry['name']}（示例）",
                )
                self.assertIn(PREVIEW_NOTICE, contents["preview"])
                self.assertIn("俄乌冲突", contents["preview"])
                self.assertNotIn("效果预览（示例）", contents["preview"])
                self.assertNotIn("资料口径截至", contents["preview"])
                self.assertNotIn("截至", contents["preview"])
                self.assertNotRegex(
                    contents["preview"],
                    r"(?<!\d)(?:19|20)\d{2}(?!\d)",
                )
                self.assertNotRegex(contents["preview"], r"\[(事件名称|风险等级|议题|信号|事实)")
                self.assertNotIn("资料来源", contents["preview"])
                self.assertNotIn("http://", contents["preview"])
                self.assertNotIn("https://", contents["preview"])
                minimum, maximum = PREVIEW_BODY_LENGTHS[template_id]
                body_length = _preview_body_length(contents["preview"])
                self.assertGreaterEqual(body_length, minimum)
                self.assertLessEqual(body_length, maximum)
                self.assertGreaterEqual(entry["max_output_tokens"], 600)
                self.assertLessEqual(entry["max_output_tokens"], 1600)
                self.assertLessEqual(len(contents["guidance"]), 2400)
                self.assertLessEqual(len(contents["preview"]), 1800)
                self.assertLessEqual(len(contents["generation"]), 2400)

                template = load_report_template(template_id, project_root=ROOT)
                self.assertEqual(template.recommended, recommended)
                self.assertEqual(template.guidance_content, contents["guidance"])
                self.assertEqual(template.preview_content, contents["preview"])
                self.assertEqual(template.generation_content, contents["generation"])
                self.assertIn("俄乌冲突", template.generation_content)
                self.assertIn("事件名称", template.generation_content)
                self.assertIn("报告标题", template.generation_content)
                self.assertIn("日期", template.generation_content)
                self.assertNotIn("占位符", template.generation_content)

    def test_internal_fact_ledger_is_not_part_of_template_contract(self):
        ledger = ROOT / "report_templates" / "references" / "russia_ukraine_20260730.md"
        self.assertTrue(ledger.is_file())
        ledger_content = ledger.read_text(encoding="utf-8")
        self.assertIn("核验登记日期：2026-07-30", ledger_content)
        self.assertIn("https://docs.un.org/en/A/RES/ES-11/1", ledger_content)

        manifest_text = (ROOT / "report_templates" / "manifest.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("russia_ukraine_20260730", manifest_text)

    def test_frontend_ref_is_exact_preview_and_hides_other_parts(self):
        from singleAgent.report_template_loader import (
            load_all_report_templates,
            split_unified_template_md,
        )

        for template in load_all_report_templates(project_root=ROOT):
            with self.subTest(template_id=template.id):
                ref = template.to_knowledge_ref()
                md_path = ROOT / "report_templates" / "user" / f"{template.id}.md"
                parts = split_unified_template_md(
                    md_path.read_text(encoding="utf-8")
                )
                self.assertIsNotNone(parts)
                assert parts is not None
                self.assertEqual(ref["content"], parts["preview"])
                self.assertNotIn(template.guidance_content, ref["content"])
                self.assertNotIn(template.generation_content, ref["content"])
                self.assertEqual(ref["category"], "报告版式")
                self.assertEqual(ref["dataset_id"], template.id)

    def test_model_prompt_contains_three_parts_in_order(self):
        from singleAgent.report_template_loader import load_all_report_templates
        from singleAgent.prompts import build_system_prompt

        for template in load_all_report_templates(project_root=ROOT):
            with self.subTest(template_id=template.id):
                prompt = template.to_prompt_block(topic="测试主题")
                guidance_at = prompt.index(template.guidance_content)
                preview_at = prompt.index(template.preview_content)
                generation_at = prompt.index(template.generation_content)
                self.assertLess(guidance_at, preview_at)
                self.assertLess(preview_at, generation_at)
                self.assertEqual(prompt.count(template.preview_content), 1)
                self.assertIn("模板说明和生成要求不得原样复述给用户", prompt)
                self.assertIn("效果预览仅用于学习版式和表达风格", prompt)
                self.assertIn(
                    "效果预览是真实事件的版式示例，不代表当前事实状态",
                    prompt,
                )
                self.assertIn("【当前分析主题】\n测试主题", prompt)
                self.assertIn(
                    "不得把其中事件名称、报告标题、日期、事实、数字或结论迁移到本轮分析主题",
                    prompt,
                )
                self.assertIn("正式报告标题必须围绕【当前分析主题】生成", prompt)
                self.assertIn("不得沿用效果预览中的报告标题", prompt)
                self.assertNotIn("https://docs.un.org/en/A/RES/ES-11/1", prompt)
                self.assertIn("输出上限：", prompt)
                system_prompt = build_system_prompt(
                    topic="测试主题",
                    template_id=template.id,
                    project_root=ROOT,
                )
                self.assertIn(template.guidance_content, system_prompt)
                self.assertIn(template.preview_content, system_prompt)
                self.assertIn(template.generation_content, system_prompt)

    def test_mode1_and_mode0_sse_refs_only_contain_previews(self):
        from singleAgent.report_template_loader import (
            load_all_report_templates,
            load_report_template,
        )
        from singleAgent.sse import emit_retrieval_knowledge

        all_templates = load_all_report_templates(project_root=ROOT)
        mode1_line = emit_retrieval_knowledge(
            refs=[template.to_knowledge_ref() for template in all_templates],
            message="可选报告版式",
            session_id="sess-mode1",
            progress=10,
        )
        mode1_refs = _sse_data(mode1_line)["refs"]
        self.assertEqual(len(mode1_refs), len(EXPECTED_TEMPLATES))
        for ref, template in zip(mode1_refs, all_templates):
            self.assertEqual(ref["content"], template.preview_content)
            self.assertNotIn(template.guidance_content, mode1_line)
            self.assertNotIn(template.generation_content, mode1_line)

        selected = load_report_template("decision_memo", project_root=ROOT)
        mode0_line = emit_retrieval_knowledge(
            refs=[selected.to_knowledge_ref()],
            message="已选报告版式",
            session_id="sess-mode0",
            progress=10,
        )
        mode0_refs = _sse_data(mode0_line)["refs"]
        self.assertEqual(len(mode0_refs), 1)
        self.assertEqual(mode0_refs[0]["content"], selected.preview_content)
        self.assertNotIn(selected.guidance_content, mode0_line)
        self.assertNotIn(selected.generation_content, mode0_line)

    def test_invalid_content_files_and_unknown_template_fail_clearly(self):
        from singleAgent.report_template_loader import (
            TemplateConfigurationError,
            load_builtin_report_template,
            load_report_template,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            templates_root = project_root / "report_templates"
            user_root = templates_root / "user" / "demo"
            user_root.mkdir(parents=True)
            (user_root / "01.md").write_text("guidance", encoding="utf-8")
            (user_root / "02.md").write_text("preview", encoding="utf-8")
            (user_root / "03.md").write_text("generation", encoding="utf-8")
            (project_root / "outside.md").write_text("outside", encoding="utf-8")

            def write_manifest(content_files: dict) -> None:
                manifest = {
                    "version": 4,
                    "default_template_id": "demo",
                    "templates": [{
                        "id": "demo",
                        "sort_order": 1,
                        "max_output_tokens": 800,
                        "content_files": content_files,
                    }],
                }
                (templates_root / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False),
                    encoding="utf-8",
                )

            valid = {
                "guidance": "user/demo/01.md",
                "preview": "user/demo/02.md",
                "generation": "user/demo/03.md",
            }
            write_manifest(valid)
            with self.assertRaisesRegex(TemplateConfigurationError, "未知报告模板"):
                load_report_template("unknown", project_root=project_root)

            missing = dict(valid, preview="user/demo/missing.md")
            write_manifest(missing)
            with self.assertRaisesRegex(FileNotFoundError, "preview.*文件不存在"):
                load_builtin_report_template("demo", project_root=project_root)

            (user_root / "02.md").write_text("   \n", encoding="utf-8")
            write_manifest(valid)
            with self.assertRaisesRegex(TemplateConfigurationError, "preview.*文件为空"):
                load_builtin_report_template("demo", project_root=project_root)

            (user_root / "02.md").write_text("preview", encoding="utf-8")
            escaping = dict(valid, preview="../outside.md")
            write_manifest(escaping)
            with self.assertRaisesRegex(TemplateConfigurationError, "preview.*路径越界"):
                load_builtin_report_template("demo", project_root=project_root)

    def test_resolve_ismodel_defaults_to_2(self):
        from singleAgent.report_template_loader import resolve_ismodel, resolve_template_id

        self.assertEqual(resolve_ismodel({}), 2)
        self.assertEqual(resolve_ismodel({"ismodel": 2}), 2)
        self.assertEqual(resolve_template_id({"ismodel": 2}), "flexible_assistant")

    def test_resolve_ismodel_independent_of_ext(self):
        from singleAgent.report_template_loader import resolve_ismodel

        self.assertEqual(resolve_ismodel({"ismodel": 1, "ext": {}}), 1)
        self.assertEqual(
            resolve_ismodel({
                "ismodel": 2,
                "ext": {"selected_knowledge_ids": ["brief_bullets"]},
            }),
            2,
        )

    def test_resolve_template_from_selected_knowledge_ids(self):
        from singleAgent.report_template_loader import resolve_template_id

        metadata = {
            "ismodel": 1,
            "ext": {"selected_knowledge_ids": ["brief_bullets"]},
        }
        self.assertEqual(resolve_template_id(metadata), "brief_bullets")

    def test_knowledge_ref_shape(self):
        from singleAgent.knowledge_base import KnowledgeChunk, chunks_to_knowledge_refs

        refs = chunks_to_knowledge_refs([
            KnowledgeChunk(
                content="测试片段",
                similarity=0.42,
                document_keyword="demo.txt",
                dataset_id="ds-1",
            ),
        ])
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["category"], "通用知识")
        self.assertEqual(refs[0]["source"], "demo.txt")
        self.assertIn("id", refs[0])


class TestRetrievalSse(unittest.TestCase):
    def test_emit_retrieval_knowledge_template(self):
        from singleAgent.sse import emit_retrieval_knowledge

        line = emit_retrieval_knowledge(
            refs=[{"id": "flexible_assistant", "content": "x", "category": "报告版式"}],
            message="已选报告版式：灵活助手",
            session_id="sess-1",
            progress=10,
        )
        self.assertIn("event: retrieval", line)
        self.assertIn('"type": "knowledge"', line)
        self.assertIn('"category": "报告版式"', line)
        self.assertIn('"session_id": "sess-1"', line)

    def test_emit_retrieval_knowledge_kb(self):
        from singleAgent.sse import emit_retrieval_knowledge
        import json

        line = emit_retrieval_knowledge(
            refs=[{"id": "c1", "content": "片段", "category": "通用知识", "source": "a.pdf"}],
            message="知识库检索完成: 1 条",
            session_id="sess-2",
            progress=1,
            retrieval_type="general_knowledge",
        )
        self.assertIn("event: retrieval", line)
        raw = line.split("data:", 1)[1].strip()
        payload = json.loads(raw)
        self.assertEqual(payload["type"], "general_knowledge")
        self.assertEqual(payload["refs"][0]["category"], "通用知识")


if __name__ == "__main__":
    unittest.main()
