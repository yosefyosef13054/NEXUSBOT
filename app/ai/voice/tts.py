from __future__ import annotations

import math
import re
import struct
from typing import AsyncIterator

from app.config import settings
from app.core.exceptions import ServiceUnavailableError
from app.utils.media import wav_bytes

OPENAI_VOICES = ("alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer")
FAKE_VOICES = ("alloy", "fable")
MIME_BY_FORMAT = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")


def provider_name() -> str | None:
    p = settings.TTS_PROVIDER.lower()
    if p == "none":
        return None
    if p == "auto":
        if settings.AI_FAKE_MODE:
            return "fake"
        return "openai" if settings.OPENAI_API_KEY else None
    return p


def available() -> bool:
    return provider_name() is not None


def voices() -> list[str]:
    provider = provider_name()
    if provider == "openai":
        return list(OPENAI_VOICES)
    if provider == "fake":
        return list(FAKE_VOICES)
    return []


def default_format() -> str:
    # The fake provider only knows how to write WAV.
    return "wav" if provider_name() == "fake" else settings.TTS_FORMAT


def mime_for(fmt: str) -> str:
    return MIME_BY_FORMAT.get(fmt, "application/octet-stream")


def split_sentences(text: str, *, max_chars: int = 400) -> list[str]:
    """Sentence-sized chunks so audio can start before the whole reply is synthesized."""
    pieces = [p.strip() for p in _SENTENCE_SPLIT.split(text.strip()) if p.strip()]
    out: list[str] = []
    buf = ""
    for piece in pieces:
        if buf and len(buf) + len(piece) + 1 > max_chars:
            out.append(buf)
            buf = piece
        else:
            buf = f"{buf} {piece}".strip()
    if buf:
        out.append(buf)
    return out


def _fake_tone(text: str, *, voice: str) -> bytes:
    """A short tone whose length tracks the text (so clients can exercise playback)."""
    rate = 8000
    seconds = min(0.05 * max(len(text), 1), 3.0)
    freq = 440.0 if voice != "fable" else 330.0
    n = int(rate * seconds)
    samples = struct.pack(f"<{n}h", *(int(12000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)))
    return wav_bytes(samples, sample_rate=rate)


async def synthesize(
    text: str, *, voice: str | None = None, fmt: str | None = None, chunk_size: int = 1 << 15
) -> AsyncIterator[bytes]:
    """Stream audio bytes for `text`."""
    provider = provider_name()
    if provider is None:
        raise ServiceUnavailableError(
            "Text-to-speech is not configured", details={"code": "voice_unavailable"}
        )
    voice = voice or settings.TTS_VOICE
    fmt = fmt or default_format()
    text = text[: settings.VOICE_MAX_CHARS]
    if provider == "fake":
        data = _fake_tone(text, voice=voice)
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]
        return
    if provider != "openai":
        raise ServiceUnavailableError(
            f"Unknown TTS provider {provider}", details={"code": "voice_unavailable"}
        )
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    async with client.audio.speech.with_streaming_response.create(
        model=settings.TTS_MODEL,
        voice=voice,  # type: ignore[arg-type]
        input=text,
        response_format=fmt,  # type: ignore[arg-type]
    ) as response:
        async for chunk in response.iter_bytes(chunk_size):
            yield chunk


async def synthesize_bytes(text: str, *, voice: str | None = None, fmt: str | None = None) -> bytes:
    return b"".join([chunk async for chunk in synthesize(text, voice=voice, fmt=fmt)])
