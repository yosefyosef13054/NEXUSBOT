from __future__ import annotations

from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from app.utils.web import UnsafeUrlError, fetch_url_safely, html_to_text


class FetchUrlInput(BaseModel):
    url: str = Field(description="Absolute http(s) URL of a public web page to read.")
    max_chars: int = Field(
        default=4000, ge=200, le=20000, description="Maximum characters of text to return."
    )


class FetchUrlTool(BaseTool):
    """Reads a public web page and returns its visible text.

    Makes an outbound request on the user's behalf, so it is gated behind approval by
    default (see `app/ai/tools/registry.py`). Private/loopback destinations are refused.
    """

    name: str = "fetch_url"
    description: str = (
        "Fetches a public web page and returns its text content. Use it to read an article, "
        "documentation page or any URL the user mentions."
    )
    args_schema: Type[BaseModel] = FetchUrlInput

    def _run(self, url: str, max_chars: int = 4000) -> str:  # type: ignore[override]
        raise NotImplementedError("Use async invocation.")

    async def _arun(self, url: str, max_chars: int = 4000) -> str:  # type: ignore[override]
        try:
            resource = await fetch_url_safely(url)
        except UnsafeUrlError as e:
            return f"Could not fetch URL: {e}"
        except Exception as e:  # noqa: BLE001
            return f"Could not fetch URL: {str(e)[:200]}"
        if resource.is_pdf:
            return "The URL points to a PDF. Ask the user to upload it as a document so it can be indexed."
        if resource.is_html:
            text = html_to_text(resource.body, max_chars=max_chars)
            title = f"{resource.title}\n\n" if resource.title else ""
            return f"{title}{text}" if text else "The page has no readable text."
        return resource.body.decode("utf-8", errors="replace")[:max_chars]
