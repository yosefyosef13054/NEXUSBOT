"""Pure unit tests: no DB, no Redis, no network."""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.ai.callbacks import StreamCollector, extract_usage
from app.ai.tools.context import ToolContext
from app.ai.tools.database_query import UserSessionsInput, UserSessionsTool
from app.config import Settings, settings
from app.core.exceptions import UnauthorizedError
from app.core.middleware import RequestContextMiddleware, SelectiveGZipMiddleware
from app.core.rate_limit import rate_limit_dependency
from app.core.security import REFRESH, create_refresh_token, decode_token
from app.utils import tokens as tokens_mod
from app.utils.files import safe_filename, sniff_mime

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------- settings ----


def _settings(**overrides) -> Settings:
    base = dict(
        DATABASE_URL="postgresql+asyncpg://x:y@localhost/db",
        SYNC_DATABASE_URL="postgresql://x:y@localhost/db",
        _env_file=None,
    )
    base.update(overrides)
    return Settings(**base)


def test_production_refuses_default_jwt_secret():
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        _settings(APP_ENV="production", JWT_SECRET_KEY="change-me")
    with pytest.raises(ValueError):
        _settings(APP_ENV="production", JWT_SECRET_KEY="short")


def test_production_forces_debug_off_with_strong_secret():
    s = _settings(APP_ENV="production", JWT_SECRET_KEY="x" * 48, APP_DEBUG=True)
    assert s.APP_DEBUG is False
    assert s.cors_allow_credentials is False  # origins default to *


def test_development_tolerates_weak_secret():
    s = _settings(APP_ENV="development", JWT_SECRET_KEY="change-me")
    assert s.JWT_SECRET_KEY == "change-me"


def test_csv_and_json_list_parsing():
    s = _settings(ALLOWED_ORIGINS="https://a.com, https://b.com", MODEL_PRICING='{"m": [0.1, 0.2]}')
    assert s.ALLOWED_ORIGINS == ["https://a.com", "https://b.com"]
    assert s.cors_allow_credentials is True
    assert s.MODEL_PRICING == {"m": [0.1, 0.2]}


# ------------------------------------------------------------------ tokens ----


def test_count_tokens_never_zero_for_text():
    assert tokens_mod.count_tokens("a") >= 1
    assert tokens_mod.count_tokens("x" * 400) >= 50
    assert tokens_mod.count_message_tokens(["hi", "there"]) > tokens_mod.count_tokens("hithere")


def test_pricing_prefix_match_and_override(monkeypatch):
    tokens_mod.pricing_table.cache_clear()
    assert tokens_mod.lookup_pricing("gpt-4o-mini-2024-07-18") == tokens_mod.lookup_pricing("gpt-4o-mini")
    assert tokens_mod.lookup_pricing("totally-unknown") is None
    assert tokens_mod.estimate_cost("totally-unknown", prompt_tokens=1000) == 0.0
    monkeypatch.setattr(settings, "MODEL_PRICING", {"my-model": [1.0, 2.0]})
    tokens_mod.pricing_table.cache_clear()
    assert tokens_mod.estimate_cost("my-model", prompt_tokens=1000, completion_tokens=500) == pytest.approx(
        2.0
    )
    tokens_mod.pricing_table.cache_clear()


# -------------------------------------------------------------------- files ----


@pytest.mark.parametrize(
    "head,ext,ok",
    [
        (b"%PDF-1.7 ...", ".pdf", True),
        (b"hello world", ".pdf", False),
        (b"PK\x03\x04zip", ".docx", True),
        (b"MZ\x90\x00", ".docx", False),
        (b"# Title\n\nbody", ".md", True),
        (b"\x00\x01\x02binary", ".txt", False),
        (b'{"a": 1}', ".json", True),
        (b"<html><body>hi</body></html>", ".html", True),
        (b"anything", ".exe", False),
    ],
)
def test_sniff_mime(head, ext, ok):
    assert (sniff_mime(head, ext) is not None) is ok


def test_safe_filename_strips_paths():
    assert "/" not in safe_filename("../../etc/passwd")
    assert safe_filename("résumé final.PDF").endswith(".PDF")


# ----------------------------------------------------------------- security ----


def test_refresh_token_carries_jti_and_family():
    uid = uuid4()
    token, expires_at = create_refresh_token(uid, jti="abc", family_id="fam1")
    payload = decode_token(token, expected_type=REFRESH)
    assert payload["jti"] == "abc" and payload["fam"] == "fam1" and payload["sub"] == str(uid)
    assert expires_at.tzinfo is not None
    with pytest.raises(UnauthorizedError):
        decode_token(token, expected_type="access")
    with pytest.raises(UnauthorizedError):
        decode_token(token + "x", expected_type=REFRESH)


# -------------------------------------------------------------------- tools ----


def test_db_tool_cannot_take_user_id_from_model():
    """The IDOR fix: identity comes from ToolContext, not from the model's arguments."""
    assert "user_id" not in UserSessionsInput.model_fields
    ctx = ToolContext(user_id=uuid4(), session_id=uuid4())
    tool = UserSessionsTool(ctx=ctx)
    assert tool.ctx.user_id == ctx.user_id
    assert "limit" in tool.args_schema.model_fields


def test_rate_limit_dependency_resolves_user_first():
    dep = rate_limit_dependency("chat")
    params = inspect.signature(dep).parameters
    assert "user" in params, "limiter must depend on current_user so it keys on the user id"


# ---------------------------------------------------------------- callbacks ----


def _llm_result(**kw):
    msg = SimpleNamespace(
        usage_metadata=kw.get("usage_metadata"), response_metadata=kw.get("response_metadata", {})
    )
    return SimpleNamespace(generations=[[SimpleNamespace(message=msg)]], llm_output=kw.get("llm_output"))


def test_extract_usage_prefers_usage_metadata():
    r = _llm_result(usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})
    assert extract_usage(r) == (12, 3)
    r = _llm_result(response_metadata={"token_usage": {"prompt_tokens": 7, "completion_tokens": 2}})
    assert extract_usage(r) == (7, 2)
    r = _llm_result(response_metadata={"usage": {"input_tokens": 5, "output_tokens": 1}})
    assert extract_usage(r) == (5, 1)
    assert extract_usage(_llm_result()) == (0, 0)
    assert extract_usage(object()) == (0, 0)


async def test_stream_collector_accumulates_usage_across_calls():
    c = StreamCollector()
    await c.on_llm_new_token("")  # ignored
    await c.on_llm_new_token("hi")
    await c.on_llm_end(_llm_result(usage_metadata={"input_tokens": 10, "output_tokens": 2}))
    await c.on_llm_end(_llm_result(usage_metadata={"input_tokens": 5, "output_tokens": 1}))
    assert (c.prompt_tokens, c.completion_tokens, c.llm_calls) == (15, 3, 2)
    assert c.saw_token
    first = await c.queue.get()
    assert first == {"type": "token", "data": "hi"}
    usage = await c.queue.get()
    assert usage["data"]["total_tokens"] == 12


async def test_stream_collector_tool_end_carries_name():
    c = StreamCollector()
    rid = uuid4()
    await c.on_tool_start({"name": "calculator"}, "2+2", run_id=rid, inputs={"expression": "2+2"})
    await c.on_tool_end("4", run_id=rid)
    start = await c.queue.get()
    end = await c.queue.get()
    assert start["data"]["phase"] == "start" and start["data"]["tool"] == "calculator"
    assert end["data"]["phase"] == "end" and end["data"]["tool"] == "calculator"
    assert end["data"]["input"] == {"expression": "2+2"} and end["data"]["output"] == "4"


# --------------------------------------------------------------- middleware ----


def _mini_app() -> Starlette:
    async def big(_request):
        return JSONResponse({"data": "x" * 5000})

    async def sse(_request):
        async def gen():
            for i in range(3):
                yield f"data: {json.dumps({'i': i})}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/big", big), Route("/sse", sse)])
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=100)
    app.add_middleware(RequestContextMiddleware)
    return app


async def test_gzip_skips_event_streams_but_compresses_json():
    app = _mini_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/big", headers={"Accept-Encoding": "gzip"})
        assert r.headers.get("content-encoding") == "gzip"
        r = await c.get("/sse", headers={"Accept-Encoding": "gzip", "Accept": "text/event-stream"})
        assert "content-encoding" not in r.headers
        assert r.text.count("data:") == 3


async def test_request_id_header_generated_and_echoed():
    app = _mini_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/big")
        assert len(r.headers["x-request-id"]) == 32
        assert r.headers["x-process-time-ms"].isdigit()
        r = await c.get("/big", headers={"X-Request-Id": "trace-123"})
        assert r.headers["x-request-id"] == "trace-123"
