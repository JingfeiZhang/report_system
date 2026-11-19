"""Adapt Anspire and Bocha Web Search to Query's existing search result types."""
from datetime import datetime, timedelta

import requests

from .search import SearchResult, TavilyNewsAgency, TavilyResponse


class WebSearchNewsAgency(TavilyNewsAgency):
    """Reuse the six Query tool methods with another web search provider."""

    def __init__(self, config):
        self.provider = config.QUERY_SEARCH_PROVIDER
        if self.provider == "anspire":
            self.url = config.ANSPIRE_BASE_URL
            key = config.ANSPIRE_API_KEY
        elif self.provider == "bocha":
            self.url = config.QUERY_BOCHA_BASE_URL
            key = config.BOCHA_WEB_SEARCH_API_KEY
        else:
            raise ValueError("Unsupported web search provider")
        if not key:
            raise ValueError(f"Missing {self.provider} search API key")
        self.headers = {"Authorization": f"Bearer {key}"}

    def _search_internal(self, **kwargs):
        query = kwargs["query"]
        count = min(max(int(kwargs.get("max_results", 10)), 1), 50)
        start, end = kwargs.get("start_date"), kwargs.get("end_date")
        time_range = kwargs.get("time_range")
        if self.provider == "anspire":
            payload = {"query": query, "top_k": count}
            if time_range:
                end_time = datetime.now()
                start_time = end_time - timedelta(days=1 if time_range == "d" else 7)
                start, end = start_time.strftime("%Y-%m-%d %H:%M:%S"), end_time.strftime("%Y-%m-%d %H:%M:%S")
            if start:
                payload["FromTime"] = start if " " in start else start + " 00:00:00"
            if end:
                payload["ToTime"] = end if " " in end else end + " 23:59:59"
        else:
            freshness = {"d": "oneDay", "w": "oneWeek"}.get(time_range, "noLimit")
            if start and end:
                freshness = f"{start}..{end}"
            payload = {"query": query, "count": count, "summary": True, "freshness": freshness}
        try:
            if self.provider == "anspire":
                response = requests.get(self.url, headers=self.headers, params=payload, timeout=30)
            else:
                response = requests.post(self.url, headers=self.headers, json=payload, timeout=30)
        except requests.RequestException:
            raise RuntimeError(f"{self.provider} search network request failed") from None
        if response.status_code != 200:
            raise RuntimeError(f"{self.provider} search returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(f"{self.provider} search returned invalid JSON") from None
        if self.provider == "anspire":
            if not isinstance(data.get("results"), list):
                raise RuntimeError("Anspire search returned no results field")
            items = data["results"]
        else:
            if str(data.get("code", 200)) != "200":
                raise RuntimeError("Bocha search returned an API error")
            pages = (data.get("data") or {}).get("webPages") or {}
            if not isinstance(pages.get("value"), list):
                raise RuntimeError("Bocha search returned no webPages field")
            items = pages["value"]
        results = [SearchResult(
            title=item.get("title") or item.get("name") or "",
            url=item.get("url") or "",
            content=item.get("content") or item.get("summary") or item.get("snippet") or "",
            score=item.get("score"),
            published_date=item.get("date") or item.get("datePublished"),
        ) for item in items[:count] if item.get("url")]
        # These web endpoints provide page evidence, not verified image results.
        return TavilyResponse(query=query, results=results)
