"""溯源 tip / (标N) 流式检测单元测试。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from types import SimpleNamespace

from singleAgent.citations import (
    CitationTip,
    CitationTipEmitter,
    build_citation_tips,
    format_citation_catalog,
)
from singleAgent.dm_search import SearchBundle
from singleAgent.knowledge_base import KnowledgeBundle, KnowledgeChunk
from singleAgent.sse import emit_tip


def _parse_tip_frame(frame: str) -> dict:
    lines = [ln for ln in frame.strip().splitlines() if ln.startswith("data:")]
    assert lines, frame
    return json.loads(lines[0][5:].strip())


class TestEmitTip(unittest.TestCase):
    def test_tip_contract_fields(self):
        frame = emit_tip(
            label="标5",
            block_id="bcdce5ca11df251c",
            dataset_name="test",
            document_name="三体.md",
            text="切片内容",
            published_at="2026-03-12",
        )
        self.assertTrue(frame.startswith("event: tip\n"))
        data = _parse_tip_frame(frame)
        self.assertEqual(data["type"], "tip")
        self.assertEqual(data["index"], 0)
        delta = data["delta"]
        self.assertEqual(delta["type"], "text_delta")
        self.assertEqual(delta["label"], "标5")
        self.assertEqual(delta["block_id"], "bcdce5ca11df251c")
        self.assertEqual(delta["dataset_name"], "test")
        self.assertEqual(delta["document_name"], "三体.md")
        self.assertEqual(delta["text"], "切片内容")
        self.assertEqual(delta["published_at"], "2026-03-12")

    def test_omit_empty_published_at(self):
        frame = emit_tip(
            label="标1",
            block_id="abc",
            dataset_name="舆情库/weibo",
            document_name="标题",
            text="正文",
        )
        data = _parse_tip_frame(frame)
        self.assertNotIn("published_at", data["delta"])


class TestBuildCitationTips(unittest.TestCase):
    def test_dm_then_kb_numbering(self):
        hit = SimpleNamespace(
            platform="weibo",
            title_or_content="伊朗局势相关讨论帖",
            url="https://example.com/1",
            publish_time=datetime(2026, 3, 12),
        )
        kb = KnowledgeBundle(
            chunks=[
                KnowledgeChunk(
                    content="知识库切片A",
                    document_keyword="手册.md",
                    dataset_id="ds1",
                    similarity=0.9,
                )
            ],
            empty=False,
        )
        tips = build_citation_tips(
            search_bundle=SearchBundle(hits=[hit], empty=False),
            kb_bundle=kb,
        )
        self.assertEqual([t.label for t in tips], ["标1", "标2"])
        self.assertTrue(tips[0].dataset_name.startswith("舆情库/"))
        self.assertEqual(tips[0].published_at, "2026-03-12")
        self.assertIn("创建日期：2026-03-12", tips[0].text)
        self.assertTrue(tips[1].dataset_name.startswith("知识库/"))
        self.assertEqual(tips[1].document_name, "手册.md")
        self.assertIn("创建日期：待考", tips[1].text)
        self.assertIn("知识库切片A", tips[1].text)
        catalog = format_citation_catalog(tips)
        self.assertIn("(标1)", catalog)
        self.assertIn("(标2)", catalog)
        self.assertIn("【溯源标注】", catalog)

    def test_dm_title_not_equal_full_body(self):
        long = (
            "推演！美伊战争，即便北约下场，美国极有可能经此一役，霸权尽丧。"
            "详细推演过程如下：首先分析海峡封锁，其次讨论补给线。"
        )
        hit = SimpleNamespace(
            platform="bilibili",
            title_or_content=long,
            url="https://example.com/b",
            publish_time=datetime(2026, 6, 1),
        )
        tips = build_citation_tips(search_bundle=SearchBundle(hits=[hit], empty=False))
        self.assertEqual(len(tips), 1)
        tip = tips[0]
        self.assertEqual(tip.document_name, "推演！美伊战争，即便北约下场，美国极有可能经此一役，霸权尽丧")
        self.assertNotEqual(tip.document_name, tip.text)
        self.assertTrue(tip.text.startswith("创建日期：2026-06-01\n"))
        self.assertIn("详细推演过程如下", tip.text)
        self.assertEqual(tip.dataset_name, "舆情库/bilibili")


class TestCitationTipEmitter(unittest.TestCase):
    def _tips(self) -> list[CitationTip]:
        return [
            CitationTip(
                label="标1",
                block_id="id1",
                dataset_name="舆情库/weibo",
                document_name="帖A",
                text="摘要A",
                published_at="2026-01-01",
            ),
            CitationTip(
                label="标2",
                block_id="id2",
                dataset_name="知识库/ds",
                document_name="doc.md",
                text="摘要B",
            ),
        ]

    def test_emit_tip_once_when_marker_completes(self):
        emitter = CitationTipEmitter(self._tips())
        out: list[tuple[str, str]] = []
        out.extend(emitter.feed("结论成立"))
        out.extend(emitter.feed("(标"))
        out.extend(emitter.feed("1)。另见(标2)。再提(标1)。"))
        out.extend(emitter.flush())

        texts = [p for k, p in out if k == "text"]
        tips = [p for k, p in out if k == "tip"]
        self.assertEqual("".join(texts), "结论成立(标1)。另见(标2)。再提(标1)。")
        self.assertEqual(len(tips), 2)
        labels = [_parse_tip_frame(f)["delta"]["label"] for f in tips]
        self.assertEqual(labels, ["标1", "标2"])

    def test_remap_by_appearance_order(self):
        """正文先出现目录标2再标1 → 展示为标1、标2。"""
        emitter = CitationTipEmitter(self._tips())
        out = list(emitter.feed("先引(标2)，再引(标1)，又引(标2)。"))
        out.extend(emitter.flush())
        texts = "".join(p for k, p in out if k == "text")
        self.assertEqual(texts, "先引(标1)，再引(标2)，又引(标1)。")
        tips = [p for k, p in out if k == "tip"]
        labels = [_parse_tip_frame(f)["delta"]["label"] for f in tips]
        self.assertEqual(labels, ["标1", "标2"])
        # tip 内容仍对应原目录材料：先出现的标2→展示标1，block 为 id2
        self.assertEqual(_parse_tip_frame(tips[0])["delta"]["block_id"], "id2")
        self.assertEqual(_parse_tip_frame(tips[1])["delta"]["block_id"], "id1")

    def test_remap_function_on_full_text(self):
        from singleAgent.citations import remap_citation_markers_by_appearance

        raw = "依据(标6)。补充(标1)(标9)。再提(标6)。"
        self.assertEqual(
            remap_citation_markers_by_appearance(raw),
            "依据(标1)。补充(标2)(标3)。再提(标1)。",
        )

    def test_unknown_marker_no_tip(self):
        emitter = CitationTipEmitter(self._tips())
        out = list(emitter.feed("无依据(标9)。"))
        self.assertEqual(len([p for k, p in out if k == "tip"]), 0)
        # 未知编号仍按出现序重排展示
        self.assertEqual("".join(p for k, p in out if k == "text"), "无依据(标1)。")

    def test_empty_tips_passthrough(self):
        emitter = CitationTipEmitter([])
        out = list(emitter.feed("正文(标1)"))
        self.assertEqual("".join(p for k, p in out if k == "text"), "正文(标1)")
        self.assertEqual(len([p for k, p in out if k == "tip"]), 0)

    def test_emitted_payloads_for_history(self):
        """历史写入应与 SSE event:tip 的 data 同构，而不是只有角标文本。"""
        from singleAgent.citations import citation_tip_payload
        from singleAgent.session_store import _platform_metadata

        emitter = CitationTipEmitter(self._tips())
        list(emitter.feed("先引(标2)，再引(标1)。"))
        payloads = emitter.emitted_payloads()
        self.assertEqual([p["type"] for p in payloads], ["tip", "tip"])
        self.assertEqual([p["index"] for p in payloads], [0, 1])
        self.assertEqual([p["delta"]["label"] for p in payloads], ["标1", "标2"])
        self.assertEqual(payloads[0]["delta"]["type"], "text_delta")
        self.assertEqual(payloads[0]["delta"]["block_id"], "id2")
        self.assertEqual(payloads[1]["delta"]["block_id"], "id1")
        self.assertEqual(payloads[0]["delta"]["document_name"], "doc.md")
        self.assertIn("摘要B", payloads[0]["delta"]["text"])

        citations = emitter.emitted_citations()
        self.assertEqual([c["label"] for c in citations], ["标1", "标2"])
        self.assertEqual(citations[0]["marker"], "(标1)")
        self.assertEqual(citations[0]["id"], "标1")
        self.assertEqual(citations[0]["block_id"], "id2")
        self.assertEqual(citations[0]["title"], "doc.md")
        self.assertEqual(citations[0]["content"], citations[0]["text"])
        self.assertIn("摘要B", citations[0]["text"])

        trimmed = _platform_metadata({
            "mode": 0,
            "tips": payloads,
            "citations": citations,
            "citation_count": len(citations),
            "search": {"huge": "x"},
        })
        self.assertIn("tips", trimmed)
        self.assertIn("citations", trimmed)
        self.assertEqual(trimmed["citation_count"], 2)
        self.assertEqual(len(trimmed["tips"]), 2)
        self.assertEqual(len(trimmed["citations"]), 2)
        self.assertNotIn("search", trimmed)
        self.assertEqual(trimmed["tips"][0]["type"], "tip")
        self.assertEqual(trimmed["tips"][0]["delta"]["label"], "标1")
        self.assertEqual(trimmed["citations"][0]["marker"], "(标1)")
        self.assertEqual(
            citation_tip_payload(self._tips()[1], display_label="标1")["block_id"],
            "id2",
        )


if __name__ == "__main__":
    unittest.main()
