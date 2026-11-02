"""Test configuration.

Environment is pinned *before* `app` is imported so `app.config.settings` sees it:
fake AI provider, inline background tasks, generous rate limits, and the test database.
Integration fixtures need Postgres (pgvector) + Redis reachable at the URLs below
(docker compose, the CI services, or local servers).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import AsyncIterator

_TEST_ENV = {
    "APP_ENV": "test",
    "AI_FAKE_MODE": "true",
    "BACKGROUND_TASKS_INLINE": "true",
    "JWT_SECRET_KEY": "test-only-secret-key-that-is-long-enough-0123456789",
    "JWT_ACCESS_TOKEN_EXPIRE_MINUTES": "30",
    "DATABASE_URL": "postgresql+asyncpg://chatbot:chatbot@127.0.0.1:5432/chatbot_test",
    "SYNC_DATABASE_URL": "postgresql://chatbot:chatbot@127.0.0.1:5432/chatbot_test",
    "REDIS_URL": "redis://127.0.0.1:6379/0",
    "CELERY_BROKER_URL": "redis://127.0.0.1:6379/1",
    "CELERY_RESULT_BACKEND": "redis://127.0.0.1:6379/2",
    "RATE_LIMIT_PER_MINUTE": "100000",
    "AUTH_RATE_LIMIT_PER_MINUTE": "100000",
    "UPLOAD_DIR": os.path.join(tempfile.gettempdir(), "nexusbot-test-uploads"),
    "LOG_LEVEL": "WARNING",
    "EMBEDDING_BATCH_SIZE": "4",
}
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import settings  # noqa: E402

INTEGRATION_TABLES = (
    "tool_calls",
    "ai_usage_logs",
    "document_chunks",
    "documents",
    "chat_messages",
    "chat_sessions",
    "refresh_tokens",
    "api_keys",
    "users",
)


def _infra_available() -> bool:
    import socket

    for host, port in (("127.0.0.1", 5432), ("127.0.0.1", 6379)):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                pass
        except OSError:
            return False
    return True


INFRA = _infra_available()
requires_infra = pytest.mark.skipif(not INFRA, reason="Postgres + Redis not reachable")


def _migrate() -> None:
    """Recreate the schema through Alembic so migrations are exercised on every run."""
    from sqlalchemy import create_engine

    from alembic import command
    from alembic.config import Config

    engine = create_engine(settings.SYNC_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    if not INFRA:
        return
    _migrate()
    Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables() -> AsyncIterator[None]:
    """Truncate everything between tests (cheaper than re-migrating)."""
    yield
    if not INFRA:
        return
    from app.db.session import engine

    async with engine.begin() as conn:
        # A leaked "idle in transaction" connection (e.g. from a streaming endpoint) would
        # block TRUNCATE forever; fail loudly instead so the leak is visible.
        await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        await conn.execute(text(f"TRUNCATE {', '.join(INTEGRATION_TABLES)} CASCADE"))


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _flush_redis() -> AsyncIterator[None]:
    if INFRA:
        from app.cache.redis_client import get_redis

        await get_redis().flushdb()
    yield
    if INFRA:
        from app.cache.redis_client import close_redis
        from app.db.session import engine

        await close_redis()
        await engine.dispose()


@pytest.fixture(autouse=True)
def _clear_fake_llm() -> None:
    from app.ai.fake import clear_scripted_responses

    clear_scripted_responses()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        yield session


_user_counter = 0


async def register_and_login(client: AsyncClient, *, password: str = "password123") -> dict:
    """Create a fresh user and return {email, password, access_token, refresh_token, user}."""
    global _user_counter
    _user_counter += 1
    email = f"user{_user_counter}-{os.getpid()}@example.com"
    r = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": password, "full_name": "Test User"}
    )
    assert r.status_code == 201, r.text
    user = r.json()
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    tokens = r.json()
    return {
        "email": email,
        "password": password,
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "user": user,
    }


def auth_headers(account: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {account['access_token']}"}


@pytest_asyncio.fixture
async def account(client: AsyncClient) -> dict:
    return await register_and_login(client)


@pytest.fixture
def api_prefix() -> str:
    return settings.API_V1_PREFIX


def run(coro):
    """Run a coroutine from a sync test helper."""
    return asyncio.get_event_loop().run_until_complete(coro)
