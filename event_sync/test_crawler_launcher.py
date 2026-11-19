#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crawler_launcher HTTP 后端冒烟测试（mock requests）。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from crawler_launcher import launch_crawlers


class HttpBackendTests(unittest.TestCase):
    def test_http_create_poll_success(self):
        config = {
            "CRAWLER_ENABLED": "true",
            "CRAWLER_BACKEND": "http",
            "MINDSPIDER_API_BASE": "http://127.0.0.1:8082/api/v1",
            "CRAWLER_PLATFORMS": "wb,xhs",
            "CRAWLER_MAX_KEYWORDS": "2",
            "CRAWLER_MAX_NOTES": "3",
            "CRAWLER_POLL_INTERVAL": "0",
            "CRAWLER_POLL_TIMEOUT": "60",
            "CRAWLER_DOWNLOAD": "false",
        }
        keywords = {"2024-01-01": ["事件A", "事件B", "事件C"]}

        create_resp = MagicMock()
        create_resp.raise_for_status = MagicMock()
        create_resp.json.return_value = {
            "task_id": "task_demo_1",
            "status": "pending",
        }

        poll_resp = MagicMock()
        poll_resp.raise_for_status = MagicMock()
        poll_resp.json.return_value = {
            "task_id": "task_demo_1",
            "status": "success",
            "download_url": "http://127.0.0.1:8082/api/v1/crawl/tasks/task_demo_1/download",
            "stats": {"total_notes": 1},
        }

        with patch("crawler_launcher.requests.post", return_value=create_resp) as post:
            with patch("crawler_launcher.requests.get", return_value=poll_resp) as get:
                with patch("crawler_launcher.time.sleep"):
                    ok = launch_crawlers(config, ["2024-01-01"], keywords)

        self.assertEqual(ok, ["2024-01-01"])
        post.assert_called_once()
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["keywords"], ["事件A", "事件B"])
        self.assertEqual(body["platforms"], ["wb", "xhs"])
        self.assertEqual(body["max_notes"], 3)
        get.assert_called()

    def test_result_folder_name(self):
        from crawler_launcher import _result_folder_name

        cfg = {"_INPUT_DAY_COMPACT": "20260809"}
        name = _result_folder_name(
            cfg,
            "2026-08-09",
            {"id": 396733, "title": "示例", "event_date": "2026-08-09"},
        )
        self.assertEqual(name, "2026/08/09/20260809-396733")

    def test_disabled_skips(self):
        ok = launch_crawlers({"CRAWLER_ENABLED": "false"}, ["2024-01-01"], {})
        self.assertEqual(ok, [])

    def test_read_cookie_string_picks_longest(self):
        from crawler_launcher import _read_cookie_string
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.txt"
            p.write_text("# comment\nA=1\nSUB=abc; SUBP=def; long=value\n", encoding="utf-8")
            s = _read_cookie_string(p)
            self.assertIn("SUB=", s)
            self.assertGreater(len(s), 5)


if __name__ == "__main__":
    unittest.main()
