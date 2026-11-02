"""Upload validation, dedup, ingestion progress/usage, retrieval and cleanup."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from app.ai.rag.pipeline import ingest_document
from app.models.document import DocumentChunk
from app.models.usage import AIUsageLog
from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"
NOTE = "NexusBot secret phrase: alpha bravo charlie delta."


class _Queue:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def delay(self, document_id: str) -> None:
        self.calls.append(document_id)


@pytest.fixture
def queue(monkeypatch) -> _Queue:
    from app.api.v1.endpoints import documents as mod

    q = _Queue()
    monkeypatch.setattr(mod, "ingest_document_task", q)
    return q


async def _upload(
    client, acct, name: str, data: bytes, mime: str = "text/plain", session_id: str | None = None
):
    form = {"session_id": session_id} if session_id else {}
    return await client.post(
        f"{P}/documents", files={"file": (name, data, mime)}, data=form, headers=auth_headers(acct)
    )


async def test_upload_ingest_and_retrieve(client, db, queue):
    acct = await register_and_login(client)
    r = await _upload(client, acct, "notes.txt", NOTE.encode())
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["queued"] is True
    doc = body["document"]
    assert doc["status"] == "pending" and doc["meta"]["sha256"] and doc["mime_type"] == "text/plain"
    assert queue.calls == [doc["id"]]

    await ingest_document(db, document_id=UUID(doc["id"]))

    r = await client.get(f"{P}/documents/{doc['id']}", headers=auth_headers(acct))
    got = r.json()
    assert got["status"] == "ready"
    assert got["meta"]["progress"] == 100 and got["meta"]["chunk_count"] >= 1
    chunks = (
        (await db.execute(select(DocumentChunk).where(DocumentChunk.document_id == UUID(doc["id"]))))
        .scalars()
        .all()
    )
    assert chunks and chunks[0].embedding is not None
    assert "source" not in chunks[0].meta or "/" not in chunks[0].meta["source"]
    usage = (await db.execute(select(AIUsageLog).where(AIUsageLog.operation == "embedding"))).scalars().all()
    assert usage and usage[0].prompt_tokens > 0 and usage[0].meta["document_id"] == doc["id"]

    # Deterministic fake embeddings: the exact chunk text as a query is a perfect match.
    r = await client.post(f"{P}/sessions", json={}, headers=auth_headers(acct))
    sid = r.json()["id"]
    r = await client.post(
        f"{P}/sessions/{sid}/messages", json={"content": NOTE, "use_rag": True}, headers=auth_headers(acct)
    )
    assert r.status_code == 200, r.text
    citations = r.json()["citations"]
    assert citations and citations[0]["document_name"] == "notes.txt" and citations[0]["score"] > 0.9
    assert r.json()["usage"]["embedding_tokens"] > 0


async def test_upload_dedup_same_content(client, queue):
    acct = await register_and_login(client)
    r1 = await _upload(client, acct, "a.txt", b"same bytes here")
    r2 = await _upload(client, acct, "b.txt", b"same bytes here")
    assert r1.status_code == 202 and r2.status_code == 202
    assert r2.json()["queued"] is False
    assert r2.json()["document"]["id"] == r1.json()["document"]["id"]
    assert queue.calls == [r1.json()["document"]["id"]]
    # Different scope (per-session) is a different document.
    r = await client.post(f"{P}/sessions", json={}, headers=auth_headers(acct))
    r3 = await _upload(client, acct, "c.txt", b"same bytes here", session_id=r.json()["id"])
    assert r3.json()["queued"] is True


@pytest.mark.parametrize(
    "name,data",
    [
        ("fake.pdf", b"this is not a pdf"),
        ("malware.exe", b"MZ\x90\x00binary"),
        ("empty.txt", b""),
        ("binary.txt", b"\x00\x01\x02\x03garbage"),
        ("noext", b"hello"),
    ],
)
async def test_upload_rejects_bad_files(client, queue, name, data):
    acct = await register_and_login(client)
    r = await _upload(client, acct, name, data, mime="application/pdf")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_error"
    assert queue.calls == []
    user_dir = Path(os.environ["UPLOAD_DIR"]) / acct["user"]["id"]
    assert not user_dir.exists() or not any(user_dir.iterdir())


async def test_client_content_type_is_ignored(client, queue):
    acct = await register_and_login(client)
    r = await _upload(client, acct, "real.md", b"# heading\n\ntext", mime="application/octet-stream")
    assert r.status_code == 202
    assert r.json()["document"]["mime_type"] == "text/markdown"


async def test_delete_document_removes_file(client, queue):
    acct = await register_and_login(client)
    r = await _upload(client, acct, "gone.txt", b"to be deleted")
    doc_id = r.json()["document"]["id"]
    user_dir = Path(os.environ["UPLOAD_DIR"]) / "documents" / acct["user"]["id"]
    assert any(user_dir.iterdir())
    r = await client.delete(f"{P}/documents/{doc_id}", headers=auth_headers(acct))
    assert r.status_code == 200
    assert not any(user_dir.iterdir())
    r = await client.get(f"{P}/documents/{doc_id}", headers=auth_headers(acct))
    assert r.status_code == 404


async def test_ingest_failure_is_recorded(client, db, queue):
    acct = await register_and_login(client)
    r = await _upload(client, acct, "lost.txt", b"content that will vanish")
    doc_id = r.json()["document"]["id"]
    user_dir = Path(os.environ["UPLOAD_DIR"]) / "documents" / acct["user"]["id"]
    for f in user_dir.iterdir():
        f.unlink()
    with pytest.raises(FileNotFoundError):
        await ingest_document(db, document_id=UUID(doc_id))
    r = await client.get(f"{P}/documents/{doc_id}", headers=auth_headers(acct))
    assert r.json()["status"] == "failed" and "missing" in r.json()["error"].lower()


async def test_documents_are_scoped_to_owner(client, queue):
    a = await register_and_login(client)
    b = await register_and_login(client)
    r = await _upload(client, a, "mine.txt", b"private")
    doc_id = r.json()["document"]["id"]
    r = await client.get(f"{P}/documents/{doc_id}", headers=auth_headers(b))
    assert r.status_code == 404
    r = await client.get(f"{P}/documents", headers=auth_headers(b))
    assert r.json()["total"] == 0
