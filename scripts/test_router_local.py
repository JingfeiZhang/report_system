"""路由 guardrail 与 prompt 单元测试（无需 LLM）。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from singleAgent.router import RouteDecision, _apply_guardrails, _fallback_route
from singleAgent.session_store import SessionState


class TestRouteGuardrails(unittest.TestCase):
    def _session(self, *, has_report: bool, topic: str = "美伊以战争") -> SessionState:
        return SessionState(
            session_id="t1",
            last_report_md="# 报告\n内容" if has_report else "",
            last_topic=topic if has_report else "",
        )

    def test_fallback_with_report_defaults_mode2(self):
        d = _fallback_route(self._session(has_report=True), False, False)
        self.assertEqual(d.mode, 2)

    def test_no_report_cannot_be_mode2(self):
        session = self._session(has_report=False)
        decision = RouteDecision(mode=2, reason="llm", source="llm")
        final = _apply_guardrails(
            decision,
            "请根据所选模板重新生成一份报告",
            session,
            False,
            False,
        )
        self.assertEqual(final.mode, 0)

    def test_with_report_llm_mode2_preserved(self):
        session = self._session(has_report=True)
        decision = RouteDecision(mode=2, reason="同主题换模板", source="llm")
        final = _apply_guardrails(
            decision,
            "请根据所选模板，重新生成一份报告",
            session,
            False,
            False,
        )
        self.assertEqual(final.mode, 2)
        self.assertEqual(final.source, "llm")

    def test_new_ext_forces_mode0(self):
        session = self._session(has_report=True)
        decision = RouteDecision(mode=2, reason="llm", source="llm")
        final = _apply_guardrails(decision, "分析一下", session, True, False)
        self.assertEqual(final.mode, 0)
        self.assertEqual(final.source, "guardrail")

    def test_template_only_ext_not_case_material(self):
        from singleAgent.ext_utils import ext_has_case_material

        self.assertFalse(
            ext_has_case_material({"selected_knowledge_ids": ["structured_four_section"]})
        )
        self.assertTrue(ext_has_case_material({"content": "某案例正文"}))

    def test_template_only_does_not_force_mode0(self):
        """开模板只传 selected_knowledge_ids 时，不应再被当成「附带新材料」。"""
        from singleAgent.ext_utils import ext_has_case_material

        session = self._session(has_report=True)
        has_ext = ext_has_case_material(
            {"selected_knowledge_ids": ["structured_four_section"]}
        )
        decision = RouteDecision(mode=1, reason="对话", source="llm")
        final = _apply_guardrails(
            decision,
            "你怎么看待美国的决策",
            session,
            has_ext,
            False,
        )
        self.assertEqual(final.mode, 1)
        self.assertEqual(final.source, "llm")

    def test_build_route_prompt_includes_anchor(self):
        from singleAgent.prompts import build_route_user_prompt

        text = build_route_user_prompt(
            instruction="请根据所选模板重新生成",
            has_report=True,
            last_topic="美伊以战争",
            has_ext=False,
            has_files=False,
            anchor_topic="美伊以战争",
        )
        self.assertIn("anchor_topic", text)
        self.assertIn("美伊以战争", text)


if __name__ == "__main__":
    unittest.main()
