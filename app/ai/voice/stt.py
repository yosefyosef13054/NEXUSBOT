from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.core.exceptions import ServiceUnavailableError
from app.core.logger import get_logger
from app.utils.media import wav_duration_seconds

logger = get_logger(__name__)

_scripted: deque[str] = deque()


def script_transcripts(*texts: str) -> None:
    """Queue transcripts the fake provider returns (tests)."""
    _scripted.extend(texts)


def clear_scripted_transcripts() -> None:
    _scripted.clear()


@dataclass
class Transcript:
    text: str
    language: str | None = None
    duration_seconds: float | None = None
    provider: str = "fake"


def provider_name() -> str | None:
    p = settings.STT_PROVIDER.lower()
    if p == "none":
        return None
    if p == "auto":
        if settings.AI_FAKE_MODE:
            return "fake"
        return "openai" if settings.OPENAI_API_KEY else None
    return p


def available() -> bool:
    return provider_name() is not None


async def transcribe(
    data: bytes, *, filename: str = "audio.wav", mime_type: str = "audio/wav", language: str | None = None
) -> Transcript:
    provider = provider_name()
    if provider is None:
        raise ServiceUnavailableError(
            "Speech-to-text is not configured", details={"code": "voice_unavailable"}
        )
    if provider == "fake":
        text = _scripted.popleft() if _scripted else f"(fake transcript of {filename})"
        return Transcript(
            text=text, language=language or "en", duration_seconds=wav_duration_seconds(data), provider="fake"
        )
    if provider == "openai":
        return await _openai_transcribe(data, filename=filename, mime_type=mime_type, language=language)
    raise ServiceUnavailableError(f"Unknown STT provider {provider}", details={"code": "voice_unavailable"})


async def _openai_transcribe(
    data: bytes, *, filename: str, mime_type: str, language: str | None
) -> Transcript:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    kwargs: dict[str, Any] = {"model": settings.STT_MODEL, "file": (filename, data, mime_type)}
    if language:
        kwargs["language"] = language
    if settings.STT_MODEL.startswith("whisper"):
        kwargs["response_format"] = "verbose_json"
    result = await client.audio.transcriptions.create(**kwargs)
    return Transcript(
        text=(getattr(result, "text", "") or "").strip(),
        language=getattr(result, "language", None) or language,
        duration_seconds=getattr(result, "duration", None),
        provider="openai",
    )
