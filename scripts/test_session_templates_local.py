"""全局报告模板 CRUD 单测（不依赖外部服务；与 session 无关）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


class TestGlobalTemplateCrud(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_list_update_delete_custom(self):
        from singleAgent.session_templates import (
            create_template,
            delete_template,
            list_templates,
            update_template,
        )

        created = create_template(
            {
                "name": "我的简报",
                "tagline": "短",
                "length_label": "约 500 字",
                "max_output_tokens": 800,
                "guidance": "说明A",
                "preview": "预览A",
                "generation": "生成A",
                "sort_order": 50,
            },
            project_root=ROOT,
            base_dir=self.base,
        )
        self.assertTrue(created["id"].startswith("custom_"))
        self.assertEqual(created["origin"], "custom")
        self.assertEqual(created["preview"], "预览A")

        items = list_templates(
            project_root=ROOT, base_dir=self.base, include_content=False
        )
        ids = [i["id"] for i in items]
        self.assertIn(created["id"], ids)
        self.assertIn("flexible_assistant", ids)

        updated = update_template(
            created["id"],
            {"preview": "预览B", "name": "我的简报2"},
            project_root=ROOT,
            base_dir=self.base,
        )
        self.assertEqual(updated["preview"], "预览B")
        self.assertEqual(updated["name"], "我的简报2")
        self.assertEqual(updated["guidance"], "说明A")

        # 自由格式：只交 content
        free = create_template(
            {
                "name": "自由模板",
                "content": "# 随便写\n\n甲方自定义正文，无强制分节。",
                "max_output_tokens": 800,
            },
            project_root=ROOT,
            base_dir=self.base,
        )
        self.assertEqual(free["preview"], free["generation"])
        self.assertIn("甲方自定义正文", free["content"])
        md = self.base / "user" / f"{free['id']}.md"
        self.assertTrue(md.is_file())
        self.assertIn("甲方自定义正文", md.read_text(encoding="utf-8"))

        deleted = delete_template(
            created["id"], project_root=ROOT, base_dir=self.base
        )
        self.assertEqual(deleted["action"], "removed_custom_copy")
        ids2 = [
            i["id"]
            for i in list_templates(project_root=ROOT, base_dir=self.base)
        ]
        self.assertNotIn(created["id"], ids2)

    def test_hide_and_restore_system(self):
        from singleAgent.session_templates import (
            delete_template,
            list_templates,
            restore_template,
        )

        before = {
            i["id"] for i in list_templates(project_root=ROOT, base_dir=self.base)
        }
        self.assertIn("structured_four_section", before)

        delete_template(
            "structured_four_section", project_root=ROOT, base_dir=self.base
        )
        after = {
            i["id"] for i in list_templates(project_root=ROOT, base_dir=self.base)
        }
        self.assertNotIn("structured_four_section", after)

        restored = restore_template(
            "structured_four_section", project_root=ROOT, base_dir=self.base
        )
        self.assertEqual(restored["origin"], "system")
        self.assertIn(
            "structured_four_section",
            {
                i["id"]
                for i in list_templates(project_root=ROOT, base_dir=self.base)
            },
        )

    def test_override_system_via_put(self):
        from singleAgent.session_templates import load_merged_report_template, update_template
        from singleAgent.report_template_loader import load_builtin_report_template

        update_template(
            "flexible_assistant",
            {
                "name": "灵活助手（全局改）",
                "preview": "全局预览",
                "guidance": "全局说明",
                "generation": "全局生成要求",
                "max_output_tokens": 1200,
            },
            project_root=ROOT,
            base_dir=self.base,
        )
        t = load_merged_report_template(
            "flexible_assistant",
            project_root=ROOT,
            base_dir=self.base,
        )
        self.assertEqual(t.name, "灵活助手（全局改）")
        self.assertEqual(t.preview_content, "全局预览")

        # 内置原版不受影响
        sys_t = load_builtin_report_template("flexible_assistant", project_root=ROOT)
        self.assertNotEqual(sys_t.preview_content, "全局预览")

    def test_http_api(self):
        from singleAgent.session_templates_api import router
        import singleAgent.session_templates as st

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        old_dir = st.CUSTOM_REPORT_TEMPLATES_DIR
        st.CUSTOM_REPORT_TEMPLATES_DIR = str(self.base)
        st.SESSION_TEMPLATES_DIR = str(self.base)
        try:
            r = client.get("/api/v1/report-templates")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["code"], 0)
            self.assertGreaterEqual(body["data"]["total"], 8)
            self.assertNotIn("session_id", body["data"])

            r = client.post(
                "/api/v1/report-templates",
                json={
                    "name": "API模板",
                    "guidance": "g",
                    "preview": "p",
                    "generation": "gen",
                    "max_output_tokens": 900,
                },
            )
            self.assertEqual(r.status_code, 200, r.text)
            tid = r.json()["data"]["id"]
            self.assertEqual(r.json()["data"]["origin"], "custom")

            r = client.put(
                f"/api/v1/report-templates/{tid}",
                json={"preview": "p2"},
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["data"]["preview"], "p2")

            r = client.delete(f"/api/v1/report-templates/{tid}")
            self.assertEqual(r.status_code, 200, r.text)

            r = client.delete("/api/v1/report-templates/brief_bullets")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["data"]["action"], "hidden_system")

            r = client.post("/api/v1/report-templates/brief_bullets/restore")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["data"]["origin"], "system")
        finally:
            st.CUSTOM_REPORT_TEMPLATES_DIR = old_dir
            st.SESSION_TEMPLATES_DIR = old_dir


if __name__ == "__main__":
    unittest.main()
