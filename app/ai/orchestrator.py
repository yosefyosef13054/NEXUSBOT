from __future__ import annotations

import asyncio
import contextlib
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
from app.ai.llm import chat_model_name
from app.ai.memory.long_term import load_session_summary
from app.ai.memory.short_term import load_recent_history
from app.ai.memory.summarizer import maybe_compact_history
from app.ai.prompts.templates import build_chat_prompt
from app.ai.rag.retriever import RetrievedChunk, format_context, retrieve_chunks
from app.ai.tools.context import ToolContext
from app.config import settings
from app.core.exceptions import AIProviderError
from app.core.logger import get_logger
from app.core.metrics import ACTIVE_GENERATIONS, INTERRUPTED_GENERATIONS, LLM_LATENCY, LLM_REQUESTS
from app.models.chat import ChatSession
from app.utils.tokens import count_message_tokens, count_tokens

logger = get_logger(__name__)

# Queue poll interval while a generation streams; bounds how fast a stop request or the
# generation deadline is noticed when the model is silent.
_POLL_SECONDS = 0.25


@dataclass
class GenerationProgress:
    """Mutable record of a generation, shared with the caller.

    Updated as events flow so that when the client disconnects mid-stream the service
    layer can still persist whatever was produced (text, citations, tool calls, usage).
    """

    text_parts: list[str] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    embedding_tokens: int = 0
    model: str = ""
    started_at: float = field(default_factory=time.time)
    interrupted: bool = False
    interrupt_reason: str | None = None  # stop | disconnect | timeout
    errored: bool = False
    finished: bool = False

    @property
    def text(self) -> str:
        return "".join(self.text_parts).strip()

    @property
    def latency_ms(self) -> int:
        return int((time.time() - self.started_at) * 1000)


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
    request_id: str | None = None
    # Cooperative cancellation (e.g. a WebSocket `stop` frame). Set it and the stream
    # finishes with `interrupted=True` carrying the partial text.
    stop_event: asyncio.Event | None = None
    progress: GenerationProgress = field(default_factory=GenerationProgress)

    @property
    def tool_context(self) -> ToolContext:
        return ToolContext(user_id=self.user_id, session_id=self.session.id, request_id=self.request_id)


@dataclass
class GenerationResult:
    text: str
    citations: list[RetrievedChunk] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    embedding_tokens: int = 0
    latency_ms: int = 0
    model: str = ""
    interrupted: bool = False


def citations_payload(citations: list[RetrievedChunk]) -> list[dict[str, Any]]:
    return [
        {
            "document_id": str(c.document_id),
            "document_name": c.document_name,
            "chunk_index": c.chunk_index,
            "score": round(c.score, 4),
            "excerpt": c.content[:300],
        }
        for c in citations
    ]


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
        retrieval = await retrieve_chunks(
            db, user_id=req.user_id, query=req.user_input, session_id=req.session.id
        )
        citations = retrieval.chunks
        req.progress.embedding_tokens = retrieval.query_tokens
        inputs["context"] = format_context(citations)

    req.progress.citations = citations_payload(citations)
    req.progress.model = chat_model_name()
    return inputs, citations


def _build_runnable(req: GenerationRequest):
    if req.use_tools:
        return build_tool_agent(
            ctx=req.tool_context,
            use_rag=req.use_rag,
            tool_names=req.tool_names,
            system_prompt=req.session.system_prompt,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            streaming=req.streaming,
        )
    builder = build_rag_chain if req.use_rag else build_chat_chain
    return builder(
        system_prompt=req.session.system_prompt,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        streaming=req.streaming,
    )


def _run_config(req: GenerationRequest, collector: StreamCollector) -> RunnableConfig:
    return {
        "callbacks": [collector],
        "run_name": "nexusbot.generate",
        "metadata": {
            "session_id": str(req.session.id),
            "user_id": str(req.user_id),
            "request_id": req.request_id or "",
            "use_rag": req.use_rag,
            "use_tools": req.use_tools,
        },
    }


def estimate_prompt_tokens(req: GenerationRequest, inputs: dict[str, Any]) -> int:
    """Local estimate of the full prompt (system + summary + context + history + input),
    used when the provider does not report usage."""
    try:
        prompt = build_chat_prompt(
            system_prompt=req.session.system_prompt, use_rag=req.use_rag, use_tools=req.use_tools
        )
        messages = prompt.format_messages(**{**inputs, "agent_scratchpad": []})
        return count_message_tokens(
            m.content if isinstance(m.content, str) else str(m.content) for m in messages
        )
    except Exception:  # noqa: BLE001
        return count_tokens(req.user_input)


def _cancel(task: asyncio.Task | None) -> None:
    if task is not None and not task.done():
        task.cancel()


async def generate(db: AsyncSession, req: GenerationRequest) -> GenerationResult:
    """One-shot, non-streaming generation. Used by the blocking endpoint, regenerate and tests."""
    p = req.progress
    inputs, citations = await _prepare_inputs(db, req)
    runnable = _build_runnable(req)
    collector = StreamCollector(emit_events=False)
    config = _run_config(req, collector)
    operation = "agent" if req.use_tools else "chat"

    ACTIVE_GENERATIONS.inc()
    try:
        output = await asyncio.wait_for(
            runnable.ainvoke(inputs, config=config), timeout=settings.GENERATION_TIMEOUT_SECONDS
        )
    except TimeoutError:
        LLM_REQUESTS.labels(p.model, operation, "timeout").inc()
        raise AIProviderError(
            "Generation timed out", details={"timeout_seconds": settings.GENERATION_TIMEOUT_SECONDS}
        )
    except Exception as e:  # noqa: BLE001
        LLM_REQUESTS.labels(p.model, operation, "error").inc()
        logger.exception("ai_generate_failed", error=str(e))
        raise AIProviderError("Upstream AI provider failed", details={"reason": str(e)[:300]})
    finally:
        ACTIVE_GENERATIONS.dec()

    if req.use_tools:
        text = str(output.get("output", "")).strip()
        tool_calls = [
            {"tool": s.tool, "input": s.tool_input, "output": str(o)[:5000]}
            for s, o in output.get("intermediate_steps", [])
        ]
    else:
        text = str(output).strip()
        tool_calls = []

    p.text_parts = [text]
    p.tool_calls = tool_calls
    p.prompt_tokens = collector.prompt_tokens or estimate_prompt_tokens(req, inputs)
    p.completion_tokens = collector.completion_tokens or count_tokens(text)
    p.finished = True

    LLM_REQUESTS.labels(p.model, operation, "ok").inc()
    LLM_LATENCY.labels(p.model, operation).observe(p.latency_ms / 1000)

    return GenerationResult(
        text=text,
        citations=citations,
        tool_calls=tool_calls,
        prompt_tokens=p.prompt_tokens,
        completion_tokens=p.completion_tokens,
        embedding_tokens=p.embedding_tokens,
        latency_ms=p.latency_ms,
        model=p.model,
    )


async def stream(db: AsyncSession, req: GenerationRequest) -> AsyncIterator[dict]:
    """Streaming generation.

    Yields structured events: `citations`, `token` (many), `tool_call`, `usage`, `error`,
    and always finalizes with `done` carrying the full text, usage and latency.

    Cancellation:
    - `req.stop_event` set → the model call is cancelled and `done.interrupted` is true.
    - consumer closed / task cancelled (client disconnect) → the model call is cancelled;
      `req.progress` keeps the partial output so the caller can persist it.
    - `GENERATION_TIMEOUT_SECONDS` exceeded → treated like a stop with reason `timeout`.
    """
    p = req.progress
    inputs, citations = await _prepare_inputs(db, req)
    runnable = _build_runnable(req)
    collector = StreamCollector()
    config = _run_config(req, collector)
    operation = "agent" if req.use_tools else "chat"

    if citations:
        yield {"type": "citations", "data": p.citations}

    async def _drive() -> None:
        try:
            if req.use_tools:
                result = await runnable.ainvoke(inputs, config=config)
                final = str(result.get("output", "")).strip()
                if final and not collector.saw_token:
                    # Some agent runs produce the final answer without streaming it.
                    await collector.queue.put({"type": "token", "data": final})
            else:
                # Tokens reach the consumer through the callback; don't also collect chunks
                # here or the text would be counted twice.
                async for _ in runnable.astream(inputs, config=config):
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("ai_stream_failed")
            await collector.queue.put(
                {"type": "error", "data": {"code": "ai_provider_error", "message": str(e)[:300]}}
            )
        finally:
            await collector.finalize()

    driver = asyncio.create_task(_drive(), name=f"generation:{req.session.id}")
    deadline = time.monotonic() + settings.GENERATION_TIMEOUT_SECONDS
    ACTIVE_GENERATIONS.inc()

    def _stop_requested() -> bool:
        if req.stop_event is not None and req.stop_event.is_set():
            p.interrupted, p.interrupt_reason = True, "stop"
            return True
        if time.monotonic() > deadline:
            p.interrupted, p.interrupt_reason = True, "timeout"
            return True
        return False

    try:
        while True:
            try:
                evt = await asyncio.wait_for(collector.queue.get(), timeout=_POLL_SECONDS)
            except TimeoutError:
                if _stop_requested():
                    break
                continue
            if evt is None:
                break
            kind = evt["type"]
            if kind == "token":
                p.text_parts.append(evt["data"])
            elif kind == "tool_call" and evt["data"].get("phase") == "end":
                p.tool_calls.append(
                    {
                        "tool": evt["data"].get("tool"),
                        "input": evt["data"].get("input"),
                        "output": evt["data"].get("output"),
                        "latency_ms": evt["data"].get("latency_ms", 0),
                    }
                )
            elif kind == "usage":
                p.prompt_tokens = int(evt["data"].get("prompt_tokens") or p.prompt_tokens)
                p.completion_tokens = int(evt["data"].get("completion_tokens") or p.completion_tokens)
            elif kind == "error":
                p.errored = True
            yield evt
            if _stop_requested():
                break
    except (asyncio.CancelledError, GeneratorExit):
        # Consumer went away (client disconnected). Stop paying for tokens nobody reads;
        # `req.progress` still holds the partial output for the service layer to persist.
        p.interrupted = True
        p.interrupt_reason = p.interrupt_reason or "disconnect"
        _cancel(driver)
        INTERRUPTED_GENERATIONS.labels(p.interrupt_reason).inc()
        ACTIVE_GENERATIONS.dec()
        raise

    ACTIVE_GENERATIONS.dec()
    _cancel(driver)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await driver

    if p.interrupted:
        INTERRUPTED_GENERATIONS.labels(p.interrupt_reason or "unknown").inc()
    p.prompt_tokens = p.prompt_tokens or estimate_prompt_tokens(req, inputs)
    p.completion_tokens = p.completion_tokens or count_tokens(p.text)
    p.finished = True

    status = "error" if p.errored else ("interrupted" if p.interrupted else "ok")
    LLM_REQUESTS.labels(p.model, operation, status).inc()
    LLM_LATENCY.labels(p.model, operation).observe(p.latency_ms / 1000)

    yield {
        "type": "done",
        "data": {
            "text": p.text,
            "latency_ms": p.latency_ms,
            "model": p.model,
            "prompt_tokens": p.prompt_tokens,
            "completion_tokens": p.completion_tokens,
            "interrupted": p.interrupted,
            "interrupt_reason": p.interrupt_reason,
        },
    }


async def maybe_summarize(db: AsyncSession, *, session_id: UUID) -> None:
    """Compact long histories into the rolling summary. Runs off the hot path."""
    try:
        await maybe_compact_history(db, session_id=session_id)
    except Exception:  # noqa: BLE001
        logger.warning("summarize_failed", session_id=str(session_id))
