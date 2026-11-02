from __future__ import annotations

from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from app.ai.providers.catalog import ModelSpec
from app.ai.providers.factory import build_chat_model
from app.ai.providers.registry import get_registry
from app.config import settings


def resolve_spec(model: str | None = None, *candidates: str | None) -> ModelSpec:
    """Resolve the model to use: explicit request → further candidates → server default."""
    return get_registry().resolve(model, *candidates)


def build_chat_llm(
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    streaming: bool = True,
    spec: ModelSpec | None = None,
) -> BaseChatModel:
    """Factory for chat models across providers.

    `spec` wins when given; otherwise `model` is resolved through the registry (which also
    applies `AI_FAKE_MODE`). Every chain, agent and maintenance task goes through here, so
    adding a provider is a catalog entry plus a branch in `app/ai/providers/factory.py`.
    """
    spec = spec or resolve_spec(model)
    return build_chat_model(spec, temperature=temperature, max_tokens=max_tokens, streaming=streaming)


def chat_model_name(model: str | None = None) -> str:
    return resolve_spec(model).id


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
