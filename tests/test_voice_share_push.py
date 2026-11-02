"""Voice (STT/TTS, voice turns, spoken replies), export & share links, push devices."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from app.ai.voice import tts
from app.ai.voice.stt import script_transcripts
from app.config import settings
from app.services import push_service
from app.utils.media import wav_bytes, wav_duration_seconds
from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"


async def _session(client, acct, title="voice") -> str:
    r = await client.post(f"{P}/sessions", json={"title": title}, headers=auth_headers(acct))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _frames(text: str) -> list[dict]:
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]


async def test_voices_transcription_and_speech_endpoints(client):
    acct = await register_and_login(client)
    r = await client.get(f"{P}/audio/voices", headers=auth_headers(acct))
    assert r.status_code == 200
    info = r.json()
    assert info["stt_available"] and info["tts_available"] and info["default_format"] == "wav"
    assert {v["id"] for v in info["voices"]} >= {"alloy"}

    clip = wav_bytes(b"\x00\x00" * 16000, sample_rate=8000)  # 2 s of silence
    script_transcripts("turn on the lights")
    r = await client.post(
        f"{P}/audio/transcriptions",
        files={"file": ("clip.wav", clip, "audio/wav")},
        data={"language": "en"},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "turn on the lights" and body["provider"] == "fake"
    assert abs(body["duration_seconds"] - 2.0) < 0.01
    r = await client.post(
        f"{P}/audio/transcriptions",
        files={"file": ("x.bin", b"not audio", "audio/wav")},
        headers=auth_headers(acct),
    )
    assert r.status_code == 422

    r = await client.post(f"{P}/audio/speech", json={"text": "Hello there"}, headers=auth_headers(acct))
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/wav")
    assert r.content[:4] == b"RIFF" and wav_duration_seconds(r.content) > 0
    r = await client.post(
        f"{P}/audio/speech", json={"text": "x", "voice": "nobody"}, headers=auth_headers(acct)
    )
    assert r.status_code == 422


async def test_voice_turn_transcribes_and_speaks_the_reply(client, monkeypatch):
    sent: list[dict] = []

    async def fake_sender(payload: dict) -> bool:
        sent.append(payload)
        return True

    push_service.set_sender(fake_sender)
    try:
        acct = await register_and_login(client)
        r = await client.post(
            f"{P}/devices",
            json={"platform": "android", "token": "device-token-123"},
            headers=auth_headers(acct),
        )
        assert r.status_code == 201 and r.json()["token_suffix"] == "en-123"
        sid = await _session(client, acct)
        script_transcripts("what is two plus two")
        clip = wav_bytes(b"\x10\x00" * 4000)
        r = await client.post(
            f"{P}/sessions/{sid}/messages/voice",
            files={"file": ("q.wav", clip, "audio/wav")},
            data={"use_rag": "false", "reply_audio": "true"},
            headers=auth_headers(acct),
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["transcript"] == "what is two plus two" and body["generation_id"]

        r = await client.get(
            f"{P}/sessions/{sid}/generations/{body['generation_id']}/events",
            headers={**auth_headers(acct), "Accept": "text/event-stream"},
        )
        frames = _frames(r.text)
        types = [f["type"] for f in frames]
        assert types[0] == "user_message" and "audio_chunk" in types and "audio_done" in types
        assert types.index("audio_done") < types.index("assistant_message")
        assert frames[0]["data"]["attachments"][0]["kind"] == "audio"
        chunk = next(f for f in frames if f["type"] == "audio_chunk")["data"]
        assert chunk["format"] == "wav" and base64.b64decode(chunk["data"])[:4] == b"RIFF"
        done = next(f for f in frames if f["type"] == "audio_done")["data"]
        assert done["chunks"] >= 1 and done["attachment"]["kind"] == "audio"
        final = frames[-1]["data"]
        assert final["audio"]["attachment_id"] == done["attachment"]["id"]
        assert "two plus two" in final["content"]
        r = await client.get(final["audio"]["url"], headers=auth_headers(acct))
        assert r.status_code == 200 and r.content[:4] == b"RIFF"

        # The background turn notified the registered device (after the log closed).
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.05)
        assert len(sent) == 1
        assert sent[0]["devices"] == [{"platform": "android", "token": "device-token-123"}]
        assert sent[0]["data"]["generation_id"] == body["generation_id"]
        assert sent[0]["notification"]["title"] == "Reply ready"
    finally:
        push_service.set_sender(None)


async def test_voice_reply_on_text_sends_and_unavailable_providers(client, monkeypatch):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    r = await client.post(
        f"{P}/sessions/{sid}/messages/stream",
        json={"content": "Say hi. Then say bye.", "use_rag": False, "voice_reply": True, "voice": "fable"},
        headers={**auth_headers(acct), "Accept": "text/event-stream"},
    )
    assert r.status_code == 200
    frames = _frames(r.text)
    chunks = [f for f in frames if f["type"] == "audio_chunk"]
    assert len(chunks) >= 1 and all(c["data"]["mime_type"] == "audio/wav" for c in chunks)
    assert tts.split_sentences("Say hi. Then say bye.", max_chars=8) == ["Say hi.", "Then say bye."]
    assert tts.split_sentences("Say hi. Then say bye.") == ["Say hi. Then say bye."]

    # Blocking sends get the whole clip as an attachment instead of chunks.
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "Speak this.", "use_rag": False, "voice_reply": True},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200, r.text
    audio = r.json()["assistant_message"]["meta"]["audio"]
    assert audio["format"] == "wav" and audio["chunks"] == 1
    r = await client.get(audio["url"], headers=auth_headers(acct))
    assert r.status_code == 200 and r.content[:4] == b"RIFF"

    monkeypatch.setattr(settings, "TTS_PROVIDER", "none")
    monkeypatch.setattr(settings, "STT_PROVIDER", "none")
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "x", "use_rag": False, "voice_reply": True},
        headers=auth_headers(acct),
    )
    assert r.status_code == 503 and r.json()["error"]["details"]["code"] == "voice_unavailable"
    r = await client.post(f"{P}/audio/speech", json={"text": "x"}, headers=auth_headers(acct))
    assert r.status_code == 503
    r = await client.get(f"{P}/audio/voices", headers=auth_headers(acct))
    assert r.json()["stt_available"] is False and r.json()["voices"] == []


async def test_export_markdown_json_and_share_links(client, monkeypatch):
    acct = await register_and_login(client)
    sid = await _session(client, acct, title="Export me")
    for text in ("first question", "second question"):
        r = await client.post(
            f"{P}/sessions/{sid}/messages",
            json={"content": text, "use_rag": False},
            headers=auth_headers(acct),
        )
        assert r.status_code == 200

    r = await client.get(f"{P}/sessions/{sid}/export", headers=auth_headers(acct))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert 'filename="Export_me.md"' in r.headers["content-disposition"]
    md = r.text
    assert md.startswith("# Export me") and "## You" in md and "## Assistant" in md
    assert md.index("first question") < md.index("second question")
    r = await client.get(f"{P}/sessions/{sid}/export?format=json", headers=auth_headers(acct))
    assert r.status_code == 200
    data = r.json()
    assert (
        data["session"]["title"] == "Export me"
        and [m["role"] for m in data["messages"]] == ["user", "assistant"] * 2
    )

    r = await client.post(
        f"{P}/sessions/{sid}/share", json={"expires_in_hours": 2}, headers=auth_headers(acct)
    )
    assert r.status_code == 201, r.text
    share = r.json()
    assert share["token"] and share["url"] == f"http://test{P}/shared/{share['token']}"

    # Public, unauthenticated, read-only.
    r = await client.get(f"{P}/shared/{share['token']}")
    assert r.status_code == 200
    public = r.json()
    assert public["session"]["title"] == "Export me" and len(public["messages"]) == 4
    assert all("id" in m and "content" in m for m in public["messages"])
    r = await client.get(f"{P}/shared/{share['token']}/markdown")
    assert r.status_code == 200 and "## Assistant" in r.text
    r = await client.get(f"{P}/sessions/{sid}/share", headers=auth_headers(acct))
    assert (
        len(r.json()["items"]) == 1
        and r.json()["items"][0]["views"] == 2
        and r.json()["items"][0]["token"] is None
    )

    r = await client.delete(f"{P}/sessions/{sid}/share", headers=auth_headers(acct))
    assert r.status_code == 200
    r = await client.get(f"{P}/shared/{share['token']}")
    assert r.status_code == 404
    r = await client.get(f"{P}/shared/not-a-token")
    assert r.status_code == 404
    other = await register_and_login(client)
    r = await client.post(f"{P}/sessions/{sid}/share", json={}, headers=auth_headers(other))
    assert r.status_code == 404


async def test_device_registration_is_idempotent_and_scoped(client):
    acct = await register_and_login(client)
    other = await register_and_login(client)
    r = await client.post(
        f"{P}/devices", json={"platform": "ios", "token": "apns-token-abcdef"}, headers=auth_headers(acct)
    )
    assert r.status_code == 201
    r = await client.post(
        f"{P}/devices",
        json={"platform": "ios", "token": "apns-token-abcdef", "app_version": "1.4.0"},
        headers=auth_headers(acct),
    )
    assert r.status_code == 201 and r.json()["app_version"] == "1.4.0"
    r = await client.get(f"{P}/devices", headers=auth_headers(acct))
    assert len(r.json()["items"]) == 1 and r.json()["push_enabled"] is False
    r = await client.post(
        f"{P}/devices", json={"platform": "tv", "token": "x" * 10}, headers=auth_headers(acct)
    )
    assert r.status_code == 422
    r = await client.delete(f"{P}/devices/apns-token-abcdef", headers=auth_headers(other))
    assert r.status_code == 404
    r = await client.delete(f"{P}/devices/apns-token-abcdef", headers=auth_headers(acct))
    assert r.status_code == 200
    r = await client.get(f"{P}/devices", headers=auth_headers(acct))
    assert r.json()["items"] == []
