"""ismodel 与 ext 解耦：请求入口 + 路由门闩（无需外部服务）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestFastapiIsmodelPreserved(unittest.TestCase):
    def test_ext_empty_keeps_ismodel_1(self):
        from fastapi_integration import AnalysisEventRequest

        req = AnalysisEventRequest(
            instruction="美伊以战争",
            session_id="sess-ismodel-1",
            ismodel=1,
            ext={},
        )
        event = req.to_analysis_event()
        self.assertEqual(event.metadata.get("ismodel"), 1)
        self.assertEqual(event.metadata.get("ext"), {})

    def test_ext_empty_keeps_ismodel_2(self):
        from fastapi_integration import AnalysisEventRequest

        req = AnalysisEventRequest(
            instruction="随便聊聊",
            session_id="sess-ismodel-2",
            ismodel=2,
            ext={},
        )
        event = req.to_analysis_event()
        self.assertEqual(event.metadata.get("ismodel"), 2)

    def test_ext_nonempty_does_not_override_ismodel_2(self):
        """mode 看 ext；ismodel 仍尊重前端（即使少见）。"""
        from fastapi_integration import AnalysisEventRequest

        req = AnalysisEventRequest(
            instruction="出报告",
            session_id="sess-ext-nonempty",
            ismodel=2,
            ext={"selected_knowledge_ids": ["structured_four_section"]},
        )
        event = req.to_analysis_event()
        self.assertEqual(event.metadata.get("ismodel"), 2)
        self.assertTrue(event.metadata.get("ext"))

    def test_empty_instruction_allowed_when_template_selected(self):
        """仅勾选模板、instruction 为空时应通过校验。"""
        from fastapi_integration import AnalysisEventRequest
        from pydantic import ValidationError

        req = AnalysisEventRequest(
            instruction="",
            session_id="sess-empty-instr-tpl",
            ismodel=1,
            ext={"selected_knowledge_ids": ["structured_four_section"]},
        )
        event = req.to_analysis_event()
        self.assertEqual(
            event.metadata.get("ext", {}).get("selected_knowledge_ids"),
            ["structured_four_section"],
        )

        with self.assertRaises(ValidationError):
            AnalysisEventRequest(
                instruction="",
                session_id="sess-empty-all",
                ismodel=1,
                ext={},
            )


class TestPipelineModeByExt(unittest.TestCase):
    def test_decide_mode_ignores_ismodel(self):
        from singleAgent.pipeline import _decide_mode_by_ext

        d0 = _decide_mode_by_ext(
            {"selected_knowledge_ids": ["brief_bullets"]},
            first_round=False,
        )
        self.assertEqual(d0.mode, 0)

        d1 = _decide_mode_by_ext({}, first_round=False)
        self.assertEqual(d1.mode, 1)

        d_first = _decide_mode_by_ext(
            {"selected_knowledge_ids": ["brief_bullets"]},
            first_round=True,
        )
        self.assertEqual(d_first.mode, 1)

    def test_first_round_clears_ext_but_keeps_ismodel(self):
        """首轮清空 ext，但保留 ismodel=1，以便讨论态推模板。"""
        from singleAgent.pipeline import run_fast_analysis
        from singleAgent.session_store import SessionState

        event = SimpleNamespace(
            event_id="e1",
            topic="美伊以战争",
            query="美伊以战争",
            metadata={
                "session_id": "sess-first-round",
                "instruction": "美伊以战争",
                "ismodel": 1,
                "ext": {"selected_knowledge_ids": ["brief_bullets"]},
                "files_data": [],
            },
        )

        empty_session = SessionState(session_id="sess-first-round")
        fake_store = SimpleNamespace(get=lambda _sid: empty_session)
        captured = {}

        def chat_side_effect(ev, config, **kwargs):
            captured["meta"] = dict(ev.metadata or {})
            captured["mode_path"] = "chat"
            yield "event: thinking\ndata: {\"type\":\"progress\",\"message\":\"ok\"}\n\n"

        with patch("singleAgent.pipeline.get_session_store", return_value=fake_store), patch(
            "singleAgent.pipeline.run_chat_pipeline", side_effect=chat_side_effect
        ), patch(
            "singleAgent.pipeline.run_report_pipeline",
            side_effect=AssertionError("首轮不应进 mode0"),
        ):
            list(run_fast_analysis(event, config={}))

        self.assertEqual(captured.get("mode_path"), "chat")
        self.assertEqual(captured["meta"].get("ext"), {})
        self.assertEqual(captured["meta"].get("ismodel"), 1)

    def test_ext_empty_ismodel_1_goes_chat_and_keeps_flag(self):
        from singleAgent.pipeline import run_fast_analysis
        from singleAgent.session_store import SessionState

        # 已有 AI 回合 → 非首轮
        session = SessionState(session_id="sess-chat-1")
        session.conversations = [
            SimpleNamespace(role="human", content="hi"),
            SimpleNamespace(role="ai", content="hello"),
        ]
        fake_store = SimpleNamespace(get=lambda _sid: session)

        event = SimpleNamespace(
            event_id="e2",
            topic="话题",
            query="继续聊",
            metadata={
                "session_id": "sess-chat-1",
                "instruction": "继续聊",
                "ismodel": 1,
                "ext": {},
                "files_data": [],
            },
        )
        captured = {}

        def chat_side_effect(ev, config, **kwargs):
            captured["meta"] = dict(ev.metadata or {})
            yield "event: thinking\ndata: {\"type\":\"progress\",\"message\":\"ok\"}\n\n"

        with patch("singleAgent.pipeline.get_session_store", return_value=fake_store), patch(
            "singleAgent.pipeline.run_chat_pipeline", side_effect=chat_side_effect
        ), patch(
            "singleAgent.pipeline.run_report_pipeline",
            side_effect=AssertionError("ext 空不应进 mode0"),
        ):
            list(run_fast_analysis(event, config={}))

        self.assertEqual(captured["meta"].get("ismodel"), 1)
        self.assertEqual(captured["meta"].get("ext"), {})


class TestChatPipelineTemplateGate(unittest.TestCase):
    def test_ismodel_1_without_template_only_prompts_pick(self):
        """ismodel=1 且未选模板：推右栏模板 + 短提示，不跑检索/LLM。"""
        from singleAgent.chat_pipeline import run_chat_pipeline
        from singleAgent.session_store import SessionState

        session = SessionState(session_id="sess-tpl")
        event = SimpleNamespace(
            topic="测模板",
            query="测模板",
            metadata={
                "session_id": "sess-tpl",
                "instruction": "测模板",
                "ismodel": 1,
                "ext": {},
                "knowledge_base_ids": [],
            },
        )
        store = SimpleNamespace(
            get=lambda sid: session,
            save_report=lambda *a, **k: None,
            update_last_topic_if_current=lambda *a, **k: True,
            append_message=lambda *a, **k: None,
            record_ai_turn=lambda *a, **k: None,
        )

        with patch(
            "singleAgent.chat_pipeline.build_initial_search_plan",
            side_effect=AssertionError("未选模板不应规划检索"),
        ), patch(
            "singleAgent.chat_pipeline.stream_chat",
            side_effect=AssertionError("未选模板不应调用 LLM"),
        ):
            lines = list(
                run_chat_pipeline(
                    event,
                    config={},
                    project_root=ROOT,
                    session_store=store,
                    session=session,
                    session_id="sess-tpl",
                    instruction="测模板",
                    started=__import__("time").time(),
                )
            )

        joined = "".join(lines)
        self.assertIn("event: retrieval", joined)
        self.assertIn("报告版式", joined)
        self.assertIn("flexible_assistant", joined)
        self.assertIn("请选择右侧模板", joined)
        self.assertIn("awaiting_template", joined)
        self.assertIn('"type": "awaiting_template"', joined)
        self.assertNotIn('"type": "chunk"', joined)
        self.assertIn("> 请选择右侧模板", joined)
        self.assertNotIn('"type": "aux"', joined)
        self.assertNotIn("正在规划讨论检索词", joined)
        self.assertNotIn("如需使用模板，请在勾选模板后回复", joined)

    def test_ismodel_2_skips_templates(self):
        from singleAgent.chat_pipeline import run_chat_pipeline
        from singleAgent.session_store import SessionState

        session = SessionState(session_id="sess-no-tpl")
        event = SimpleNamespace(
            topic="纯聊天",
            query="纯聊天",
            metadata={
                "session_id": "sess-no-tpl",
                "instruction": "纯聊天",
                "ismodel": 2,
                "ext": {},
                "knowledge_base_ids": [],
            },
        )
        store = SimpleNamespace(
            get=lambda sid: session,
            save_report=lambda *a, **k: None,
            update_last_topic_if_current=lambda *a, **k: True,
            append_message=lambda *a, **k: None,
            record_ai_turn=lambda *a, **k: None,
        )

        with patch("singleAgent.chat_pipeline.build_initial_search_plan") as plan, patch(
            "singleAgent.chat_pipeline.stream_chat", return_value=iter(["嗯"])
        ), patch(
            "singleAgent.chat_pipeline.resolve_llm_settings",
            return_value={
                "api_key": "EMPTY",
                "base_url": "http://127.0.0.1:9/v1",
                "model_name": "dummy",
            },
        ), patch(
            "singleAgent.chat_pipeline._open_search_db", return_value=None
        ), patch(
            "singleAgent.chat_pipeline.load_all_report_templates",
            side_effect=AssertionError("ismodel=2 不应加载模板"),
        ):
            plan.return_value = SimpleNamespace(
                topic="纯聊天",
                terms=[],
                topic_source="user",
                terms_source="none",
            )
            lines = list(
                run_chat_pipeline(
                    event,
                    config={},
                    project_root=ROOT,
                    session_store=store,
                    session=session,
                    session_id="sess-no-tpl",
                    instruction="纯聊天",
                    started=__import__("time").time(),
                )
            )

        joined = "".join(lines)
        self.assertNotIn("报告版式", joined)
        self.assertNotIn("可选报告版式", joined)
        self.assertNotIn("如需使用模板，请在勾选模板后回复", joined)


if __name__ == "__main__":
    unittest.main()
