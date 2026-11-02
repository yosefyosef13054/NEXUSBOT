"""Instantiate LangChain chat models for a `ModelSpec`.

Provider SDKs are imported lazily so a missing optional package only fails when that
provider is actually used.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from app.ai.providers.catalog import ModelSpec
from app.config import settings
from app.core.exceptions import AIProviderError

REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 2


def _common_sampling(spec: ModelSpec, temperature: float | None) -> dict[str, Any]:
    if not spec.supports_temperature or temperature is None:
        return {}
    return {"temperature": temperature}


def build_chat_model(
    spec: ModelSpec,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    streaming: bool = True,
) -> BaseChatModel:
    max_out = min(max_tokens or settings.CHAT_RESPONSE_MAX_TOKENS, spec.max_output_tokens)
    temp = temperature if temperature is not None else settings.CHAT_TEMPERATURE

    try:
        if spec.provider == "fake":
            from app.ai.fake import FakeChatModel

            return FakeChatModel(delay=settings.AI_FAKE_DELAY_SECONDS)

        if spec.provider == "openai":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(  # type: ignore[call-arg]
                api_key=SecretStr(settings.OPENAI_API_KEY),
                model=spec.id,
                max_tokens=max_out,
                streaming=streaming,
                stream_usage=True,
                timeout=REQUEST_TIMEOUT_SECONDS,
                max_retries=MAX_RETRIES,
                **_common_sampling(spec, temp),
            )

        if spec.provider == "azure":
            from langchain_openai import AzureChatOpenAI

            return AzureChatOpenAI(  # type: ignore[call-arg]
                api_key=SecretStr(settings.AZURE_OPENAI_API_KEY),
                azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
                api_version=settings.AZURE_OPENAI_API_VERSION,
                azure_deployment=str(spec.extra.get("deployment") or spec.id.removeprefix("azure/")),
                max_tokens=max_out,
                streaming=streaming,
                stream_usage=True,
                timeout=REQUEST_TIMEOUT_SECONDS,
                max_retries=MAX_RETRIES,
                **_common_sampling(spec, temp),
            )

        if spec.provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            # Claude 4.6+ rejects temperature/top_p/top_k: `supports_temperature=False` in the
            # catalog keeps them out of the request.
            return ChatAnthropic(  # type: ignore[call-arg]
                model=spec.id,
                anthropic_api_key=SecretStr(settings.ANTHROPIC_API_KEY),
                max_tokens=max_out,
                streaming=streaming,
                default_request_timeout=REQUEST_TIMEOUT_SECONDS,
                max_retries=MAX_RETRIES,
                **_common_sampling(spec, temp),
            )

        if spec.provider == "google":
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(  # type: ignore[call-arg]
                model=spec.id,
                google_api_key=SecretStr(settings.GOOGLE_API_KEY),
                max_output_tokens=max_out,
                timeout=REQUEST_TIMEOUT_SECONDS,
                max_retries=MAX_RETRIES,
                **_common_sampling(spec, temp),
            )

        if spec.provider == "ollama":
            from langchain_ollama import ChatOllama

            return ChatOllama(
                model=spec.id,
                base_url=settings.OLLAMA_BASE_URL or None,
                num_predict=max_out,
                **_common_sampling(spec, temp),
            )
    except ImportError as e:
        raise AIProviderError(
            f"Provider '{spec.provider}' is not installed",
            details={"provider": spec.provider, "reason": str(e)[:200]},
        ) from e
    except AIProviderError:
        raise
    except Exception as e:  # noqa: BLE001 — e.g. the SDK refusing an empty API key
        raise AIProviderError(
            f"Provider '{spec.provider}' could not be initialised",
            details={"provider": spec.provider, "model": spec.id, "reason": str(e)[:200]},
        ) from e

    raise AIProviderError(f"Unknown provider '{spec.provider}'", details={"model": spec.id})
