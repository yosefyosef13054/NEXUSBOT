# Changelog

All notable changes to NexusBot are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses semantic versioning.

## [1.1.0] — Phase 1: foundation

Makes every feature the README advertises actually work, and hardens the core before the
AI-layer upgrades of Phase 2.

### Security
- **Tools are bound to the calling user.** `ToolContext` (user, session, request id) is injected
  by the orchestrator; tools can no longer take a `user_id` from the model's arguments
  (`list_user_sessions` previously let a prompt enumerate any user's chats).
- **Refresh-token rotation and revocation.** Every refresh issues a new pair and retires the old
  one (`refresh_tokens` table keyed by `jti`). Reuse outside a short grace window revokes the
  whole token family. New endpoints: `POST /auth/logout`, `POST /auth/logout-all`.
- **Account lifecycle:** `DELETE /auth/me` (password-confirmed account deletion, required for
  app-store review), `PATCH /auth/me`, `POST /auth/change-password` (revokes other devices).
- **WebSocket auth moves to the first frame** (`{"type":"auth","token":…}`) so JWTs no longer
  land in access logs; query-string credentials still work but are logged as deprecated.
- Production refuses to boot with the default/short `JWT_SECRET_KEY`; `APP_DEBUG` is forced
  off in production; CORS credentials are disabled when origins are `*`.
- Uploads are validated by **magic bytes**, not the client `Content-Type`; extensions are
  allow-listed; empty files are rejected.
- `register`/`login`/`refresh` are rate limited per IP; chat and upload endpoints per **user**
  (the previous limiter ran before authentication and only ever saw the client IP).

### Fixed
- Tool-enabled turns crashed: the agent prompt lacked the `agent_scratchpad` placeholder that
  `create_openai_tools_agent` requires.
- Streamed generations reported no token usage (`stream_usage` was off) and blocking turns
  logged only the user message as prompt tokens; cost accounting was wrong by a large factor.
  Usage now comes from the provider (prompt + completion across every model call of an agent
  run), with local estimates as fallback; embedding usage is logged during ingestion.
- Stopping or disconnecting mid-stream kept the model call running and lost the reply.
  Generations are now cancelled, the partial text is persisted with `status=interrupted`,
  and the WebSocket gains a `stop` frame.
- Streaming endpoints leaked database connections: FastAPI tears down `Depends(get_db)` before
  a streaming body runs, so a client disconnect left a transaction open ("idle in transaction").
  Streams now own their session and close it from a detached task.
- `regenerate` could not find the user turn after a blocking send (both messages shared the
  transaction timestamp); messages now carry explicit timestamps, and regenerate replays the
  original turn's RAG/tool settings instead of hard-coded ones.
- Tool results streamed without the tool name on the `end` frame.
- The Celery worker never consumed the `ingest` queue the task was routed to.
- `.docx` ingestion failed (`docx2txt` was missing); `.md` no longer needs `unstructured`.
- GZip middleware buffered Server-Sent Events for clients that accept gzip (every Dart
  HttpClient); event streams are now excluded from compression.
- `X-Request-Id` was advertised in CORS but never set.
- `.env.example` was git-ignored, so the documented quickstart failed on a fresh clone.
- `tiktoken` failures (offline environments) no longer break requests; token counting falls
  back to an estimate.

### Added
- Title generation and summary compaction run **after** the final frame is sent
  (`app/core/background.py`), removing two model calls from the hot path.
- `GET /metrics` (Prometheus): HTTP, model, token, cost, tool, retrieval, ingestion and
  interruption metrics. `X-Request-Id` / `X-Process-Time-Ms` on every response; request ids
  in error envelopes and structured logs.
- `AI_FAKE_MODE`: deterministic in-process model + embeddings so the whole stack (and the test
  suite) runs without provider keys.
- Upload de-duplication by SHA-256 (`documents.content_hash`), batched embeddings with
  `meta.progress` percentages, `MODEL_PRICING` override, `GENERATION_TIMEOUT_SECONDS`.
- Retrieval is skipped (no embedding call) for users without ready documents.
- Worker database access uses `NullPool`, fixing cross-event-loop connection reuse.
- Test-suite: 76 tests across unit, integration (Postgres + pgvector + Redis) and live-socket
  SSE/WebSocket scenarios; GitHub Actions CI (ruff, mypy, pytest with services, Docker build);
  `docker-compose.prod.yml`; migration `0002_phase1_foundation`.

### Changed
- The Flutter client now lives only in
  [`nexusbot-app`](https://github.com/yosefyosef13054/NEXUSBOT-APP); the embedded copy was
  removed from this repository.
- Dropped unused dependencies (`unstructured`, `slowapi`, `python-jose`); removed the idle
  `beat` service from `docker-compose.yml`.
- Access tokens default to 30 minutes (was 60); refresh tokens rotate.

## [1.0.0]
Initial release: FastAPI + LangChain backend with sessions, SSE/WebSocket streaming, RAG over
pgvector, tool calling, rolling-summary memory, JWT + API keys and Celery ingestion.
