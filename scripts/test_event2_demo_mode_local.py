"""EVENT2_DEMO_MODE 演示/严肃纪律切换单元测试。"""

from __future__ import annotations

import importlib
import os
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class TestEvent2DemoMode(unittest.TestCase):
    def _reload(self, *, demo: bool):
        env = {"EVENT2_DEMO_MODE": "true" if demo else "false"}
        with mock.patch.dict(os.environ, env, clear=False):
            import singleAgent.config as cfg
            import singleAgent.prompts as prompts

            importlib.reload(cfg)
            importlib.reload(prompts)
            return cfg, prompts

    def tearDown(self):
        # 恢复为当前进程 .env / 默认，避免污染其它用例
        import singleAgent.config as cfg
        import singleAgent.prompts as prompts

        importlib.reload(cfg)
        importlib.reload(prompts)

    def test_demo_false_uses_honest_discipline(self):
        cfg, prompts = self._reload(demo=False)
        self.assertFalse(cfg.EVENT2_DEMO_MODE)
        self.assertFalse(prompts.is_demo_mode())
        self.assertIn("现有材料未提供", prompts.active_fact_discipline())
        self.assertNotIn("演示模式", prompts.active_fact_discipline())

        system = prompts.build_system_prompt(
            topic="测试",
            template_id="qa_conversational",
            project_root=ROOT,
        )
        self.assertIn("严肃模式", system)
        self.assertIn("现有材料未提供", system)

        chat = prompts.build_chat_system_prompt()
        self.assertIn("严肃模式", chat)
        self.assertIn("依据不足", chat)

    def test_demo_true_uses_demo_discipline(self):
        cfg, prompts = self._reload(demo=True)
        self.assertTrue(cfg.EVENT2_DEMO_MODE)
        self.assertTrue(prompts.is_demo_mode())
        self.assertIn("演示模式", prompts.active_fact_discipline())
        self.assertIn("禁止编造未列出的标号", prompts.active_fact_discipline())
        self.assertNotIn("现有材料未提供", prompts.active_fact_discipline())

        system = prompts.build_system_prompt(
            topic="天安门撞车",
            template_id="qa_conversational",
            project_root=ROOT,
        )
        self.assertIn("演示模式", system)
        self.assertIn("禁止伪造溯源", system)

        chat = prompts.build_chat_system_prompt()
        self.assertIn("演示模式", chat)
        self.assertIn("勿只回复", chat)

        revise = prompts.build_revise_system_prompt()
        self.assertIn("演示模式", revise)


if __name__ == "__main__":
    unittest.main()
