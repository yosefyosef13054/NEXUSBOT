from __future__ import annotations

import json
from typing import Type

import httpx
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


class WebSearchInput(BaseModel):
    query: str = Field(description="Web search query.")
    max_results: int = Field(default=5, ge=1, le=10)


class WebSearchTool(BaseTool):
    """Web search via DuckDuckGo Instant Answer — no API key required.

    Replace with Tavily/Serper/Bing by editing only `_search`.
    """

    name: str = "web_search"
    description: str = (
        "Searches the web for up-to-date information. "
        "Use only when the answer depends on recent or external knowledge."
    )
    args_schema: Type[BaseModel] = WebSearchInput

    async def _search(self, query: str, max_results: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1"},
                headers={"User-Agent": "NexusBot/1.0"},
            )
            r.raise_for_status()
            data = r.json()
        results: list[dict] = []
        for topic in data.get("RelatedTopics", [])[: max_results * 2]:
            if "FirstURL" in topic and "Text" in topic:
                results.append({"title": topic["Text"], "url": topic["FirstURL"]})
            if len(results) >= max_results:
                break
        return results

    def _run(self, query: str, max_results: int = 5) -> str:  # type: ignore[override]
        raise NotImplementedError("Use async invocation.")

    async def _arun(self, query: str, max_results: int = 5) -> str:  # type: ignore[override]
        try:
            return json.dumps(await self._search(query, max_results))
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": str(e)})
