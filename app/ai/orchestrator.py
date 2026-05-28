from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agents.tool_agent import build_tool_agent
from app.ai.callbacks import StreamCollector
from app.ai.chains.chat_chain import build_chat_chain
from app.ai.chains.rag_chain import build_rag_chain
from app.ai.memory.long_term import load_session_summary
from app.ai.memory.short_term import load_recent_history
from app.ai.memory.summarizer import maybe_compact_history
from app.ai.rag.retriever import RetrievedChunk, format_context, retrieve_chunks
from app.config import settings
from app.core.exceptions import AIProviderError
from app.core.logger import get_logger
from app.models.chat import ChatSession

logger = get_logger(__name__)


@dataclass
class GenerationRequest:
    user_id: UUID
    session: ChatSession
    user_input: str
    use_rag: bool = True
    use_tools: bool = False
    tool_names: list[str] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    streaming: bool = True


@dataclass
class GenerationResult:
    text: str
    citations: list[RetrievedChunk] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    model: str = ""


async def _prepare_inputs(
    db: AsyncSession, req: GenerationRequest
) -> tuple[dict[str, Any], list[RetrievedChunk]]:
    history = await load_recent_history(db, session_id=req.session.id)
    summary = await load_session_summary(db, session_id=req.session.id)

    citations: list[RetrievedChunk] = []
    inputs: dict[str, Any] = {
        "input": req.user_input,
        "history": history,
        "summary": summary or "(none)",
    }

    if req.use_rag:
        citations = await retrieve_chunks(
            db, user_id=req.user_id, query=req.user_input, session_id=req.session.id
        )
        inputs["context"] = format_context(citations)

    return inputs, citations


def _build_runnable(req: GenerationRequest):
    common = dict(
        system_prompt=req.session.system_prompt,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        streaming=req.streaming,
    )
    if req.use_tools:
        return build_tool_agent(use_rag=req.use_rag, tool_names=req.tool_names, **common)
    if req.use_rag:
        return build_rag_chain(**common)
    return build_chat_chain(**common)


async def generate(db: AsyncSession, req: GenerationRequest) -> GenerationResult:
    """One-shot, non-streaming generation. Used by the regenerate endpoint and tests."""
    start = time.time()
    inputs, citations = await _prepare_inputs(db, req)
    runnable = _build_runnable(req)

    try:
        if req.use_tools:
            output = await runnable.ainvoke(inputs)
            text = str(output.get("output", "")).strip()
            tool_calls = [
                {"tool": s.tool, "input": s.tool_input, "output": str(o)[:5000]}
                for s, o in output.get("intermediate_steps", [])
            ]
        else:
            output = await runnable.ainvoke(inputs)
            text = str(output).strip()
            tool_calls = []
    except Exception as e:  # noqa: BLE001
        logger.exception("ai_generate_failed", error=str(e))
        raise AIProviderError("Upstream AI provider failed", details={"reason": str(e)[:300]})

    return GenerationResult(
        text=text,
        citations=citations,
        tool_calls=tool_calls,
        latency_ms=int((time.time() - start) * 1000),
        model=settings.OPENAI_CHAT_MODEL,
    )


async def stream(db: AsyncSession, req: GenerationRequest) -> AsyncIterator[dict]:
    """Streaming generation.

    Yields structured events: `citations`, `token` (many), `tool_call`, `usage`, `done`.
    Always finalizes with `done` carrying the full text + latency.
    """
    start = time.time()
    inputs, citations = await _prepare_inputs(db, req)
    runnable = _build_runnable(req)

    collector = StreamCollector()
    config: RunnableConfig = {"callbacks": [collector]}

    if citations:
        yield {
            "type": "citations",
            "data": [
                {
                    "document_id": str(c.document_id),
                    "document_name": c.document_name,
                    "chunk_index": c.chunk_index,
                    "score": round(c.score, 4),
                    "excerpt": c.content[:300],
                }
                for c in citations
            ],
        }

    accumulator: list[str] = []

    async def _drive():
        try:
            if req.use_tools:
                result = await runnable.ainvoke(inputs, config=config)
                final = str(result.get("output", "")).strip()
                if final and not collector.saw_token:
                    # Agents don't always stream their final answer; emit it as a single token
                    # frame. The consumer loop below appends it to `accumulator`.
                    await collector.queue.put({"type": "token", "data": final})
            else:
                # Drive the stream to completion. Tokens reach the client (and `accumulator`)
                # via the streaming callback -> token events in the consumer loop below, so we
                # must NOT also accumulate the astream chunks here (that double-counts the text).
                async for _ in runnable.astream(inputs, config=config):
                    pass
        finally:
            await collector.finalize()

    driver = asyncio.create_task(_drive())

    try:
        async for evt in collector.aiter():
            if evt["type"] == "token":
                accumulator.append(evt["data"])
            yield evt
    except Exception as e:  # noqa: BLE001
        logger.exception("ai_stream_failed")
        yield {"type": "error", "data": {"message": str(e)[:300]}}
        driver.cancel()
        return

    await driver

    full_text = "".join(accumulator).strip()
    yield {
        "type": "done",
        "data": {
            "text": full_text,
            "latency_ms": int((time.time() - start) * 1000),
            "model": settings.OPENAI_CHAT_MODEL,
            "prompt_tokens": collector.prompt_tokens,
            "completion_tokens": collector.completion_tokens,
        },
    }


async def maybe_summarize(db: AsyncSession, *, session_id: UUID) -> None:
    """Hook called after a successful turn so long histories stay compact."""
    try:
        await maybe_compact_history(db, session_id=session_id)
    except Exception:  # noqa: BLE001
        logger.warning("summarize_failed", session_id=str(session_id))
