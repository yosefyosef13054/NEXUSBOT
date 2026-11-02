"""Deterministic fake chat model for tests and keyless local development.

Enabled with `AI_FAKE_MODE=true`. Streams word-by-word, reports usage metadata like a
real provider, accepts `bind_tools`, and can be scripted to *call tools*
(`script_responses(AIMessage(tool_calls=[...]))`), so every code path — plain chat, RAG,
the LangGraph agent with approvals, title generation, summarization — runs without
network access.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from typing import Any, AsyncIterator, Iterator, Optional, Sequence

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.messages.tool import ToolCall, ToolCallChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

_scripted: deque[str | AIMessage] = deque()
_last_prompt: list[BaseMessage] = []

FAKE_MODEL_NAME = "nexusbot-fake"


def last_prompt() -> list[BaseMessage]:
    """The messages of the most recent model call (tests inspect multimodal content)."""
    return list(_last_prompt)


def script_responses(*items: str | AIMessage) -> None:
    """Queue exact replies for upcoming calls (consumed in order). Tests use this.

    An `AIMessage` with `tool_calls` makes the model request those tools; a plain string is
    streamed word by word.
    """
    _scripted.extend(items)


def tool_call_message(
    name: str, args: dict[str, Any], *, call_id: str = "call_1", content: str = ""
) -> AIMessage:
    """Convenience for scripting a tool call."""
    return AIMessage(content=content, tool_calls=[ToolCall(name=name, args=args, id=call_id)])


def clear_scripted_responses() -> None:
    _scripted.clear()


def scripted_remaining() -> int:
    return len(_scripted)


def _text_of(message: BaseMessage) -> str:
    c = message.content
    if isinstance(c, str):
        return c
    parts = []
    for p in c:
        if isinstance(p, dict):
            parts.append(p.get("text", "") if p.get("type") != "image_url" else "[image]")
        else:
            parts.append(str(p))
    return " ".join(parts)


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

    # ---- scripted / echo behaviour --------------------------------------------

    def _reply(self, messages: list[BaseMessage]) -> AIMessage:
        _last_prompt[:] = list(messages)
        if _scripted:
            item = _scripted.popleft()
            if isinstance(item, AIMessage):
                return item
            return AIMessage(content=item)
        last_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        text = _text_of(last_human).strip() if last_human else ""
        return AIMessage(content=f"{self.prefix}You said: {text}" if text else f"{self.prefix}Hello!")

    def _usage(self, messages: list[BaseMessage], reply: AIMessage) -> UsageMetadata:
        prompt = sum(_approx_tokens(_text_of(m)) + 4 for m in messages)
        completion = _approx_tokens(_text_of(reply)) + 8 * len(reply.tool_calls or [])
        return UsageMetadata(input_tokens=prompt, output_tokens=completion, total_tokens=prompt + completion)

    def _finalize(self, messages: list[BaseMessage], reply: AIMessage) -> AIMessage:
        return AIMessage(
            content=reply.content,
            tool_calls=list(reply.tool_calls or []),
            usage_metadata=self._usage(messages, reply),
            response_metadata={"model_name": FAKE_MODEL_NAME},
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        msg = self._finalize(messages, self._reply(messages))
        return ChatResult(generations=[ChatGeneration(message=msg)])

    # ---- streaming ---------------------------------------------------------------

    def _chunks(self, messages: list[BaseMessage], reply: AIMessage) -> list[ChatGenerationChunk]:
        usage = self._usage(messages, reply)
        meta = {"model_name": FAKE_MODEL_NAME}
        if reply.tool_calls:
            # Tool-call turns arrive as a single chunk, like providers emit them.
            chunks = [
                ToolCallChunk(name=tc["name"], args=json.dumps(tc["args"]), id=tc.get("id"), index=i)
                for i, tc in enumerate(reply.tool_calls)
            ]
            return [
                ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=_text_of(reply),
                        tool_call_chunks=chunks,
                        usage_metadata=usage,
                        response_metadata=meta,
                    )
                )
            ]
        text = _text_of(reply)
        pieces = re.findall(r"\S+\s*|\s+", text) or [text]
        out: list[ChatGenerationChunk] = []
        for i, piece in enumerate(pieces):
            last = i == len(pieces) - 1
            out.append(
                ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=piece,
                        usage_metadata=usage if last else None,
                        response_metadata=meta if last else {},
                    )
                )
            )
        return out

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        for chunk in self._chunks(messages, self._reply(messages)):
            if run_manager and isinstance(chunk.message.content, str):
                run_manager.on_llm_new_token(chunk.message.content, chunk=chunk)
            yield chunk

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._chunks(messages, self._reply(messages)):
            if self.delay:
                await asyncio.sleep(self.delay)
            if run_manager and isinstance(chunk.message.content, str):
                await run_manager.on_llm_new_token(chunk.message.content, chunk=chunk)
            yield chunk
