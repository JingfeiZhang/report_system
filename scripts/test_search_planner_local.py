"""search_planner 解析与过滤单元测试（无 LLM）。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from singleAgent.search_planner import (
    SearchPlanningError,
    _clean_topic,
    _normalize_terms,
    _parse_json_object,
    extract_topic_via_llm,
    normalize_topic,
)


class TestSearchPlannerParse(unittest.TestCase):
    def test_parse_topic_json(self):
        data = _parse_json_object('{"topic":"美伊以战争","reason":"嵌入祈使句"}')
        self.assertEqual(data["topic"], "美伊以战争")

    def test_clean_topic_strips_report_suffix(self):
        self.assertEqual(_clean_topic("美伊以战争的报告"), "美伊以战争")
        self.assertEqual(_clean_topic("《美加墨世界杯报告》。"), "美加墨世界杯")

    def test_placeholder_topics_are_rejected_without_substring_false_positive(self):
        placeholders = (
            "未知",
            "未知主题。",
            "（未命名主题）。",
            "《分析主题》",
            "分析报告！",
            "未指定",
            "无",
        )
        for value in placeholders:
            with self.subTest(value=value):
                self.assertEqual(normalize_topic(value), "")
        self.assertEqual(normalize_topic("未知风险事件"), "未知风险事件")

    def test_invalid_model_topic_falls_back_to_explicit_chat_instruction(self):
        with (
            patch("singleAgent.search_planner.complete_chat", return_value='{"topic":"未知"}'),
            patch("singleAgent.search_planner.plan_search_terms", return_value=["美加墨世界杯报告"]),
        ):
            topic, source = extract_topic_via_llm(
                instruction="请分析美加墨世界杯报告",
                session_topic="未知主题",
                conversation_context=[],
                llm_cfg={"api_key": "x", "base_url": "http://llm", "model_name": "m"},
            )

        self.assertEqual((topic, source), ("美加墨世界杯", "fallback"))

    def test_invalid_model_topic_is_fatal_for_strict_report(self):
        with patch(
            "singleAgent.search_planner.complete_chat",
            return_value='{"topic":"（未知）。"}',
        ):
            with self.assertRaisesRegex(SearchPlanningError, "未返回有效事件"):
                extract_topic_via_llm(
                    instruction="按模板生成报告",
                    session_topic="未知",
                    conversation_context=[
                        {"role": "user", "content": "请分析美加墨世界杯"},
                    ],
                    llm_cfg={"api_key": "x", "base_url": "http://llm", "model_name": "m"},
                    strict_context=True,
                )

    def test_normalize_drops_meta_and_nongrams(self):
        terms = _normalize_terms(
            ["请生成", "生成美", "成美伊", "美伊以战争", "伊朗"],
            topic="美伊以战争",
        )
        self.assertIn("美伊以战争", terms)
        self.assertIn("伊朗", terms)
        self.assertNotIn("请生成", terms)
        self.assertNotIn("生成美", terms)

    def test_fallback_from_topic_when_empty(self):
        terms = _normalize_terms([], topic="美伊以战争")
        self.assertTrue(terms)
        self.assertTrue(any("美" in t or "伊" in t or "战" in t for t in terms))


if __name__ == "__main__":
    unittest.main()
