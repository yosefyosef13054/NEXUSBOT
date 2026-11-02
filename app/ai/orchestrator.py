from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar
from uuid import UUID, uuid4

from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agents.checkpointer import get_checkpointer
from app.ai.agents.runner import AgentOutcome, AgentRunner
from app.ai.artifacts import ArtifactSink
from app.ai.attachments import build_user_message
from app.ai.callbacks import StreamCollector
from app.ai.chains.chat_chain import build_chat_chain
from app.ai.chains.rag_chain import build_rag_chain
from app.ai.llm import build_chat_llm
from app.ai.memory.long_term import load_session_summary
from app.ai.memory.short_term import load_recent_history
from app.ai.memory.summarizer import maybe_compact_history
from app.ai.prompts.templates import build_chat_prompt
from app.ai.providers.catalog import ModelSpec
from app.ai.providers.registry import get_registry
from app.ai.rag.query import rewrite_query
from app.ai.rag.retriever import RetrievedChunk, format_context, retrieve_chunks
from app.ai.tools.context import ToolContext
from app.ai.tools.registry import get_tools, requires_approval
from app.config import settings
from app.core.exceptions import AIProviderError, ValidationError
from app.core.logger import get_logger
from app.core.metrics import ACTIVE_GENERATIONS, APPROVALS, INTERRUPTED_GENERATIONS, LLM_LATENCY, LLM_REQUESTS
from app.models.chat import ChatSession
from app.services import attachment_service
from app.services.attachment_service import AttachmentInput
from app.services.memory_service import format_memories, recall
from app.utils.tokens import count_message_tokens, count_tokens

logger = get_logger(__name__)

T = TypeVar("T")

# Queue poll interval while a generation streams; bounds how fast a stop request or the
# generation deadline is noticed when the model is silent.
_POLL_SECONDS = 0.25

STATUS_OK = "ok"
STATUS_INTERRUPTED = "interrupted"
STATUS_ERROR = "error"
STATUS_AWAITING_APPROVAL = "awaiting_approval"


@dataclass
class GenerationProgress:
    """Mutable record of a generation, shared with the caller.

    Updated as events flow so that when the client disconnects mid-stream the service
    layer can still persist whatever was produced (text, citations, tool calls, usage).
    """

    text_parts: list[str] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # live callback view
    tool_runs: list[dict[str, Any]] = field(default_factory=list)  # authoritative, from the agent state
    prompt_tokens: int = 0
    completion_tokens: int = 0
    embedding_tokens: int = 0
    model: str = ""
    provider: str = ""
    started_at: float = field(default_factory=time.time)
    interrupted: bool = False
    interrupt_reason: str | None = None  # stop | disconnect | timeout
    errored: bool = False
    finished: bool = False
    approval: dict[str, Any] | None = None
    memories_used: list[str] = field(default_factory=list)
    # Files produced by tools during this turn (attachment summaries).
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def add_artifact(self, record: dict[str, Any]) -> None:
        if record.get("id") not in {a.get("id") for a in self.artifacts}:
            self.artifacts.append(record)

    @property
    def text(self) -> str:
        return "".join(self.text_parts).strip()

    @property
    def latency_ms(self) -> int:
        return int((time.time() - self.started_at) * 1000)

    @property
    def status(self) -> str:
        if self.errored:
            return STATUS_ERROR
        if self.interrupted:
            return STATUS_INTERRUPTED
        if self.approval:
            return STATUS_AWAITING_APPROVAL
        return STATUS_OK


@dataclass
class GenerationRequest:
    user_id: UUID
    session: ChatSession
    user_input: str
    use_rag: bool = True
    use_tools: bool = False
    tool_names: list[str] | None = None
    # Model resolution: request → session → user default → server default.
    model: str | None = None
    user_default_model: str | None = None
    collection_ids: list[UUID] | None = None
    org_id: UUID | None = None
    # Inject the user's long-term memories into the system prompt.
    use_memory: bool = False
    # Assistant / organization defaults (lower priority than request and session).
    system_prompt: str | None = None
    assistant_model: str | None = None
    org_default_model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    streaming: bool = True
    request_id: str | None = None
    # Cooperative cancellation (e.g. a WebSocket `stop` frame). Set it and the stream
    # finishes with `interrupted=True` carrying the partial text.
    stop_event: asyncio.Event | None = None
    progress: GenerationProgress = field(default_factory=GenerationProgress)
    # LangGraph thread for agent turns (persisted with pending approvals).
    thread_id: str = field(default_factory=lambda: uuid4().hex)
    spec: ModelSpec | None = None
    # Images / files / transcripts attached to the user message (built by the service layer).
    attachments: list[AttachmentInput] = field(default_factory=list)
    # Tools served by MCP servers for this org, already wrapped as LangChain tools.
    extra_tools: list[BaseTool] = field(default_factory=list)
    artifact_sink: ArtifactSink | None = None

    @property
    def tool_context(self) -> ToolContext:
        return ToolContext(
            user_id=self.user_id,
            session_id=self.session.id,
            request_id=self.request_id,
            org_id=self.org_id,
            artifacts=self.artifact_sink,
        )

    @property
    def effective_system_prompt(self) -> str | None:
        return self.session.system_prompt or self.system_prompt


@dataclass
class GenerationResult:
    text: str
    citations: list[RetrievedChunk] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_runs: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    embedding_tokens: int = 0
    latency_ms: int = 0
    model: str = ""
    provider: str = ""
    interrupted: bool = False
    approval: dict[str, Any] | None = None
    thread_id: str = ""
    status: str = STATUS_OK


def citations_payload(citations: list[RetrievedChunk]) -> list[dict[str, Any]]:
    return [c.to_citation() for c in citations]


def resolve_spec_for(req: GenerationRequest) -> ModelSpec:
    spec = req.spec or get_registry().resolve(
        req.model, req.session.model, req.assistant_model, req.user_default_model, req.org_default_model
    )
    _use_spec(req, spec)
    return spec


def _use_spec(req: GenerationRequest, spec: ModelSpec) -> None:
    req.spec = spec
    req.progress.model = spec.id
    req.progress.provider = spec.provider


async def _prepare_inputs(
    db: AsyncSession, req: GenerationRequest
) -> tuple[dict[str, Any], list[RetrievedChunk]]:
    history = await load_recent_history(db, session_id=req.session.id)
    summary = await load_session_summary(db, session_id=req.session.id)

    if (
        attachment_service.has_images(req.attachments)
        and req.spec is not None
        and not req.spec.supports_vision
    ):
        raise ValidationError(
            f"Model '{req.spec.id}' cannot read images",
            details={"vision_models": [m.id for m in get_registry().available() if m.supports_vision]},
        )
    citations: list[RetrievedChunk] = []
    inputs: dict[str, Any] = {
        "input": req.user_input,
        "input_messages": [build_user_message(req.user_input, req.attachments)],
        "history": history,
        "summary": summary or "(none)",
    }

    if req.use_memory and req.user_input.strip():
        memories = await recall(db, user_id=req.user_id, org_id=req.org_id, query=req.user_input)
        if memories:
            inputs["memory_messages"] = [SystemMessage(content=format_memories(memories))]
            req.progress.memories_used = [str(m.id) for m in memories]

    if req.use_rag:
        search_query = req.user_input
        if settings.RAG_QUERY_REWRITE and history:
            search_query = await rewrite_query(history, req.user_input, spec=req.spec)
        retrieval = await retrieve_chunks(
            db,
            user_id=req.user_id,
            query=search_query,
            session_id=req.session.id,
            collection_ids=req.collection_ids,
            spec=req.spec,
            org_id=req.org_id,
        )
        citations = retrieval.chunks
        req.progress.embedding_tokens = retrieval.query_tokens
        inputs["context"] = format_context(citations)

    req.progress.citations = citations_payload(citations)
    return inputs, citations


def _build_chain(req: GenerationRequest):
    builder = build_rag_chain if req.use_rag else build_chat_chain
    return builder(
        system_prompt=req.effective_system_prompt,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        streaming=req.streaming,
        spec=req.spec,
    )


def _run_config(req: GenerationRequest, collector: StreamCollector) -> RunnableConfig:
    return {
        "callbacks": [collector],
        "run_name": "nexusbot.generate",
        "metadata": {
            "session_id": str(req.session.id),
            "user_id": str(req.user_id),
            "request_id": req.request_id or "",
            "model": req.progress.model,
            "use_rag": req.use_rag,
            "use_tools": req.use_tools,
        },
    }


def initial_agent_messages(req: GenerationRequest, inputs: dict[str, Any]) -> list[BaseMessage]:
    prompt = build_chat_prompt(system_prompt=req.effective_system_prompt, use_rag=req.use_rag, use_tools=True)
    return prompt.format_messages(**{**inputs, "agent_scratchpad": []})


def _ensure_sink(req: GenerationRequest, collector: StreamCollector) -> None:
    if req.artifact_sink is None:
        req.artifact_sink = ArtifactSink(
            user_id=req.user_id,
            org_id=req.org_id,
            session_id=req.session.id,
            queue=collector.queue if collector.emit_events else None,
        )


async def _agent_runner(req: GenerationRequest, collector: StreamCollector) -> AgentRunner:
    _ensure_sink(req, collector)
    tools = [*get_tools(req.tool_names, req.tool_context), *req.extra_tools]
    llm = build_chat_llm(
        spec=req.spec, temperature=req.temperature, max_tokens=req.max_tokens, streaming=req.streaming
    )
    return AgentRunner(
        llm=llm,
        tools=tools,
        approval_required={
            t.name
            for t in tools
            if requires_approval(t.name) or bool(getattr(t, "nexusbot_requires_approval", False))
        },
        thread_id=req.thread_id,
        checkpointer=await get_checkpointer(),
        callbacks=[collector],
        metadata=_run_config(req, collector)["metadata"],
        max_steps=settings.AGENT_MAX_STEPS,
        tool_timeout_seconds=settings.AGENT_MAX_TOOL_SECONDS,
    )


def estimate_prompt_tokens(req: GenerationRequest, inputs: dict[str, Any]) -> int:
    """Local estimate of the full prompt (system + summary + context + history + input),
    used when the provider does not report usage."""
    try:
        prompt = build_chat_prompt(
            system_prompt=req.effective_system_prompt, use_rag=req.use_rag, use_tools=req.use_tools
        )
        messages = prompt.format_messages(**{**inputs, "agent_scratchpad": []})
        return count_message_tokens(
            (_estimable_text(m.content) for m in messages), model=req.progress.model or None
        ) + IMAGE_TOKEN_ESTIMATE * sum(_image_parts(m.content) for m in messages)
    except Exception:  # noqa: BLE001
        return count_tokens(req.user_input)


# Rough per-image prompt cost when the provider reports no usage (OpenAI "high detail" tile).
IMAGE_TOKEN_ESTIMATE = 765


def _estimable_text(content: Any) -> str:
    """Text of a message without base64 image payloads (they are not tokens)."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict):
            if part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        else:
            parts.append(str(part))
    return "\n".join(parts)


def _image_parts(content: Any) -> int:
    if isinstance(content, str):
        return 0
    return sum(1 for part in content if isinstance(part, dict) and part.get("type") == "image_url")


def _cancel(task: asyncio.Task | None) -> None:
    if task is not None and not task.done():
        task.cancel()


async def _with_fallback(
    req: GenerationRequest,
    attempt: Callable[[], Awaitable[T]],
    *,
    operation: str,
    collector: StreamCollector | None = None,
) -> T:
    """Run `attempt` on the resolved model, retrying once per configured fallback model.

    Fallbacks only kick in before anything was streamed to the client — a reply cannot be
    restarted on another model once tokens are out.
    """
    assert req.spec is not None
    chain = [req.spec, *get_registry().fallbacks_for(req.spec.id)]
    last_exc: BaseException | None = None
    for index, spec in enumerate(chain):
        if index > 0:
            logger.warning("model_fallback", from_model=chain[index - 1].id, to_model=spec.id)
            req.thread_id = uuid4().hex
        _use_spec(req, spec)
        try:
            return await attempt()
        except (TimeoutError, asyncio.CancelledError):
            raise
        except Exception as e:  # noqa: BLE001
            last_exc = e
            LLM_REQUESTS.labels(spec.id, operation, "error").inc()
            if collector is not None and collector.saw_token:
                raise
            logger.warning("model_call_failed", model=spec.id, error=str(e)[:300])
    assert last_exc is not None
    raise last_exc


def _absorb_outcome(req: GenerationRequest, collector: StreamCollector | None, outcome: AgentOutcome) -> str:
    p = req.progress
    p.tool_runs = [asdict(r) for r in outcome.tool_runs]
    if req.artifact_sink is not None:
        for record in req.artifact_sink.records:
            p.add_artifact(record)
    if outcome.approval:
        p.approval = {"thread_id": req.thread_id, **outcome.approval}
        APPROVALS.labels("requested").inc()
        return ""
    return outcome.final_text


async def generate(db: AsyncSession, req: GenerationRequest) -> GenerationResult:
    """One-shot, non-streaming generation. Used by the blocking endpoint, regenerate and tests."""
    p = req.progress
    resolve_spec_for(req)
    inputs, citations = await _prepare_inputs(db, req)
    collector = StreamCollector(emit_events=False)
    operation = "agent" if req.use_tools else "chat"

    async def attempt() -> Any:
        if req.use_tools:
            runner = await _agent_runner(req, collector)
            return await asyncio.wait_for(
                runner.start(initial_agent_messages(req, inputs)), timeout=settings.GENERATION_TIMEOUT_SECONDS
            )
        chain = _build_chain(req)
        return await asyncio.wait_for(
            chain.ainvoke(inputs, config=_run_config(req, collector)),
            timeout=settings.GENERATION_TIMEOUT_SECONDS,
        )

    ACTIVE_GENERATIONS.inc()
    try:
        output = await _with_fallback(req, attempt, operation=operation)
    except TimeoutError:
        LLM_REQUESTS.labels(p.model, operation, "timeout").inc()
        raise AIProviderError(
            "Generation timed out", details={"timeout_seconds": settings.GENERATION_TIMEOUT_SECONDS}
        )
    except AIProviderError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("ai_generate_failed", error=str(e))
        raise AIProviderError("Upstream AI provider failed", details={"reason": str(e)[:300]})
    finally:
        ACTIVE_GENERATIONS.dec()

    if req.use_tools:
        text = _absorb_outcome(req, None, output)
    else:
        text = str(output).strip()

    p.text_parts = [text]
    p.prompt_tokens = collector.prompt_tokens or estimate_prompt_tokens(req, inputs)
    p.completion_tokens = collector.completion_tokens or count_tokens(text)
    p.finished = True

    LLM_REQUESTS.labels(p.model, operation, "ok").inc()
    LLM_LATENCY.labels(p.model, operation).observe(p.latency_ms / 1000)

    return GenerationResult(
        text=text,
        citations=citations,
        tool_calls=p.tool_runs,
        tool_runs=p.tool_runs,
        prompt_tokens=p.prompt_tokens,
        completion_tokens=p.completion_tokens,
        embedding_tokens=p.embedding_tokens,
        latency_ms=p.latency_ms,
        model=p.model,
        provider=p.provider,
        approval=p.approval,
        thread_id=req.thread_id,
        status=p.status,
    )


async def _consume(
    req: GenerationRequest,
    collector: StreamCollector,
    run: Callable[[], Awaitable[None]],
    *,
    operation: str,
    inputs: dict[str, Any] | None,
) -> AsyncIterator[dict]:
    """Shared streaming loop: republish collector events, honor stop/timeout/disconnect,
    then emit `done`."""
    p = req.progress

    async def _drive() -> None:
        try:
            await run()
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
            elif kind == "artifact":
                p.add_artifact(evt["data"])
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
    if inputs is not None:
        p.prompt_tokens = p.prompt_tokens or estimate_prompt_tokens(req, inputs)
    p.completion_tokens = p.completion_tokens or count_tokens(p.text)
    p.finished = True

    status = p.status
    LLM_REQUESTS.labels(p.model, operation, status).inc()
    LLM_LATENCY.labels(p.model, operation).observe(p.latency_ms / 1000)

    yield {
        "type": "done",
        "data": {
            "text": p.text,
            "latency_ms": p.latency_ms,
            "model": p.model,
            "provider": p.provider,
            "prompt_tokens": p.prompt_tokens,
            "completion_tokens": p.completion_tokens,
            "interrupted": p.interrupted,
            "interrupt_reason": p.interrupt_reason,
            "status": status,
            "thread_id": req.thread_id,
        },
    }


async def stream(db: AsyncSession, req: GenerationRequest) -> AsyncIterator[dict]:
    """Streaming generation.

    Yields structured events: `citations`, `token` (many), `tool_call`, `usage`,
    `approval_required`, `error`, and always finalizes with `done` carrying the full text,
    usage, latency and status.

    Cancellation:
    - `req.stop_event` set → the model call is cancelled and `done.interrupted` is true.
    - consumer closed / task cancelled (client disconnect) → the model call is cancelled;
      `req.progress` keeps the partial output so the caller can persist it.
    - `GENERATION_TIMEOUT_SECONDS` exceeded → treated like a stop with reason `timeout`.
    """
    resolve_spec_for(req)
    inputs, citations = await _prepare_inputs(db, req)
    collector = StreamCollector()
    operation = "agent" if req.use_tools else "chat"

    if citations:
        yield {"type": "citations", "data": req.progress.citations}

    async def run() -> None:
        if req.use_tools:

            async def attempt_agent() -> AgentOutcome:
                runner = await _agent_runner(req, collector)
                return await runner.start(initial_agent_messages(req, inputs))

            outcome = await _with_fallback(req, attempt_agent, operation=operation, collector=collector)
            final = _absorb_outcome(req, collector, outcome)
            if req.progress.approval:
                await collector.queue.put({"type": "approval_required", "data": req.progress.approval})
            elif final and not collector.saw_token:
                # Some providers deliver the final answer without streaming it.
                await collector.queue.put({"type": "token", "data": final})
        else:

            async def attempt_chain() -> None:
                # Tokens reach the consumer through the callback; don't also collect chunks
                # here or the text would be counted twice.
                async for _ in _build_chain(req).astream(inputs, config=_run_config(req, collector)):
                    pass

            await _with_fallback(req, attempt_chain, operation=operation, collector=collector)

    async for evt in _consume(req, collector, run, operation=operation, inputs=inputs):
        yield evt


async def resume_stream(
    db: AsyncSession, req: GenerationRequest, decision: dict[str, Any]
) -> AsyncIterator[dict]:
    """Continue an agent turn paused for approval. Retrieval is not re-run: the context is
    already part of the checkpointed conversation."""
    resolve_spec_for(req)
    collector = StreamCollector()

    async def run() -> None:
        runner = await _agent_runner(req, collector)
        outcome = await runner.resume(decision)
        final = _absorb_outcome(req, collector, outcome)
        if req.progress.approval:
            await collector.queue.put({"type": "approval_required", "data": req.progress.approval})
        elif final and not collector.saw_token:
            await collector.queue.put({"type": "token", "data": final})

    async for evt in _consume(req, collector, run, operation="agent", inputs=None):
        yield evt


async def maybe_summarize(db: AsyncSession, *, session_id: UUID) -> None:
    """Compact long histories into the rolling summary. Runs off the hot path."""
    try:
        await maybe_compact_history(db, session_id=session_id)
    except Exception:  # noqa: BLE001
        logger.warning("summarize_failed", session_id=str(session_id))
