from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.config import settings


def build_chat_llm(
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    streaming: bool = True,
) -> ChatOpenAI:
    """Factory for ChatOpenAI instances.

    Swap providers (Anthropic, Azure, Ollama, …) by changing the body of this
    function — every chain calls back through here so no other code needs to change.
    """
    return ChatOpenAI(
        api_key=settings.OPENAI_API_KEY,
        model=model or settings.OPENAI_CHAT_MODEL,
        temperature=temperature if temperature is not None else settings.CHAT_TEMPERATURE,
        max_tokens=max_tokens or settings.CHAT_RESPONSE_MAX_TOKENS,
        streaming=streaming,
        timeout=60,
        max_retries=2,
    )


@lru_cache
def get_embeddings() -> OpenAIEmbeddings:
    """Embeddings model — cached because it's stateless and reusable across requests."""
    return OpenAIEmbeddings(
        api_key=settings.OPENAI_API_KEY,
        model=settings.OPENAI_EMBEDDING_MODEL,
    )
