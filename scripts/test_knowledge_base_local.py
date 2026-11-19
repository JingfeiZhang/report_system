#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机知识库模块测试（无需启动 FastAPI）。"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_API_KEY = os.getenv(
    "KNOWLEDGE_BASE_API_KEY",
    "ak_5jo_5FgKOyBjY1UynKeAOp0IFNvpnq9UNUph_UyDFTE",
)
DEFAULT_BASE = os.getenv("KNOWLEDGE_BASE_API_BASE_URL", "http://192.168.1.10:8081")
DEFAULT_DATASET_IDS = [
    "4ead8d0665fe11f196d82978a39a5161",
    "7cbd869a1cea11f193343512025a46c9",
    "416d8ca4631011f196d82978a39a5161",
]


def _apply_test_env() -> None:
    os.environ.setdefault("KNOWLEDGE_BASE_API_BASE_URL", DEFAULT_BASE)
    os.environ.setdefault("KNOWLEDGE_BASE_API_KEY", DEFAULT_API_KEY)
    os.environ.setdefault("SINGLE_AGENT_THINKING_PROGRESS_DELAY_SEC", "0")
    os.environ.setdefault("SINGLE_AGENT_SEARCH_ROUND_DELAY_SEC", "0")
    os.environ.setdefault("SINGLE_AGENT_PHASE_GAP_DELAY_SEC", "0")
    os.environ.setdefault("SINGLE_AGENT_PRE_LLM_DELAY_SEC", "0")
    os.environ.setdefault("SINGLE_AGENT_THINKING_CHUNK_DELAY_SEC", "0")


_apply_test_env()

from singleAgent.context import build_fast_context  # noqa: E402
from singleAgent.knowledge_base import (  # noqa: E402
    KnowledgeBundle,
    _build_summary,
    normalize_knowledge_base_ids,
    retrieve,
)
from singleAgent.report_pipeline import run_report_pipeline  # noqa: E402
from singleAgent.session_store import SessionStore  # noqa: E402

_MOCK_SEARCH_OK: Optional[bool] = None


def is_mock_search_ok() -> bool:
    global _MOCK_SEARCH_OK
    if _MOCK_SEARCH_OK is None:
        try:
            r = httpx.get("http://127.0.0.1:8787/health", timeout=5)
            _MOCK_SEARCH_OK = r.status_code == 200
        except Exception:
            _MOCK_SEARCH_OK = False
    return _MOCK_SEARCH_OK


class TestKnowledgeBaseUnit(unittest.TestCase):
    def test_normalize_dataset_ids(self):
        ids = normalize_knowledge_base_ids(
            ["4ead8d0665fe11f196d82978a39a5161", "  ", "7cbd869a1cea11f193343512025a46c9", "4ead8d0665fe11f196d82978a39a5161"]
        )
        self.assertEqual(
            ids,
            [
                "4ead8d0665fe11f196d82978a39a5161",
                "7cbd869a1cea11f193343512025a46c9",
            ],
        )

    def test_build_context_with_kb_summary(self):
        ctx = build_fast_context(
            topic="美伊以战争",
            query="美伊以战争",
            metadata={
                "instruction": "美伊以战争",
                "knowledge_base_ids": DEFAULT_DATASET_IDS,
            },
            search_summary="库内命中 5 条",
            knowledge_summary="知识库命中 2 条",
        )
        self.assertIn("【知识库检索摘要】", ctx.user_prompt)
        self.assertIn("知识库命中 2 条", ctx.user_prompt)
        self.assertIn("【库内检索摘要】", ctx.user_prompt)

    def test_summary_format_empty(self):
        text = _build_summary(
            dataset_ids=[DEFAULT_DATASET_IDS[0]],
            chunks=[],
        )
        self.assertIn("暂无与主题匹配", text)
        self.assertIn(DEFAULT_DATASET_IDS[0], text)


class TestKnowledgeBaseLive(unittest.TestCase):
    def test_mock_search_health(self):
        self.assertTrue(is_mock_search_ok(), "mock_search_api 未在 8787 端口运行")

    def test_retrieval_open_api(self):
        headers = {
            "Authorization": f"Bearer {DEFAULT_API_KEY}",
            "Content-Type": "application/json",
        }
        body = {
            "question": "美伊以战争",
            "dataset_ids": [DEFAULT_DATASET_IDS[0]],
        }
        r = httpx.post(
            f"{DEFAULT_BASE}/api/v1/dataplatform/knowledge-base/open/retrieval",
            headers=headers,
            json=body,
            timeout=90,
        )
        self.assertEqual(r.status_code, 200, r.text[:300])
        data = r.json().get("data") or {}
        chunks = data.get("chunks") or []
        self.assertGreater(len(chunks), 0, "retrieval 应返回 chunks")

    def test_retrieve_with_dataset_ids(self):
        bundle = retrieve("美伊以战争", DEFAULT_DATASET_IDS)
        print("\n[retrieve] summary:\n", bundle.summary_text[:500])
        self.assertFalse(bundle.error, bundle.error)
        self.assertEqual(bundle.dataset_ids, DEFAULT_DATASET_IDS)
        if bundle.empty:
            self.skipTest("知识库无匹配片段（库内容为空）")
        self.assertGreater(bundle.total_count, 0)


class TestReportPipelineKB(unittest.TestCase):
    def test_pipeline_emits_knowledge_stage(self):
        if not is_mock_search_ok():
            self.skipTest("mock 未运行，跳过 pipeline 测试")

        event = SimpleNamespace(
            topic="美伊以战争",
            query="美伊以战争",
            metadata={
                "instruction": "美伊以战争",
                "session_id": f"kb-test-{int(time.time())}",
                "knowledge_base_ids": DEFAULT_DATASET_IDS,
            },
        )
        store = SessionStore(persist_dir=None)
        config = {
            "INSIGHT_ENGINE_API_KEY": "EMPTY",
            "INSIGHT_ENGINE_BASE_URL": "http://127.0.0.1:9",
            "INSIGHT_ENGINE_MODEL_NAME": "test-model",
        }

        captured: dict = {}

        def fake_stream(**kwargs):
            captured.update(kwargs)
            yield "# 美伊以战争\n\n## 一、事件与背景\n测试\n"

        sse_text = []
        with patch("singleAgent.report_pipeline.stream_chat", side_effect=fake_stream):
            gen = run_report_pipeline(
                event,
                config,
                project_root=ROOT,
                session_store=store,
                session_id=event.metadata["session_id"],
                started=time.time(),
            )
            for line in gen:
                sse_text.append(line)

        blob = "".join(sse_text)
        self.assertIn("计划检索知识库", blob)
        self.assertIn('"stage": "knowledge"', blob)
        self.assertIn("知识库检索摘要", blob)
        user_prompt = captured.get("user_prompt") or ""
        self.assertIn("【知识库检索摘要】", user_prompt, "LLM prompt 应含知识库摘要")
        print("\n[pipeline] SSE 片段数:", len(sse_text))


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestKnowledgeBaseUnit))
    suite.addTests(loader.loadTestsFromTestCase(TestKnowledgeBaseLive))
    suite.addTests(loader.loadTestsFromTestCase(TestReportPipelineKB))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print("\n" + "=" * 60)
    print(
        f"总计: {result.testsRun}  失败: {len(result.failures)}  "
        f"错误: {len(result.errors)}  跳过: {len(result.skipped)}"
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
