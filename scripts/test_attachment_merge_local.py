"""附件 content 合并进 instruction；附件不参与 mode 门闩。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from singleAgent.ext_utils import (
    apply_attachment_merge_to_metadata,
    merge_instruction_with_file_contents,
)
from singleAgent.pipeline import _decide_mode_by_ext


class TestMergeInstructionWithAttachments(unittest.TestCase):
    def test_merge_appends_contents(self):
        merged = merge_instruction_with_file_contents(
            "123",
            [
                {
                    "name": "a.docx",
                    "type": "docx",
                    "content": "第一页新建pdf文档，文档内容仅测试使用",
                }
            ],
            max_chars_per_file=5000,
        )
        self.assertIn("123", merged)
        self.assertIn("第一页新建pdf文档", merged)

    def test_merge_files_only(self):
        merged = merge_instruction_with_file_contents(
            "",
            [{"name": "a.docx", "type": "docx", "content": "仅附件正文"}],
            max_chars_per_file=5000,
        )
        self.assertEqual(merged, "仅附件正文")

    def test_apply_metadata_is_idempotent(self):
        meta = {
            "instruction": "短指令",
            "files_data": [
                {"name": "a.docx", "type": "docx", "content": "附件全文"}
            ],
        }
        first = apply_attachment_merge_to_metadata(meta)
        second = apply_attachment_merge_to_metadata(meta)
        self.assertEqual(first, second)
        self.assertTrue(meta.get("attachments_merged_into_instruction"))
        self.assertEqual(meta.get("raw_instruction"), "短指令")
        self.assertEqual(first.count("附件全文"), 1)


class TestHumanMetadataFilesData(unittest.TestCase):
    def test_build_event2_human_metadata_includes_filesData(self):
        from fastapi_integration import (
            AnalysisEvent,
            AnalysisEventRequest,
            FileDataItem,
            _build_event2_human_metadata,
        )
        from singleAgent.session_store import _platform_metadata

        body = AnalysisEventRequest(
            instruction="看附件",
            session_id="s1",
            files_data=[
                FileDataItem(name="a.docx", type="docx", content="正文A"),
            ],
        )
        event = AnalysisEvent(
            event_id="e1",
            event_type="analysis_request",
            topic="主题",
            query="看附件",
            metadata={"request_id": "r1"},
        )
        meta = _build_event2_human_metadata(
            body, event, {"model_name": "m", "base_url": "http://x", "api_key": "k"}
        )
        self.assertIn("filesData", meta)
        self.assertEqual(len(meta["filesData"]), 1)
        self.assertEqual(meta["filesData"][0]["name"], "a.docx")
        self.assertEqual(meta["filesData"][0]["content"], "正文A")

        platform = _platform_metadata(meta)
        self.assertEqual(platform.get("filesData"), meta["filesData"])


class TestFilesDoNotForceMode0(unittest.TestCase):
    def test_files_ignored_by_mode_gate(self):
        # 无模板、非首轮、ext 空 → mode1，即使有附件也不再强制 mode0
        decision = _decide_mode_by_ext({}, first_round=False)
        self.assertEqual(decision.mode, 1)

    def test_run_fast_analysis_merges_before_chat(self):
        from singleAgent.pipeline import run_fast_analysis
        from singleAgent.session_store import SessionState

        event = SimpleNamespace(
            event_id="sess-file",
            topic="测",
            query="测",
            metadata={
                "session_id": "sess-file",
                "instruction": "123",
                "ismodel": 2,
                "ext": {},
                "files_data": [
                    {
                        "name": "新建 DOCX 文档.docx",
                        "type": "docx",
                        "content": "第一页新建pdf文档，文档内容仅测试使用",
                    }
                ],
                "knowledge_base_ids": [],
            },
        )
        captured = {}

        def fake_chat(*args, **kwargs):
            captured["instruction"] = kwargs.get("instruction")
            captured["meta"] = dict(args[0].metadata or {})
            if False:
                yield ""

        store = SimpleNamespace(get=lambda sid: SessionState(session_id=sid))

        with patch(
            "singleAgent.pipeline.get_session_store",
            return_value=store,
        ), patch(
            "singleAgent.pipeline.run_chat_pipeline",
            side_effect=fake_chat,
        ), patch(
            "singleAgent.pipeline.run_report_pipeline",
            side_effect=AssertionError("有附件不应再强制 mode0"),
        ):
            list(
                run_fast_analysis(
                    event,
                    config={},
                    project_root=None,
                )
            )

        self.assertIn("123", captured.get("instruction") or "")
        self.assertIn("第一页新建pdf文档", captured.get("instruction") or "")
        self.assertTrue(
            captured.get("meta", {}).get("attachments_merged_into_instruction")
        )


if __name__ == "__main__":
    unittest.main()
