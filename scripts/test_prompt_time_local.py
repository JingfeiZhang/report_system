"""提示词当前时间 / 时态纪律单元测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestPromptCurrentTime(unittest.TestCase):
    def test_format_now_shape(self):
        from singleAgent.prompts import current_time_block, format_now_for_prompt

        now = format_now_for_prompt()
        self.assertRegex(now, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        block = current_time_block()
        self.assertTrue(block.startswith("【当前时间】"))
        self.assertIn(now, block)

    def test_report_prompts_include_time_and_discipline(self):
        from singleAgent.prompts import FACT_DISCIPLINE, build_system_prompt, build_user_prompt

        self.assertIn("【当前时间】", FACT_DISCIPLINE)
        self.assertIn("日期待考", FACT_DISCIPLINE)

        system = build_system_prompt(
            topic="美伊战争",
            template_id="decision_memo",
            project_root=ROOT,
        )
        self.assertIn("【当前时间】", system)
        self.assertIn("Asia/Shanghai", system)
        self.assertIn("不得按训练知识臆测今天是哪一天", system)

        user = build_user_prompt("【分析主题】\n美伊战争")
        self.assertTrue(user.startswith("【当前时间】"))
        self.assertIn("【分析主题】", user)
        # 幂等：已含当前时间时不再叠加
        again = build_user_prompt(user)
        self.assertEqual(again.count("【当前时间】"), 1)

    def test_chat_prompts_include_time_and_discipline(self):
        from singleAgent.prompts import (
            CHAT_SYSTEM_PROMPT,
            build_chat_system_prompt,
            build_chat_user_prompt,
        )

        self.assertIn("【当前时间】", CHAT_SYSTEM_PROMPT)
        self.assertIn("最新进展", CHAT_SYSTEM_PROMPT)

        system = build_chat_system_prompt()
        self.assertIn("【当前时间】", system)
        self.assertRegex(system, r"【当前时间】\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

        user = build_chat_user_prompt(
            instruction="谈谈近期态势",
            topic="美伊战争",
            last_report_md="",
            recent_messages=[],
            search_summary="1. [weibo] 2026-03-09 | 样例",
        )
        self.assertTrue(user.startswith("【当前时间】"))
        self.assertIn("【达梦检索摘要】", user)

    def test_revise_user_prompt_includes_time(self):
        from singleAgent.prompts import build_revise_user_prompt

        user = build_revise_user_prompt(
            instruction="按模板重写",
            topic="美伊战争",
            last_report_md="# 旧报告",
            last_search_summary="",
            recent_messages=[],
        )
        self.assertTrue(user.startswith("【当前时间】"))


if __name__ == "__main__":
    unittest.main()
