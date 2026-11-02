"""Attachments: upload/sniffing, vision inputs, chat-with-a-file, artifacts, storage backends."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

import pytest

from app.ai.fake import last_prompt, script_responses, tool_call_message
from app.config import settings
from app.storage.local import LocalStorage
from app.storage.s3 import S3Storage
from app.utils.media import image_size, sniff_audio, sniff_image, wav_bytes
from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"


def png_bytes(width: int = 3, height: int = 2) -> bytes:
    """A valid tiny PNG (one solid colour)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


async def _session(client, acct) -> str:
    r = await client.post(f"{P}/sessions", json={"title": "attachments"}, headers=auth_headers(acct))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _upload(client, acct, sid: str, name: str, data: bytes, mime: str = "application/octet-stream"):
    return await client.post(
        f"{P}/sessions/{sid}/attachments", files={"file": (name, data, mime)}, headers=auth_headers(acct)
    )


def test_media_sniffing_and_dimensions():
    assert sniff_image(png_bytes()) == "image/png"
    assert image_size(png_bytes(7, 5)) == (7, 5)
    assert sniff_image(b"GIF89a" + b"\x05\x00\x03\x00") == "image/gif"
    assert image_size(b"GIF89a" + struct.pack("<HH", 5, 3)) == (5, 3)
    assert sniff_image(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert sniff_image(b"plain text") is None
    wav = wav_bytes(b"\x00\x00" * 800, sample_rate=8000)
    assert sniff_audio(wav) == "audio/wav"
    assert sniff_audio(b"ID3\x03") == "audio/mpeg" and sniff_audio(b"OggS") == "audio/ogg"
    assert sniff_audio(b"\x00\x00\x00\x18ftypM4A ") == "audio/x-m4a"
    assert sniff_audio(b"nope") is None


async def test_upload_classifies_image_file_and_audio(client):
    acct = await register_and_login(client)
    sid = await _session(client, acct)

    r = await _upload(client, acct, sid, "photo.png", png_bytes(4, 3), "image/png")
    assert r.status_code == 201, r.text
    img = r.json()
    assert img["kind"] == "image" and img["mime_type"] == "image/png"
    assert (img["width"], img["height"]) == (4, 3) and img["message_id"] is None
    assert img["url"].endswith(f"/sessions/{sid}/attachments/{img['id']}/content")

    r = await _upload(
        client, acct, sid, "notes.md", b"# Plan\n\nShip attachments by Friday.", "text/markdown"
    )
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["kind"] == "file" and doc["has_text"] is True and "Friday" in doc["text_excerpt"]

    r = await _upload(client, acct, sid, "clip.wav", wav_bytes(b"\x00\x01" * 4000), "audio/wav")
    assert r.status_code == 201, r.text
    audio = r.json()
    assert audio["kind"] == "audio" and audio["mime_type"] == "audio/wav"
    assert audio["has_text"] is True and "fake transcript" in audio["text_excerpt"]

    # Bytes are served back; listing shows all three; other users see nothing.
    r = await client.get(img["url"], headers=auth_headers(acct))
    assert r.status_code == 200 and r.content == png_bytes(4, 3)
    assert r.headers["content-type"].startswith("image/png")
    r = await client.get(f"{P}/sessions/{sid}/attachments", headers=auth_headers(acct))
    assert {a["kind"] for a in r.json()["items"]} == {"image", "file", "audio"}
    other = await register_and_login(client)
    r = await client.get(img["url"], headers=auth_headers(other))
    assert r.status_code == 404

    # Rejections: disguised executable, unknown extension, oversized.
    r = await _upload(client, acct, sid, "evil.png", b"MZ\x90\x00 not an image", "image/png")
    assert r.status_code == 422
    r = await _upload(client, acct, sid, "tool.exe", b"MZ\x90\x00", "application/octet-stream")
    assert r.status_code == 422
    r = await _upload(client, acct, sid, "empty.txt", b"", "text/plain")
    assert r.status_code == 422

    r = await client.delete(f"{P}/sessions/{sid}/attachments/{doc['id']}", headers=auth_headers(acct))
    assert r.status_code == 200
    r = await client.get(f"{P}/sessions/{sid}/attachments/{doc['id']}", headers=auth_headers(acct))
    assert r.status_code == 404


async def test_image_attachment_reaches_a_vision_model(client):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    img = (await _upload(client, acct, sid, "chart.png", png_bytes(), "image/png")).json()
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "What is in this picture?", "use_rag": False, "attachment_ids": [img["id"]]},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["id"] for a in body["user_message"]["meta"]["attachments"]] == [img["id"]]
    human = last_prompt()[-1]
    assert isinstance(human.content, list)
    parts = {p["type"] for p in human.content}
    assert parts == {"text", "image_url"}
    image_part = next(p for p in human.content if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(image_part["image_url"]["url"].split(",", 1)[1]) == png_bytes()

    # Bound attachments cannot be reused on another message.
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "again", "use_rag": False, "attachment_ids": [img["id"]]},
        headers=auth_headers(acct),
    )
    assert r.status_code == 422
    r = await client.get(f"{P}/sessions/{sid}/attachments/{img['id']}", headers=auth_headers(acct))
    assert r.json()["message_id"] == body["user_message"]["id"]

    # History carries the attachment summary on the user message.
    r = await client.get(f"{P}/sessions/{sid}/messages", headers=auth_headers(acct))
    user = next(m for m in r.json()["items"] if m["role"] == "user")
    assert user["meta"]["attachments"][0]["kind"] == "image"


async def test_image_rejected_for_models_without_vision(client, monkeypatch):
    from app.ai.providers.registry import get_registry

    acct = await register_and_login(client)
    sid = await _session(client, acct)
    img = (await _upload(client, acct, sid, "chart.png", png_bytes(), "image/png")).json()
    spec = get_registry().default
    monkeypatch.setattr(
        get_registry(), "_enabled", {spec.id: spec.__class__(**{**spec.__dict__, "supports_vision": False})}
    )
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "look", "use_rag": False, "attachment_ids": [img["id"]]},
        headers=auth_headers(acct),
    )
    assert r.status_code == 422, r.text
    assert "cannot read images" in r.json()["error"]["message"]
    assert "vision_models" in r.json()["error"]["details"]


async def test_file_attachment_text_is_given_inline_and_streams_carry_attachments(client):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    doc = (await _upload(client, acct, sid, "facts.txt", b"The launch code is 7421.", "text/plain")).json()
    r = await client.post(
        f"{P}/sessions/{sid}/messages/stream",
        json={"content": "What is the launch code?", "use_rag": False, "attachment_ids": [doc["id"]]},
        headers={**auth_headers(acct), "Accept": "text/event-stream"},
    )
    assert r.status_code == 200, r.text
    frames = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    assert frames[0]["type"] == "user_message" and frames[0]["data"]["attachments"][0]["name"] == "facts.txt"
    human = last_prompt()[-1]
    assert isinstance(human.content, str) and "7421" in human.content and "facts.txt" in human.content
    final = frames[-1]
    assert final["type"] == "assistant_message" and final["data"]["artifacts"] == []


async def test_run_python_tool_produces_artifacts_after_approval(client, db):
    from sqlalchemy import select

    from app.models.attachment import Attachment, AttachmentKind

    acct = await register_and_login(client)
    sid = await _session(client, acct)
    code = "import os\nprint(2+2)\nos.makedirs('output', exist_ok=True)\nopen('output/result.txt','w').write('four')\n"
    script_responses(tool_call_message("run_python", {"code": code}), "It printed 4 and saved result.txt")
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={
            "content": "compute 2+2 and save it",
            "use_rag": False,
            "use_tools": True,
            "tool_names": ["run_python"],
        },
        headers=auth_headers(acct),
    )
    assert r.status_code == 200, r.text
    approval = r.json()["approval"]
    assert approval and approval["tool_calls"][0]["tool"] == "run_python"

    r = await client.post(
        f"{P}/sessions/{sid}/approvals/{approval['approval_id']}",
        json={"decision": "approve"},
        headers={**auth_headers(acct), "Accept": "text/event-stream"},
    )
    assert r.status_code == 200, r.text
    frames = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    types = [f["type"] for f in frames]
    assert "artifact" in types
    artifact = next(f for f in frames if f["type"] == "artifact")["data"]
    assert artifact["name"] == "result.txt" and artifact["kind"] == "artifact"
    tool_end = next(f for f in frames if f["type"] == "tool_call" and f["data"].get("phase") == "end")["data"]
    assert "stdout:\n4" in tool_end["output"] and "Artifacts: result.txt" in tool_end["output"]
    final = frames[-1]["data"]
    assert final["content"] == "It printed 4 and saved result.txt"
    assert [a["id"] for a in final["artifacts"]] == [artifact["id"]]

    rows = (await db.execute(select(Attachment).where(Attachment.session_id == sid))).scalars().all()
    assert len(rows) == 1 and rows[0].kind == AttachmentKind.ARTIFACT
    assert str(rows[0].message_id) == final["id"]
    r = await client.get(artifact["url"], headers=auth_headers(acct))
    assert r.status_code == 200 and r.content == b"four"


async def test_run_python_timeout_and_disabled(client, monkeypatch):
    from uuid import uuid4

    from app.ai.tools.context import ToolContext
    from app.ai.tools.python_sandbox import PythonSandboxTool

    tool = PythonSandboxTool(ctx=ToolContext(user_id=uuid4()))
    out = await tool._arun(code="print('hi')")
    assert "stdout:\nhi" in out
    out = await tool._arun(code="raise SystemExit(3)")
    assert "Exit code 3" in out
    monkeypatch.setattr(settings, "CODE_EXECUTION_TIMEOUT_SECONDS", 1)
    out = await tool._arun(code="import time\nwhile True: time.sleep(0.1)")
    assert "timed out" in out
    monkeypatch.setattr(settings, "CODE_EXECUTION_ENABLED", False)
    out = await tool._arun(code="print(1)")
    assert "disabled" in out
    from app.ai.tools.registry import list_tool_names

    assert "run_python" not in list_tool_names()


async def test_local_and_s3_storage_backends(tmp_path):
    local = LocalStorage(tmp_path)
    await local.put_bytes("a/b/c.txt", b"hello", content_type="text/plain")
    assert await local.exists("a/b/c.txt") and await local.get_bytes("a/b/c.txt") == b"hello"
    assert b"".join([c async for c in local.stream("a/b/c.txt", chunk_size=2)]) == b"hello"
    async with local.local_path("a/b/c.txt") as path:
        assert Path(path).read_text() == "hello"
    assert await local.presigned_url("a/b/c.txt", expires_seconds=60) is None
    with pytest.raises(ValueError):
        local.path_for("../escape")
    await local.delete("a/b/c.txt")
    assert not await local.exists("a/b/c.txt")

    class FakeBody:
        def __init__(self, data: bytes) -> None:
            self.data = data
            self.pos = 0

        def read(self, n: int = -1) -> bytes:
            if n < 0:
                out, self.pos = self.data[self.pos :], len(self.data)
            else:
                out = self.data[self.pos : self.pos + n]
                self.pos += n
            return out

    class FakeClient:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}
            self.deleted: list[str] = []

        def put_object(self, Bucket, Key, Body, ContentType):
            self.objects[Key] = Body if isinstance(Body, bytes) else Body.read()

        def get_object(self, Bucket, Key):
            return {"Body": FakeBody(self.objects[Key])}

        def delete_object(self, Bucket, Key):
            self.deleted.append(Key)
            self.objects.pop(Key, None)

        def head_object(self, Bucket, Key):
            if Key not in self.objects:
                raise KeyError(Key)

        def download_file(self, Bucket, Key, Filename):
            Path(Filename).write_bytes(self.objects[Key])

        def generate_presigned_url(self, op, Params, ExpiresIn):
            return f"https://s3.test/{Params['Bucket']}/{Params['Key']}?exp={ExpiresIn}"

    client = FakeClient()
    s3 = S3Storage(bucket="bkt", client=client)
    src = tmp_path / "up.bin"
    src.write_bytes(b"\x00\x01\x02")
    await s3.put_file("k/up.bin", src, content_type="application/octet-stream")
    assert not src.exists() and client.objects["k/up.bin"] == b"\x00\x01\x02"
    await s3.put_bytes("k/t.txt", b"abc", content_type="text/plain")
    assert (
        await s3.get_bytes("k/t.txt") == b"abc" and await s3.exists("k/t.txt") and not await s3.exists("nope")
    )
    assert b"".join([c async for c in s3.stream("k/t.txt", chunk_size=2)]) == b"abc"
    async with s3.local_path("k/t.txt") as path:
        assert path.read_bytes() == b"abc"
    assert not path.exists()
    url = await s3.presigned_url("k/t.txt", expires_seconds=30, filename="t.txt")
    assert url and url.startswith("https://s3.test/bkt/k/t.txt")
    assert await s3.presigned_url("k/t.txt", expires_seconds=0) is None
    await s3.delete("k/t.txt")
    assert client.deleted == ["k/t.txt"]


def test_prompt_token_estimate_ignores_image_payloads():
    from app.ai.orchestrator import _estimable_text, _image_parts

    content = [
        {"type": "text", "text": "describe this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100_000}},
    ]
    assert _estimable_text(content) == "describe this" and _image_parts(content) == 1
    assert _estimable_text("plain") == "plain" and _image_parts("plain") == 0
