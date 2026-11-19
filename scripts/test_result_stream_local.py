"""result 流 chunk/aux 分类单元测试。"""

from __future__ import annotations

import unittest

from singleAgent.result_stream import (
    ResultStreamClassifier,
    extract_body_markdown,
    iter_classified_result_chunks,
)


class TestResultStreamClassifier(unittest.TestCase):
    def test_disclaimer_split_after_body(self):
        text = "# 标题\n\n正文段落。\n\n**免责声明：**本内容仅供参考。"
        pieces = list(iter_classified_result_chunks([text]))
        types = [p.stream_type for p in pieces]
        self.assertIn("chunk", types)
        self.assertIn("aux", types)
        aux = [p for p in pieces if p.stream_type == "aux"]
        self.assertTrue(aux)
        self.assertEqual(aux[0].aux_kind, "disclaimer")
        self.assertIn("仅供参考", "".join(p.text for p in aux))

    def test_flexible_style_body_still_splits_aux(self):
        """无四段结构的自然段报告，免责声明仍应 type=aux。"""
        text = "近期局势分析……\n\n【免责声明】不构成投资建议。"
        pieces = list(iter_classified_result_chunks([text]))
        self.assertEqual(pieces[0].stream_type, "chunk")
        aux = [p for p in pieces if p.stream_type == "aux"]
        self.assertEqual(len(aux), 1)
        self.assertEqual(aux[0].aux_kind, "disclaimer")

    def test_extract_body_excludes_aux(self):
        full = "## 一、背景\n内容\n\n**备注：**测试备注"
        body = extract_body_markdown(full)
        self.assertIn("背景", body)
        self.assertNotIn("测试备注", body)

    def test_streaming_feed_preserves_aux(self):
        streamed: list = []
        clf = ResultStreamClassifier()
        for piece in clf.feed("正文\n\n"):
            streamed.append(piece)
        for piece in clf.feed("**小结：**要点"):
            streamed.append(piece)
        for piece in clf.flush():
            streamed.append(piece)
        self.assertTrue(any(p.stream_type == "chunk" for p in streamed))
        aux = [p for p in streamed if p.stream_type == "aux"]
        self.assertEqual(len(aux), 1)
        self.assertEqual(aux[0].aux_kind, "summary")


if __name__ == "__main__":
    unittest.main()
