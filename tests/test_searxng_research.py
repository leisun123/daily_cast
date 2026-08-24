"""SearXNG-backed discovery for DailyCast briefing sources."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx

from dailycast.llm.contracts import LLMMessage
from dailycast.sources.research import SearxngWebResearchProvider, WebResearchCandidateSet


def test_searxng_research_provider_turns_search_hits_into_bounded_candidates() -> None:
    """The independent search backend must preserve direct links and snippets for verification."""
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "运营商发布网络升级计划",
                        "url": "https://operator.example.test/network-upgrade",
                        "content": "公告披露网络升级的覆盖范围与交付安排。",
                        "publishedDate": "2026-08-24T08:00:00+08:00",
                    }
                ]
            },
            request=request,
        )

    async def scenario() -> object:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = SearxngWebResearchProvider(
                base_url="https://search.example.test",
                timeout_seconds=10,
                http_client=client,
            )
            return await provider.generate_web_research(
                (LLMMessage(role="user", content="ignored by the direct search backend"),),
                WebResearchCandidateSet,
                {"search_query": "中国移动 网络建设 过去24小时"},
            )

    result = asyncio.run(scenario())

    request = captured["request"]
    assert request.url.path == "/search"
    assert parse_qs(request.url.query.decode())["q"] == ["中国移动 网络建设 过去24小时"]
    assert result.model == "searxng"
    assert result.content == {
        "candidates": [
            {
                "title": "运营商发布网络升级计划",
                "url": "https://operator.example.test/network-upgrade",
                "publisher": "operator.example.test",
                "finding": "公告披露网络升级的覆盖范围与交付安排。",
                "published_at_hint": "2026-08-24T08:00:00+08:00",
            }
        ]
    }
