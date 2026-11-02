"""RAG 2.0: structure-aware chunking, hybrid retrieval, collections, URL ingestion, chunk
viewer endpoints and full-text chat search."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

import pytest
from langchain_core.documents import Document as LCDocument

from app.ai.rag.pipeline import ingest_document
from app.ai.rag.retriever import reciprocal_rank_fusion, retrieve_chunks
from app.ai.rag.splitter import split_documents, split_sections
from app.config import settings
from app.utils.web import UnsafeUrlError, validate_public_url
from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"

MARKDOWN = """Intro paragraph before any heading.

# Scope

## Goals
Zebrafish husbandry guidelines for the lab.

## Non-goals
Nothing about hamsters.

# Appendix
Reference tables.
"""


# ----------------------------------------------------------------- unit: splitter / fusion


def test_split_sections_builds_breadcrumbs_and_offsets():
    sections = split_sections(MARKDOWN)
    paths = [s.path for s in sections]
    assert paths[0] is None  # preamble
    assert "Scope > Goals" in paths and "Scope > Non-goals" in paths and "Appendix" in paths
    goals = next(s for s in sections if s.path == "Scope > Goals")
    assert MARKDOWN[goals.offset : goals.offset + len(goals.text)] == goals.text
    assert "Zebrafish" in goals.text


def test_split_documents_adds_context_headers_and_pages(monkeypatch):
    monkeypatch.setattr(settings, "RAG_CONTEXT_HEADERS", True)
    docs = [LCDocument(page_content=MARKDOWN, metadata={"page": 2, "source": "/srv/x/spec.md"})]
    chunks = split_documents(docs, document_name="spec.md")
    goals = next(c for c in chunks if "Zebrafish" in c.page_content)
    assert goals.metadata["section"] == "Scope > Goals"
    assert goals.metadata["page"] == 3  # loaders are 0-based
    assert goals.metadata["embed_text"].startswith("spec.md > Scope > Goals\n")
    assert MARKDOWN[goals.metadata["start_index"] : goals.metadata["end_index"]].strip() == goals.page_content


def test_reciprocal_rank_fusion_prefers_items_in_both_lists():
    a, b, c = UUID(int=1), UUID(int=2), UUID(int=3)
    fused = reciprocal_rank_fusion([[a, b], [c, a]])
    assert fused[a] > fused[c] > fused[b]


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "http://user:pw@example.com/"])
async def test_validate_public_url_rejects_bad_schemes(url, monkeypatch):
    monkeypatch.setattr(settings, "WEB_FETCH_ALLOW_PRIVATE", False)
    with pytest.raises(UnsafeUrlError):
        await validate_public_url(url)


async def test_validate_public_url_blocks_private_hosts(monkeypatch):
    monkeypatch.setattr(settings, "WEB_FETCH_ALLOW_PRIVATE", False)
    for url in ("http://127.0.0.1:8000/", "http://localhost/", "http://169.254.169.254/latest/meta-data"):
        with pytest.raises(UnsafeUrlError):
            await validate_public_url(url)


# ---------------------------------------------------------------- integration helpers


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


async def _upload(client, acct, name: str, data: bytes, **form):
    return await client.post(
        f"{P}/documents",
        files={"file": (name, data, "text/plain")},
        data={k: str(v) for k, v in form.items() if v is not None},
        headers=auth_headers(acct),
    )


async def _ingest(db, document_id: str) -> None:
    await ingest_document(db, document_id=UUID(document_id))


async def _session(client, acct, **body) -> str:
    r = await client.post(f"{P}/sessions", json=body, headers=auth_headers(acct))
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------- hybrid retrieval


async def test_keyword_hit_without_vector_similarity(client, db, queue):
    acct = await register_and_login(client)
    r = await _upload(client, acct, "lab.md", MARKDOWN.encode())
    doc_id = r.json()["document"]["id"]
    await _ingest(db, doc_id)

    # Fake embeddings are random per text, so a paraphrased query has ~0 cosine similarity;
    # only the full-text index can find "zebrafish".
    result = await retrieve_chunks(db, user_id=UUID(acct["user"]["id"]), query="zebrafish guidelines")
    assert result.embedded and result.chunks
    hit = result.chunks[0]
    assert "Zebrafish" in hit.content
    assert hit.text_hit and hit.match in ("keyword", "hybrid")
    citation = hit.to_citation()
    assert citation["section"] == "Scope > Goals" and citation["chunk_id"] == str(hit.chunk_id)
    assert citation["start_index"] is not None and citation["end_index"] > citation["start_index"]

    # Keyword search can be switched off.
    settings.RAG_HYBRID_SEARCH = False
    try:
        result = await retrieve_chunks(db, user_id=UUID(acct["user"]["id"]), query="zebrafish guidelines")
        assert result.chunks == []
    finally:
        settings.RAG_HYBRID_SEARCH = True


async def test_structured_citations_flow_to_chat(client, db, queue):
    acct = await register_and_login(client)
    text = "The quarterly revenue target is 12 million euros."
    r = await _upload(client, acct, "targets.txt", text.encode())
    await _ingest(db, r.json()["document"]["id"])
    sid = await _session(client, acct)
    r = await client.post(
        f"{P}/sessions/{sid}/messages", json={"content": text, "use_rag": True}, headers=auth_headers(acct)
    )
    assert r.status_code == 200, r.text
    c = r.json()["citations"][0]
    assert c["document_name"] == "targets.txt" and c["chunk_id"] and c["score"] > 0.9
    assert c["start_index"] == 0 and c["end_index"] == len(text)
    assert r.json()["assistant_message"]["meta"]["citations"][0]["chunk_id"] == c["chunk_id"]


# ---------------------------------------------------------------- collections


async def test_collections_scope_retrieval(client, db, queue):
    acct = await register_and_login(client)
    r = await client.post(
        f"{P}/collections", json={"name": "Finance", "description": "money"}, headers=auth_headers(acct)
    )
    assert r.status_code == 201, r.text
    finance = r.json()["id"]
    r = await client.post(f"{P}/collections", json={"name": "Empty"}, headers=auth_headers(acct))
    empty = r.json()["id"]

    finance_text = "Finance memo: the budget for marketing is 40000 dollars."
    other_text = "Kitchen rota: please clean the espresso machine."
    r = await _upload(client, acct, "memo.txt", finance_text.encode(), collection_id=finance)
    assert r.status_code == 202, r.text
    assert r.json()["document"]["collection_id"] == finance
    await _ingest(db, r.json()["document"]["id"])
    r = await _upload(client, acct, "rota.txt", other_text.encode())
    await _ingest(db, r.json()["document"]["id"])

    r = await client.get(f"{P}/collections/{finance}", headers=auth_headers(acct))
    assert r.json()["document_count"] == 1
    r = await client.get(f"{P}/documents", params={"collection_id": finance}, headers=auth_headers(acct))
    assert r.json()["total"] == 1

    sid = await _session(client, acct)
    # Scoped to the finance collection: the rota must not be retrievable even with an exact query.
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": other_text, "use_rag": True, "collection_ids": [finance]},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200
    assert all(c["document_name"] != "rota.txt" for c in r.json()["citations"])
    # Empty collection → retrieval skipped entirely.
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": finance_text, "use_rag": True, "collection_ids": [empty]},
        headers=auth_headers(acct),
    )
    assert r.json()["citations"] == [] and r.json()["usage"]["embedding_tokens"] == 0
    # Session default scope via meta.
    r = await client.patch(
        f"{P}/sessions/{sid}", json={"meta": {"collection_ids": [finance]}}, headers=auth_headers(acct)
    )
    assert r.status_code == 200 and r.json()["meta"]["collection_ids"] == [finance]
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": finance_text, "use_rag": True},
        headers=auth_headers(acct),
    )
    assert r.json()["citations"][0]["document_name"] == "memo.txt"
    assert r.json()["citations"][0]["collection_id"] == finance
    assert r.json()["assistant_message"]["meta"]["generation"]["collection_ids"] == [finance]

    # Unknown collection ids are rejected.
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "x", "collection_ids": [str(UUID(int=9))]},
        headers=auth_headers(acct),
    )
    assert r.status_code == 422

    # Move a document, then delete the collection without deleting documents.
    r = await client.get(f"{P}/documents", headers=auth_headers(acct))
    rota_id = next(d["id"] for d in r.json()["items"] if d["name"] == "rota.txt")
    r = await client.patch(
        f"{P}/documents/{rota_id}", json={"collection_id": finance}, headers=auth_headers(acct)
    )
    assert r.status_code == 200 and r.json()["collection_id"] == finance
    r = await client.patch(
        f"{P}/documents/{rota_id}", json={"collection_id": None}, headers=auth_headers(acct)
    )
    assert r.json()["collection_id"] is None
    r = await client.delete(f"{P}/collections/{finance}", headers=auth_headers(acct))
    assert r.status_code == 200
    r = await client.get(f"{P}/documents", headers=auth_headers(acct))
    assert all(d["collection_id"] is None for d in r.json()["items"]) and r.json()["total"] == 2
    r = await client.get(f"{P}/collections/{finance}", headers=auth_headers(acct))
    assert r.status_code == 404


async def test_collections_are_private(client):
    a = await register_and_login(client)
    b = await register_and_login(client)
    r = await client.post(f"{P}/collections", json={"name": "Mine"}, headers=auth_headers(a))
    cid = r.json()["id"]
    r = await client.get(f"{P}/collections/{cid}", headers=auth_headers(b))
    assert r.status_code == 404
    r = await client.post(f"{P}/sessions", json={"meta": {"collection_ids": [cid]}}, headers=auth_headers(b))
    assert r.status_code == 422


# ---------------------------------------------------------------- chunk endpoints


async def test_chunk_endpoints(client, db, queue):
    acct = await register_and_login(client)
    other = await register_and_login(client)
    r = await _upload(client, acct, "lab.md", MARKDOWN.encode())
    doc_id = r.json()["document"]["id"]
    await _ingest(db, doc_id)
    r = await client.get(f"{P}/documents/{doc_id}/chunks", headers=auth_headers(acct))
    assert r.status_code == 200
    items = r.json()["items"]
    assert r.json()["total"] == len(items) >= 3
    assert [c["chunk_index"] for c in items] == sorted(c["chunk_index"] for c in items)
    goals = next(c for c in items if "Zebrafish" in c["content"])
    assert goals["section"] == "Scope > Goals"
    r = await client.get(f"{P}/documents/{doc_id}/chunks/{goals['id']}", headers=auth_headers(acct))
    assert r.status_code == 200 and r.json()["content"] == goals["content"]
    r = await client.get(f"{P}/documents/{doc_id}/chunks/{goals['id']}", headers=auth_headers(other))
    assert r.status_code == 404


# ---------------------------------------------------------------- URL ingestion


class _Handler(BaseHTTPRequestHandler):
    page = b"<html><head><title>Zebra Lab Notes</title></head><body><h1>Hi</h1><p>Zebrafish love warm water.</p><script>x()</script></body></html>"

    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        if self.path == "/big":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(10 * 1024 * 1024))
            self.end_headers()
            return
        if self.path == "/binary":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(b"\x00\x01")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(self.page)

    def log_message(self, *_):  # silence
        return


@pytest.fixture(scope="module")
def web_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


async def test_ingest_from_url(client, db, queue, web_server):
    acct = await register_and_login(client)
    r = await client.post(
        f"{P}/documents/from-url", json={"url": f"{web_server}/redirect"}, headers=auth_headers(acct)
    )
    assert r.status_code == 202, r.text
    doc = r.json()["document"]
    assert doc["name"] == "Zebra Lab Notes" and doc["mime_type"] == "text/html"
    assert doc["source_url"] == f"{web_server}/page"
    assert queue.calls == [doc["id"]]
    await _ingest(db, doc["id"])
    r = await client.get(f"{P}/documents/{doc['id']}", headers=auth_headers(acct))
    assert r.json()["status"] == "ready"
    result = await retrieve_chunks(db, user_id=UUID(acct["user"]["id"]), query="zebrafish water")
    assert result.chunks and "warm water" in result.chunks[0].content
    assert "x()" not in result.chunks[0].content  # scripts stripped by the HTML loader

    # Same content again → dedup.
    r = await client.post(
        f"{P}/documents/from-url", json={"url": f"{web_server}/page"}, headers=auth_headers(acct)
    )
    assert r.status_code == 202 and r.json()["queued"] is False


async def test_ingest_from_url_rejections(client, queue, web_server, monkeypatch):
    acct = await register_and_login(client)
    for path in ("/big", "/binary"):
        r = await client.post(
            f"{P}/documents/from-url", json={"url": f"{web_server}{path}"}, headers=auth_headers(acct)
        )
        assert r.status_code == 422, r.text
    r = await client.post(f"{P}/documents/from-url", json={"url": "not a url"}, headers=auth_headers(acct))
    assert r.status_code == 422
    monkeypatch.setattr(settings, "WEB_FETCH_ALLOW_PRIVATE", False)
    r = await client.post(
        f"{P}/documents/from-url", json={"url": f"{web_server}/page"}, headers=auth_headers(acct)
    )
    assert r.status_code == 422 and "private" in r.json()["error"]["message"].lower()
    assert queue.calls == []


# ---------------------------------------------------------------- search


async def test_search_uses_full_text_with_highlights(client):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "The quick brown foxes jumped over the lazy dog", "use_rag": False},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200
    r = await client.post(f"{P}/search", json={"query": "fox jumping"}, headers=auth_headers(acct))
    assert r.status_code == 200
    results = r.json()
    assert results and results[0]["rank"] > 0
    assert "<mark>" in results[0]["highlight"]
    # Substring fallback for tokens the parser drops.
    r = await client.post(f"{P}/search", json={"query": "zz"}, headers=auth_headers(acct))
    assert r.status_code == 200 and r.json() == []
    r = await client.post(f"{P}/search", json={"query": "lazy do"}, headers=auth_headers(acct))
    assert r.status_code == 200 and r.json()
