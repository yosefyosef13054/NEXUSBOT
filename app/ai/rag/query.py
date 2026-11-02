"""Query understanding for retrieval: standalone rewriting and multi-query expansion."""

from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from app.ai.llm import build_chat_llm
from app.ai.providers.catalog import ModelSpec
from app.core.logger import get_logger

logger = get_logger(__name__)

REWRITE_SYSTEM = (
    "You rewrite the user's latest message into a single standalone search query for a "
    "document index. Resolve pronouns and references using the conversation. Keep the "
    "user's language. Output only the query, no quotes, no explanation."
)

EXPAND_SYSTEM = (
    "Generate {n} alternative phrasings of the search query below, each capturing a "
    "different angle or vocabulary a document might use. One per line, no numbering, no "
    "explanations."
)


def _text(message: BaseMessage) -> str:
    c = message.content
    return (
        c if isinstance(c, str) else " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    )


def _transcript(history: list[BaseMessage], limit: int = 6) -> str:
    lines = []
    for m in history[-limit:]:
        role = "User" if m.type == "human" else "Assistant"
        lines.append(f"{role}: {_text(m)[:500]}")
    return "\n".join(lines)


async def rewrite_query(history: list[BaseMessage], query: str, *, spec: ModelSpec | None = None) -> str:
    """Turn a follow-up ("what about the second one?") into a self-contained query."""
    if not history:
        return query
    try:
        llm = build_chat_llm(spec=spec, streaming=False, temperature=0, max_tokens=120)
        out = await llm.ainvoke(
            [
                SystemMessage(content=REWRITE_SYSTEM),
                HumanMessage(content=f"Conversation:\n{_transcript(history)}\n\nLatest message:\n{query}"),
            ]
        )
        text = _text(out).strip().strip('"')
        return text.splitlines()[0][:500] if text else query
    except Exception as e:  # noqa: BLE001
        logger.warning("query_rewrite_failed", error=str(e)[:200])
        return query


async def expand_queries(query: str, *, n: int = 3, spec: ModelSpec | None = None) -> list[str]:
    """Return `query` plus up to `n` alternative phrasings."""
    try:
        llm = build_chat_llm(spec=spec, streaming=False, temperature=0.7, max_tokens=200)
        out = await llm.ainvoke(
            [SystemMessage(content=EXPAND_SYSTEM.format(n=n)), HumanMessage(content=query)]
        )
        variants = [line.strip(" -•*\t") for line in _text(out).splitlines()]
        variants = [v for v in variants if v and v.lower() != query.lower()]
        return [query, *variants[:n]]
    except Exception as e:  # noqa: BLE001
        logger.warning("query_expand_failed", error=str(e)[:200])
        return [query]
