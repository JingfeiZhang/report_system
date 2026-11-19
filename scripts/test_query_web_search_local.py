"""Offline search-adapter contract checks; no API credits or real credentials."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from QueryEngine.tools.web_search import WebSearchNewsAgency


class SearchContractTests(unittest.TestCase):
    def client(self, provider):
        return WebSearchNewsAgency(SimpleNamespace(
            QUERY_SEARCH_PROVIDER=provider, ANSPIRE_API_KEY="test-key",
            ANSPIRE_BASE_URL="https://example.invalid/search",
            BOCHA_WEB_SEARCH_API_KEY="test-key",
            QUERY_BOCHA_BASE_URL="https://example.invalid/web-search",
        ))

    @patch("QueryEngine.tools.web_search.requests.post")
    def test_bocha_preserves_evidence_and_date_filter(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"code": 200, "data": {"webPages": {"value": [{
            "name": "Evidence", "url": "https://example.org/article", "summary": "Full summary",
            "snippet": "Short", "datePublished": "2026-11-01", "dateLastCrawled": "2026-11-15",
        }]}}}
        result = self.client("bocha").search_news_by_date("topic", "2026-11-01", "2026-11-15")
        self.assertEqual(post.call_args.kwargs["json"]["freshness"], "2026-11-01..2026-11-15")
        self.assertEqual(result.results[0].content, "Full summary")
        self.assertEqual(result.results[0].published_date, "2026-11-01")
        self.assertEqual(result.results[0].url, "https://example.org/article")

    @patch("QueryEngine.tools.web_search.requests.get")
    def test_anspire_date_filter_and_no_invented_images(self, get):
        get.return_value = Mock(status_code=200)
        get.return_value.json.return_value = {"results": [{"title": "Evidence", "url": "https://example.org", "content": "Body"}]}
        client = self.client("anspire")
        result = client.search_news_by_date("topic", "2026-11-01", "2026-11-15")
        self.assertEqual(get.call_args.kwargs["params"]["ToTime"], "2026-11-15 23:59:59")
        self.assertIsNone(result.results[0].published_date)
        self.assertEqual(client.search_images_for_news("topic").images, [])
        client.search_news_last_week("topic")
        params = get.call_args.kwargs["params"]
        self.assertLess(params["FromTime"], params["ToTime"])

    @patch("QueryEngine.tools.web_search.requests.post")
    def test_api_errors_are_not_empty_success(self, post):
        post.return_value = Mock(status_code=403)
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            self.client("bocha").basic_search_news("topic")
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"code": 403}
        with self.assertRaisesRegex(RuntimeError, "API error"):
            self.client("bocha").basic_search_news("topic")
        post.return_value.json.return_value = {"code": 200, "data": {}}
        with self.assertRaisesRegex(RuntimeError, "webPages"):
            self.client("bocha").basic_search_news("topic")


if __name__ == "__main__":
    unittest.main()
