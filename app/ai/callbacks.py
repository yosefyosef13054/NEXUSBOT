from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from uuid import UUID

from langchain_core.callbacks.base import AsyncCallbackHandler


@dataclass
class StreamCollector(AsyncCallbackHandler):
    """Async callback that pushes streamed LLM tokens + structured events to a queue.

    The orchestrator awaits this queue and republishes events as SSE/WS frames
    to the Flutter client.
    """

    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    finish_token: str = "__END__"
    started_at: float = field(default_factory=time.time)
    prompt_tokens: int = 0
    completion_tokens: int = 0

    async def on_llm_new_token(self, token: str, **_: Any) -> None:
        await self.queue.put({"type": "token", "data": token})

    async def on_llm_end(self, response, **_: Any) -> None:
        usage = {}
        try:
            for gen in response.generations:
                for g in gen:
                    meta = g.message.response_metadata or {}
                    usage = meta.get("token_usage") or meta.get("usage") or usage
        except Exception:  # noqa: BLE001
            pass
        if usage:
            self.prompt_tokens = int(usage.get("prompt_tokens", 0))
            self.completion_tokens = int(usage.get("completion_tokens", 0))
        await self.queue.put({"type": "usage", "data": usage})

    async def on_tool_start(self, serialized, input_str, *, run_id: UUID, **_: Any) -> None:
        await self.queue.put(
            {"type": "tool_call", "data": {
                "tool": serialized.get("name") if isinstance(serialized, dict) else str(serialized),
                "input": input_str, "run_id": str(run_id), "phase": "start",
            }}
        )

    async def on_tool_end(self, output, *, run_id: UUID, **_: Any) -> None:
        await self.queue.put(
            {"type": "tool_call", "data": {"run_id": str(run_id), "phase": "end", "output": str(output)[:5000]}}
        )

    async def on_tool_error(self, error, *, run_id: UUID, **_: Any) -> None:
        await self.queue.put(
            {"type": "tool_call", "data": {"run_id": str(run_id), "phase": "error", "error": str(error)}}
        )

    async def aiter(self) -> AsyncIterator[dict]:
        while True:
            evt = await self.queue.get()
            if evt is None:
                return
            yield evt

    async def finalize(self) -> None:
        await self.queue.put(None)
