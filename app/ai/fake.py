"""Deterministic fake chat model for tests and keyless local development.

Enabled with `AI_FAKE_MODE=true`. Streams word-by-word, reports usage metadata like a
real provider, and accepts `bind_tools` (tools are ignored; the model answers directly),
so every code path — plain chat, RAG, agent, title generation, summarization — runs
without network access.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Any, AsyncIterator, Iterator, Optional, Sequence

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

_scripted: deque[str] = deque()

FAKE_MODEL_NAME = "nexusbot-fake"


def script_responses(*texts: str) -> None:
    """Queue exact replies for upcoming calls (consumed in order). Tests use this."""
    _scripted.extend(texts)


def clear_scripted_responses() -> None:
    _scripted.clear()


def _text_of(message: BaseMessage) -> str:
    c = message.content
    if isinstance(c, str):
        return c
    return " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class FakeChatModel(BaseChatModel):
    """Echoes the last human message unless a scripted reply is queued."""

    delay: float = 0.0
    prefix: str = "(fake) "

    @property
    def _llm_type(self) -> str:
        return FAKE_MODEL_NAME

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": FAKE_MODEL_NAME}

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> FakeChatModel:  # type: ignore[override]
        return self

    def _reply(self, messages: list[BaseMessage]) -> str:
        if _scripted:
            return _scripted.popleft()
        last_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        text = _text_of(last_human).strip() if last_human else ""
        return f"{self.prefix}You said: {text}" if text else f"{self.prefix}Hello!"

    def _usage(self, messages: list[BaseMessage], reply: str) -> UsageMetadata:
        prompt = sum(_approx_tokens(_text_of(m)) + 4 for m in messages)
        completion = _approx_tokens(reply)
        return UsageMetadata(input_tokens=prompt, output_tokens=completion, total_tokens=prompt + completion)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        reply = self._reply(messages)
        msg = AIMessage(
            content=reply,
            usage_metadata=self._usage(messages, reply),
            response_metadata={"model_name": FAKE_MODEL_NAME},
        )
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _pieces(self, reply: str) -> list[str]:
        return re.findall(r"\S+\s*|\s+", reply) or [reply]

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        reply = self._reply(messages)
        pieces = self._pieces(reply)
        usage = self._usage(messages, reply)
        for i, piece in enumerate(pieces):
            last = i == len(pieces) - 1
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(
                    content=piece,
                    usage_metadata=usage if last else None,
                    response_metadata={"model_name": FAKE_MODEL_NAME} if last else {},
                )
            )
            if run_manager:
                run_manager.on_llm_new_token(piece, chunk=chunk)
            yield chunk

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        reply = self._reply(messages)
        pieces = self._pieces(reply)
        usage = self._usage(messages, reply)
        for i, piece in enumerate(pieces):
            if self.delay:
                await asyncio.sleep(self.delay)
            last = i == len(pieces) - 1
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(
                    content=piece,
                    usage_metadata=usage if last else None,
                    response_metadata={"model_name": FAKE_MODEL_NAME} if last else {},
                )
            )
            if run_manager:
                await run_manager.on_llm_new_token(piece, chunk=chunk)
            yield chunk
