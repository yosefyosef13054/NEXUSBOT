from __future__ import annotations

from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from app.config import settings


def build_chat_llm(
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    streaming: bool = True,
) -> BaseChatModel:
    """Factory for chat models.

    With `AI_FAKE_MODE=true` a deterministic in-process model is returned so the whole
    stack runs without provider keys (tests, UI development). Otherwise OpenAI; other
    providers plug in here (see Phase 2 provider registry).
    """
    if settings.AI_FAKE_MODE:
        from app.ai.fake import FakeChatModel

        return FakeChatModel(delay=settings.AI_FAKE_DELAY_SECONDS)

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(  # type: ignore[call-arg]
        api_key=SecretStr(settings.OPENAI_API_KEY),
        model=model or settings.OPENAI_CHAT_MODEL,
        temperature=temperature if temperature is not None else settings.CHAT_TEMPERATURE,
        max_tokens=max_tokens or settings.CHAT_RESPONSE_MAX_TOKENS,
        streaming=streaming,
        # Ask the provider to include token usage in the final streamed chunk, otherwise
        # streamed generations report no usage at all and cost accounting is blind.
        stream_usage=True,
        timeout=60,
        max_retries=2,
    )


def chat_model_name(model: str | None = None) -> str:
    if settings.AI_FAKE_MODE:
        from app.ai.fake import FAKE_MODEL_NAME

        return FAKE_MODEL_NAME
    return model or settings.OPENAI_CHAT_MODEL


@lru_cache
def get_embeddings() -> Embeddings:
    """Embeddings model — cached because it's stateless and reusable across requests."""
    if settings.AI_FAKE_MODE:
        from langchain_core.embeddings.fake import DeterministicFakeEmbedding

        return DeterministicFakeEmbedding(size=settings.OPENAI_EMBEDDING_DIM)

    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(
        api_key=SecretStr(settings.OPENAI_API_KEY),
        model=settings.OPENAI_EMBEDDING_MODEL,
    )


def embedding_model_name() -> str:
    return "fake-embedding" if settings.AI_FAKE_MODE else settings.OPENAI_EMBEDDING_MODEL
