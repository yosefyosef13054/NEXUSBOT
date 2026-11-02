from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from uuid import UUID

from langchain_core.callbacks.base import AsyncCallbackHandler

from app.core.metrics import TOOL_CALLS


def extract_usage(response: Any) -> tuple[int, int]:
    """Pull (prompt_tokens, completion_tokens) out of an LLMResult, whatever the provider.

    Checks, in order: the message's `usage_metadata` (LangChain's normalized form, set by
    OpenAI when `stream_usage=True`, by Anthropic, Google, …), then provider-specific
    `response_metadata` keys, then `llm_output`.
    """
    try:
        for gen in getattr(response, "generations", []) or []:
            for g in gen:
                msg = getattr(g, "message", None)
                um = getattr(msg, "usage_metadata", None)
                if um:
                    return int(um.get("input_tokens", 0)), int(um.get("output_tokens", 0))
                meta = getattr(msg, "response_metadata", None) or {}
                tu = meta.get("token_usage") or meta.get("usage") or {}
                if tu:
                    prompt = tu.get("prompt_tokens", tu.get("input_tokens", 0))
                    completion = tu.get("completion_tokens", tu.get("output_tokens", 0))
                    return int(prompt or 0), int(completion or 0)
        llm_output = getattr(response, "llm_output", None) or {}
        tu = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if tu:
            return int(tu.get("prompt_tokens", 0) or 0), int(tu.get("completion_tokens", 0) or 0)
    except Exception:  # noqa: BLE001 — never let accounting break a response
        pass
    return 0, 0


@dataclass
class StreamCollector(AsyncCallbackHandler):
    """Async callback that pushes streamed LLM tokens + structured events to a queue and
    accumulates usage across every model call in the run (agents call the model several
    times per turn).

    The orchestrator awaits the queue and republishes events as SSE/WS frames. With
    `emit_events=False` only the accounting side runs (non-streaming generation).
    """

    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    emit_events: bool = True
    started_at: float = field(default_factory=time.time)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    saw_token: bool = False
    _tool_runs: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def _emit(self, evt: dict[str, Any]) -> None:
        if self.emit_events:
            await self.queue.put(evt)

    async def on_llm_new_token(self, token: str, **_: Any) -> None:
        if not token:
            return
        self.saw_token = True
        await self._emit({"type": "token", "data": token})

    async def on_llm_end(self, response: Any, **_: Any) -> None:
        self.llm_calls += 1
        prompt, completion = extract_usage(response)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        await self._emit(
            {
                "type": "usage",
                "data": {
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "total_tokens": self.prompt_tokens + self.completion_tokens,
                },
            }
        )

    async def on_tool_start(self, serialized: Any, input_str: str, *, run_id: UUID, **kw: Any) -> None:
        name = serialized.get("name") if isinstance(serialized, dict) else str(serialized)
        inputs = kw.get("inputs")
        self._tool_runs[str(run_id)] = {
            "tool": name,
            "input": inputs if isinstance(inputs, dict) else input_str,
            "started": time.time(),
        }
        await self._emit(
            {
                "type": "tool_call",
                "data": {
                    "tool": name,
                    "input": inputs if isinstance(inputs, dict) else input_str,
                    "run_id": str(run_id),
                    "phase": "start",
                },
            }
        )

    async def on_tool_end(self, output: Any, *, run_id: UUID, **_: Any) -> None:
        run = self._tool_runs.pop(str(run_id), {})
        TOOL_CALLS.labels(run.get("tool") or "unknown", "ok").inc()
        await self._emit(
            {
                "type": "tool_call",
                "data": {
                    "tool": run.get("tool"),
                    "input": run.get("input"),
                    "run_id": str(run_id),
                    "phase": "end",
                    "output": str(getattr(output, "content", output))[:5000],
                    "latency_ms": int((time.time() - run.get("started", time.time())) * 1000),
                },
            }
        )

    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **_: Any) -> None:
        run = self._tool_runs.pop(str(run_id), {})
        TOOL_CALLS.labels(run.get("tool") or "unknown", "error").inc()
        await self._emit(
            {
                "type": "tool_call",
                "data": {
                    "tool": run.get("tool"),
                    "input": run.get("input"),
                    "run_id": str(run_id),
                    "phase": "error",
                    "error": str(error)[:1000],
                },
            }
        )

    async def aiter(self) -> AsyncIterator[dict]:
        while True:
            evt = await self.queue.get()
            if evt is None:
                return
            yield evt

    async def finalize(self) -> None:
        await self.queue.put(None)
