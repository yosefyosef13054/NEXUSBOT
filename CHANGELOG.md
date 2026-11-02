# Changelog

All notable changes to NexusBot are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses semantic versioning.

## [1.4.0] — Phase 4: multimodal and mobile depth

Images, files and voice on chat messages, files back out of tools, an open tool ecosystem
through MCP, and the plumbing a mobile client needs (object storage, share links, push).

### Added
- **Object storage layer** (`app/storage/`): uploads, attachments and artifacts are addressed
  by key on local disk or any S3-compatible bucket (`STORAGE_BACKEND=s3`, MinIO / R2 via
  `S3_ENDPOINT_URL`), with presigned downloads; API and workers no longer need a shared volume.
- **Attachments** (`/sessions/{id}/attachments`): sniffed images (dimensions parsed without
  Pillow), audio (transcribed on upload when STT is configured) and documents (text
  extracted); bound to a message with `attachment_ids`. Images reach vision-capable models as
  image content (`VISION_FALLBACK_MODEL` or **422** otherwise); files and transcripts are given
  to the model inline — "chat with a file" without indexing.
- **Voice**: `POST /audio/transcriptions`, `POST /audio/speech` (streamed), `GET /audio/voices`,
  and `POST /sessions/{id}/messages/voice` (transcribe → background turn → spoken reply).
  `voice_reply` on any send streams `audio_chunk` / `audio_done` events (sentence-sized) and
  stores the clip as an audio attachment (`meta.audio`). OpenAI providers, an in-process fake
  for tests, or disabled (**503** `voice_unavailable`).
- **Artifacts**: tools can emit files through the `ToolContext` artifact sink; they are stored
  as `artifact` attachments, streamed as `artifact` events and listed in
  `assistant_message.meta.artifacts`. New `run_python` tool: model-written Python in a
  resource-limited subprocess (CPU, memory, file size, timeout), approval required, off by
  default (`CODE_EXECUTION_ENABLED`); files under `./output/` become artifacts.
- **MCP client** (`/mcp/servers`): register Model Context Protocol servers per organization
  (streamable HTTP, SSE, or stdio for server admins with `MCP_ALLOW_STDIO`); their tools are
  discovered (cached), listed by `GET /tools` as `mcp:<server>:<tool>`, usable per turn and on
  assistants, approval-gated per server, and executed through the agent like built-in tools.
  Secrets in `env` / `headers` are never returned.
- **Export and share links**: `GET /sessions/{id}/export?format=markdown|json`,
  `POST /sessions/{id}/share` (hashed, expiring, revocable tokens) and the public
  `GET /shared/{token}` (+ `/markdown`) transcript view.
- **Push notifications**: `/devices` token registry and a signed-webhook provider
  (`PUSH_PROVIDER=webhook`) that bridges to FCM / APNs / OneSignal; background and voice turns
  notify when the reply is ready.
- The fake model understands image content parts and exposes the last prompt for tests;
  migration `0005` adds `attachments`, `mcp_servers`, `devices`, `session_shares`.

### Changed
- The chat prompt's user turn is a message placeholder (`input_messages`) instead of a
  `{input}` string so multimodal content can be passed; `build_chat_prompt` callers pass
  `input_messages=[HumanMessage(...)]`.
- `Document.storage_path` holds a storage key (legacy absolute paths still resolve on the
  local backend); temporary uploads go to `UPLOAD_DIR/tmp`.
- `ToolContext` carries `org_id` and the artifact sink; `GET /tools` reports a `source`.

## [1.3.0] — Phase 3: product layer

Workspaces, personas, budgets, feedback, long-term memory, conversation branching and
streams that survive disconnects.

### Added
- **Organizations (workspaces) and roles.** Every user gets a personal org; shared orgs with
  `owner` / `admin` / `member` roles, member management by email, and per-org settings
  (monthly token / USD budgets, default model, allowed models). The active org is selected per
  request with `X-Org-Id` (WebSocket: `org_id` in the auth frame); sessions, documents,
  collections, assistants, memories and usage are scoped to it. Migration `0004` backfills a
  personal org for existing users and re-homes their data.
- **Usage and quotas.** `GET /usage/me` (month-to-date for user and org with remaining
  budget), `GET /orgs/{id}/usage?group_by=user|model|day` for org admins, and
  `GET /admin/usage` / `GET /admin/feedback` for server administrators. Exhausted budgets
  refuse every generation path with **429 `quota_exceeded`** (`details.resets_at`), checked
  against a short-lived cache that is kept current as turns are logged.
- **Assistants (personas)** (`/assistants`): system prompt, model, tools, collections,
  RAG / tool / memory defaults and an org-wide default; attach to sessions with
  `assistant_id`. Request fields override assistant defaults field by field. Edit / delete is
  limited to the creator or an org admin.
- **Feedback.** Thumbs up / down with an optional comment per assistant message
  (`POST/DELETE /sessions/{id}/messages/{mid}/feedback`), mirrored in `meta.feedback`.
- **Long-term memory** (`/memories`): facts, preferences and standing instructions per user,
  recalled by pgvector similarity into every turn (`meta.memories_used`) and extracted
  automatically after turns by a small model call; near-duplicate detection, per-user cap,
  user-level opt-out (`PATCH /auth/me {memory_enabled}`) and per-assistant switch.
- **Conversation branching.** Messages form a tree (`parent_id`, `siblings {index, count}`,
  `SessionRead.active_leaf_id`): editing a user message (`POST …/messages/{mid}/edit`, SSE)
  and regenerating (`…/regenerate`, new `…/regenerate/stream`) create siblings instead of
  overwriting; `GET …/messages/{mid}/siblings` and `POST /sessions/{id}/branch` switch
  branches; history returns the active branch.
- **Resumable streams and background turns.** Every streamed turn is written to a Redis
  Streams log with a sequence number (SSE `id:`); generations run detached from the
  connection. `GET /sessions/{id}/generations/{gid}/events?after=` (or `Last-Event-ID`)
  replays missed frames and follows live, `GET …/generations/active` finds the in-flight
  turn, `POST …/messages/background` (202) starts a turn without holding a connection, and
  `POST …/generations/{gid}/stop` ends one explicitly (works across workers).
- **Idempotency keys** (`Idempotency-Key` header) on message sends, streams, background
  turns and edits: retries return the original turn instead of creating a duplicate.
- **Eval harness** (`evals/`): JSONL datasets run through the real generation pipeline with
  deterministic graders (`contains`, `regex`, `tool_called`, `citations_min`, …) and an
  LLM judge, a CLI (`python -m evals.run --dataset … --fail-under 0.9 --out report.json`) and
  a shipped smoke dataset; covered by tests against the fake model.

### Changed
- **Disconnecting a stream no longer cancels the generation.** Previously a dropped SSE /
  WebSocket connection interrupted the model call and persisted a partial reply
  (`interrupt_reason = "disconnect"`); now the turn completes in the background and the
  client re-attaches. Stopping is explicit (`stop` frame / endpoint,
  `interrupt_reason = "stop"`).
- `regenerate` keeps the previous reply as a sibling branch instead of soft-deleting it.
- `UserRead` gains `personal_org_id` and `memory_enabled`; `SessionRead`, `DocumentRead` and
  `CollectionRead` gain `org_id`; `MessageRead` gains `parent_id` and `siblings`;
  `assistant_message.meta.memories_used` is always present.
- Full-text chat search is scoped to the active org.

## [1.2.0] — Phase 2: AI core

Multi-provider models with real routing, a LangGraph agent with human-in-the-loop
approvals, and a second-generation retrieval pipeline.

### Added
- **Model registry and routing.** `app/ai/providers/` holds a catalog of OpenAI,
  Anthropic (Claude Opus 5.5, Sonnet 5.5, Haiku 5.5, Fable 5.1), Google Gemini, Azure OpenAI
  and Ollama models with context windows, prices and capability flags. Providers are enabled
  by configuring their credentials; `GET /models` lists what the server can serve. A turn's
  model resolves request → session → user default (`PATCH /auth/me {default_model}`) →
  server default, all validated. `MODEL_FALLBACKS` retries another model when the primary
  provider fails before any token was streamed. Claude 4.6+ models omit sampling parameters
  the API rejects.
- **LangGraph agent** (`app/ai/agents/`) replacing the legacy `AgentExecutor`: tool calls
  run with timeouts and a per-turn step budget, every tool run is persisted as a `role=tool`
  message, and tools flagged `requires_approval` (the new `fetch_url` tool, plus
  `TOOLS_REQUIRING_APPROVAL`) pause the turn with an `approval_required` event. The user
  resolves it with `POST /sessions/{id}/approvals/{approval_id}` (SSE continuation) or the
  WebSocket `approval` frame; graph state lives in a Postgres checkpointer. `GET /tools`
  describes available tools; `GET /sessions/{id}/approvals` lists pending ones.
- **RAG 2.0.** Hybrid retrieval (pgvector cosine + Postgres full-text, reciprocal rank
  fusion), optional LLM reranking and conversation-aware query rewriting, heading- and
  page-aware chunking with contextual headers, structured citations (chunk id, page,
  section, character offsets, match type), chunk viewer endpoints, URL ingestion
  (`POST /documents/from-url`, SSRF-guarded), and **collections** (knowledge bases) with
  per-turn / per-session scoping.
- Full-text chat search with ranked, highlighted results.

### Fixed
- HTML documents were loaded without separators between elements (words glued
  together), defeating keyword search; a dedicated HTML loader strips scripts and keeps
  the page title.
- `PATCH /sessions/{id}` ignored `model`.

### Changed
- `SendMessageResponse`/`RegenerateResponse` include `tool_messages` and `approval`;
  message `meta` carries `provider`, `status` and `approval`.
- Titles and summaries are generated only once a turn completes (not while it waits for
  an approval).
- Migration `0003_phase2_ai_core` (collections, approvals, generated `tsvector` columns,
  `users.default_model`, LangGraph checkpoint tables).

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
