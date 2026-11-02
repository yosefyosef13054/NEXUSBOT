<div align="center">

<img src="assets/banner.svg" alt="NexusBot" width="100%" />

<br/>

[![CI](https://img.shields.io/github/actions/workflow/status/yosefyosef13054/NEXUSBOT/ci.yml?branch=main&style=for-the-badge&label=CI&labelColor=0A0A0A&color=EF4444)](https://github.com/yosefyosef13054/NEXUSBOT/actions)
[![Python](https://img.shields.io/badge/PYTHON-3.12-0A0A0A?style=for-the-badge&logo=python&logoColor=EF4444&labelColor=0A0A0A)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FASTAPI-0.115-0A0A0A?style=for-the-badge&logo=fastapi&logoColor=EF4444&labelColor=0A0A0A)](https://fastapi.tiangolo.com/)
[![LangChain](https://img.shields.io/badge/LANGCHAIN-0.3-0A0A0A?style=for-the-badge&logo=langchain&logoColor=EF4444&labelColor=0A0A0A)](https://www.langchain.com/)
[![Postgres](https://img.shields.io/badge/POSTGRES-16-0A0A0A?style=for-the-badge&logo=postgresql&logoColor=EF4444&labelColor=0A0A0A)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/REDIS-7-0A0A0A?style=for-the-badge&logo=redis&logoColor=EF4444&labelColor=0A0A0A)](https://redis.io/)
[![License: MIT](https://img.shields.io/badge/LICENSE-MIT-EF4444?style=for-the-badge&labelColor=0A0A0A)](LICENSE)

<br/>

# NexusBot

**The production-grade AI chatbot engine for any Flutter app.**
**Built with FastAPI + LangChain. API-first, streaming-first, modular by design.**

[Quickstart](#-quickstart) · [Features](#-features) · [Architecture](#-architecture) · [API](#-api-reference) · [Flutter App](https://github.com/yosefyosef13054/NEXUSBOT-APP) · [Changelog](CHANGELOG.md)

</div>

---

## ✨ Why NexusBot

Most "AI chatbot" repos are demos: one file, one model, no persistence, no streaming, no auth. The moment you try to ship one to a real Flutter app — with multiple users, document chat, real-time tokens, tool calls, and cost tracking — you have to rebuild it from scratch.

**NexusBot is the rebuild.** Drop it behind any Flutter app and you immediately get:

- 💬 Multi-session chat with persistent history & background-generated titles
- ⚡ Real-time token streaming over **SSE** *and* **WebSocket**, with stop/cancel that keeps the partial reply
- 🔀 **Multi-provider models** — OpenAI, Anthropic Claude, Google Gemini, Azure OpenAI, Ollama — with per-session / per-turn / per-user routing and fallbacks
- 🤖 **LangGraph agent** with human-in-the-loop approvals for side-effecting tools, step budgets and persisted tool messages
- 📚 **Hybrid RAG** (vector + full-text) over PDFs / DOCX / TXT / MD / HTML / URLs with structured citations and collections
- 🧠 Conversation memory with automatic rolling-summary compaction (off the hot path)
- 🔐 JWT with rotating refresh tokens, revocable API keys, per-user rate limiting, account deletion
- 📊 Provider-reported usage and cost per call, Prometheus `/metrics`, request ids everywhere
- 🧪 Runs end-to-end without any provider key (`AI_FAKE_MODE`) — same mode the test-suite uses
- 🐳 One-command Docker Compose deployment (dev and prod variants)
- 📱 Flutter-ready: identical JSON event format across REST, SSE, and WS

---

## 🚀 Quickstart

```bash
git clone https://github.com/yosefyosef13054/NEXUSBOT.git
cd NEXUSBOT

cp .env.example .env
# edit .env → set OPENAI_API_KEY (or AI_FAKE_MODE=true to run without a key)
#             and JWT_SECRET_KEY (required in production)

docker compose up --build
```

Then open:

| Service | URL |
|---|---|
| API root        | http://localhost:8000/api/v1 |
| Swagger UI      | http://localhost:8000/api/v1/docs |
| ReDoc           | http://localhost:8000/api/v1/redoc |
| Liveness probe  | http://localhost:8000/api/v1/healthz |
| Readiness probe | http://localhost:8000/api/v1/readyz |
| Prometheus      | http://localhost:8000/metrics |

A 60-second end-to-end smoke test:

```bash
# 1. Register
curl -s -X POST localhost:8000/api/v1/auth/register \
  -H 'content-type: application/json' \
  -d '{"email":"a@b.com","password":"demopassword"}'

# 2. Login → grab access_token from the response
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"a@b.com","password":"demopassword"}' | jq -r .access_token)

# 3. Create a session
SID=$(curl -s -X POST localhost:8000/api/v1/sessions \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{}' | jq -r .id)

# 4. Talk to NexusBot (blocking) …
curl -s -X POST localhost:8000/api/v1/sessions/$SID/messages \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"content":"What can you do?"}' | jq

# 5. … or stream it
curl -N -X POST localhost:8000/api/v1/sessions/$SID/messages/stream \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -H 'accept: text/event-stream' -d '{"content":"Tell me a joke"}'
```

Production: `docker compose -f docker-compose.prod.yml up -d --build` (immutable images, no
reload, migrations as a one-shot job, multiple uvicorn workers).

---

## 🧩 Features

<table>
<tr>
<td width="50%" valign="top">

### 💬 Chat
- Multi-session, multi-user, **multi-workspace** (orgs with owner/admin/member roles)
- Persistent history (cursor pagination) on a **message tree**: edit a message or regenerate
  a reply and switch between the resulting branches
- **Assistants** (personas): system prompt, model, tools, collections and memory defaults per org
- Titles generated in the background after the first turn
- Thumbs up/down feedback per reply; search across all chats

</td>
<td width="50%" valign="top">

### ⚡ Streaming
- Server-Sent Events (`/messages/stream`) and WebSocket (`/ws/sessions/{id}`) with
  first-frame auth — **identical JSON event format** on both
- Tokens, citations, tool calls, usage, done
- **Resumable**: every frame is logged with a sequence number; reconnect and replay what you
  missed, or start a turn in the background and attach later
- Explicit `stop` → partial reply persisted; disconnects never lose a reply
- `Idempotency-Key` on sends: retries never duplicate a turn

</td>
</tr>
<tr>
<td valign="top">

### 📚 RAG 2.0
- File upload or **URL ingestion** → async ingest via Celery, with progress %
- PDF · DOCX · MD · HTML · TXT · CSV · JSON (sniffed, not trusted), SHA-256 dedup
- **Hybrid retrieval**: pgvector HNSW + Postgres full-text, reciprocal rank fusion
- Heading/page-aware chunks with contextual headers; optional LLM reranking and query rewriting
- **Structured citations** (chunk, page, section, offsets) + chunk viewer endpoints
- **Collections** (knowledge bases) scoping per chat or per turn

</td>
<td valign="top">

### 🛠 Agent & Tools
- **LangGraph** ReAct agent with Postgres checkpoints
- Built-in: calculator, web search, fetch URL, DB query (`GET /tools`)
- **Approvals**: side-effecting tools pause the turn until the user approves or denies
- Every tool run persisted as a `role=tool` message + audit row; step and time budgets
- Every tool receives a `ToolContext` — identity comes from the request, never from the model

</td>
</tr>
<tr>
<td valign="top">

### 🔀 Models
- `GET /models`: every provider with credentials configured
- OpenAI · Anthropic · Google · Azure OpenAI · Ollama (local)
- Resolution: request → session → user default → server default
- Fallback chains, catalog prices for cost accounting
- Capability flags (tools, vision, sampling params)

### 🧠 Memory
- Windowed short-term history (configurable)
- Long-term rolling summary per session
- **Long-term user memory**: facts, preferences and standing instructions recalled by
  similarity into every turn and extracted automatically after turns (per-user opt-out)
- Auto-compaction when the token budget is exceeded, after the reply is delivered
- Token-aware via `tiktoken` (with an offline fallback)

</td>
<td valign="top">

### 🔐 Auth & Security
- JWT access tokens + **rotating** refresh tokens with reuse detection
- Logout (one device / everywhere), change password, delete account
- Revocable API keys (SHA-256 hashed at rest)
- Redis rate limiting per user (chat) and per IP (auth)
- Production guard for weak secrets, magic-byte upload validation
- Structured error envelope with request ids

</td>
</tr>
<tr>
<td valign="top">

### 📊 Observability
- Structured JSON logs (structlog) with request ids
- Prometheus `/metrics`: requests, latency, tokens, cost, tools, retrievals, ingestion
- Provider-reported usage + cost per call in `ai_usage_logs`
- Optional Sentry integration
- `X-Request-Id` / `X-Process-Time-Ms` on every response

</td>
<td valign="top">

### 🐳 Deployment & DX
- `docker compose up` for dev, `docker-compose.prod.yml` for prod
- API + worker + Postgres (pgvector) + Redis
- Alembic migrations
- `AI_FAKE_MODE` for keyless local development
- GitHub Actions CI: ruff, mypy, pytest with services, Docker build

</td>
</tr>
</table>

---

## 🏗 Architecture

<div align="center">
<img src="assets/architecture.svg" alt="NexusBot architecture" width="100%"/>
</div>

```mermaid
flowchart LR
    F[Flutter App<br/>mobile · web · desktop]
    F -->|REST / SSE / WS| API[FastAPI<br/>/api/v1]
    API --> S[Services<br/>auth · chat · sessions · docs]
    S --> ORCH[NexusBot Orchestrator]
    ORCH --> C1[Chat Chain]
    ORCH --> C2[RAG Chain]
    ORCH --> C3[Tool Agent]
    ORCH --> MEM[Memory<br/>window + summary]
    C2 --> R[Retriever<br/>pgvector + HNSW]
    C3 --> T[Tools<br/>calc · web · db · custom]
    S --> DB[(Postgres<br/>+ pgvector)]
    S --> RD[(Redis)]
    S -.after reply.-> BG[Background<br/>titles · summaries]
    API -.queues.-> W[Celery Worker]
    W --> ING[Doc Ingest<br/>load → split → embed]
    ING --> DB
    ORCH -->|chat + embeddings| LLM[OpenAI API<br/>or fake mode]
```

### Layered design — strictly one-way dependencies

```
┌────────────────────────────────────────────────────┐
│  app/api/v1     ← HTTP / SSE / WS layer            │
├────────────────────────────────────────────────────┤
│  app/services   ← business logic, transactions     │
├────────────────────────────────────────────────────┤
│  app/ai         ← LangChain orchestrator + chains  │
├────────────────────────────────────────────────────┤
│  app/models     ← SQLAlchemy ORM (UUIDs, soft-del) │
└────────────────────────────────────────────────────┘
```

**Principles applied:** Clean Architecture · SOLID · Dependency Injection (FastAPI `Depends`) · single LLM/embedding/vector swap point · zero coupling between API and LangChain.

### How a streamed turn works

1. The user message is persisted and committed, then echoed as a `user_message` event.
2. The orchestrator loads the history window + rolling summary, runs retrieval (only if the
   user has ready documents) and emits `citations`.
3. The model streams through a callback collector: `token`, `tool_call` and cumulative `usage`
   events flow to the client as they happen.
4. `done` carries the full text, provider-reported usage and latency; the assistant message,
   tool-call audit rows and the usage/cost row are persisted and `assistant_message` closes
   the stream.
5. Title generation and summary compaction run afterwards in a background task.

If the client disconnects or sends `stop`, the model call is cancelled right away and the
partial reply is persisted with `status = "interrupted"`.

---

## 📡 API Reference

> Full reference in [`docs/api.md`](docs/api.md) · Importable Postman collection: [`postman/collection.json`](postman/collection.json)

All authenticated endpoints accept **either** `Authorization: Bearer <jwt>` **or** `X-API-Key: <key>`.

### Auth
| Method | Endpoint | Purpose |
|---|---|---|
| `POST`   | `/auth/register`           | Create account |
| `POST`   | `/auth/login`              | Login → JWT pair |
| `POST`   | `/auth/refresh`            | Rotate the refresh token, get a new pair |
| `POST`   | `/auth/logout`             | Revoke one refresh token |
| `POST`   | `/auth/logout-all`         | Revoke every refresh token |
| `GET`    | `/auth/me`                 | Current user profile (incl. `personal_org_id`) |
| `PATCH`  | `/auth/me`                 | Update profile / default model / memory opt-out |
| `POST`   | `/auth/change-password`    | Change password (signs other devices out) |
| `DELETE` | `/auth/me`                 | Delete account (password confirmed) |
| `POST`   | `/auth/api-keys`           | Mint API key (shown once) |
| `DELETE` | `/auth/api-keys/{id}`      | Revoke API key |

### Organizations, usage & assistants
All resources live in the active org, chosen per request with `X-Org-Id` (default: personal org).

| Method | Endpoint | Purpose |
|---|---|---|
| `GET`/`POST` | `/orgs` · `/orgs/{id}` | Workspaces; `PATCH` settings: budgets, default / allowed models |
| `GET`/`POST`/`PATCH`/`DELETE` | `/orgs/{id}/members…` | Members by email, roles owner / admin / member |
| `GET`    | `/usage/me`                | Month-to-date usage + remaining budget (quota → **429 `quota_exceeded`**) |
| `GET`    | `/orgs/{id}/usage`         | Admin report grouped by user / model / day |
| `GET`    | `/admin/usage` · `/admin/feedback` | Platform-wide reports for server admins |
| `GET`/`POST`/`PATCH`/`DELETE` | `/assistants…` | Personas: prompt, model, tools, collections, defaults |
| `GET`/`POST`/`PATCH`/`DELETE` | `/memories…` | Long-term user memory |

### Models & tools
| Method | Endpoint | Purpose |
|---|---|---|
| `GET`    | `/models`                  | Models the server can serve, with prices and capabilities |
| `GET`    | `/tools`                   | Agent tools and whether they need approval |

### Sessions
| Method | Endpoint | Purpose |
|---|---|---|
| `POST`   | `/sessions`                | Create session (validated `model`, `assistant_id`, `meta.collection_ids`) |
| `GET`    | `/sessions`                | List (paginated) |
| `GET`    | `/sessions/{id}`           | Get one |
| `PATCH`  | `/sessions/{id}`           | Update title / system prompt |
| `DELETE` | `/sessions/{id}`           | Soft delete |

### Chat
| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/sessions/{id}/messages`             | Send & receive (blocking, `Idempotency-Key`) |
| `POST` | `/sessions/{id}/messages/stream`      | **SSE stream** (`id:` sequence numbers) |
| `POST` | `/sessions/{id}/messages/background`  | Start a turn, **202**, attach later |
| `GET`  | `/sessions/{id}/generations/{gid}/events` | Replay / follow a generation (`after`, `Last-Event-ID`) |
| `GET`  | `/sessions/{id}/generations/active`   | In-flight turn for this session |
| `POST` | `/sessions/{id}/generations/{gid}/stop` | Stop a generation (partial reply persisted) |
| `POST` | `/sessions/{id}/messages/{mid}/edit`  | Edit a user message → new branch (SSE) |
| `POST` | `/sessions/{id}/messages/regenerate` · `…/regenerate/stream` | Regenerate; previous reply kept as a sibling |
| `GET`  | `/sessions/{id}/messages/{mid}/siblings` · `POST /sessions/{id}/branch` | Browse / switch branches |
| `POST`/`DELETE` | `/sessions/{id}/messages/{mid}/feedback` | Thumbs up / down |
| `GET`  | `/sessions/{id}/messages`             | History of the active branch (cursor pagination, incl. tool messages) |
| `GET`  | `/sessions/{id}/approvals`            | Pending tool approvals |
| `POST` | `/sessions/{id}/approvals/{aid}`      | Approve / deny → **SSE continuation** |
| `POST` | `/search`                             | Full-text search with highlights |
| `WS`   | `/ws/sessions/{id}`                   | **WebSocket stream** (auth in first frame, `approval` / `regenerate` / `stop` frames) |

### Documents & collections (RAG)
| Method | Endpoint | Purpose |
|---|---|---|
| `POST`   | `/documents`            | Upload → queued ingest (dedup by content hash) |
| `POST`   | `/documents/from-url`   | Ingest a public web page / PDF (SSRF-guarded) |
| `GET`    | `/documents`            | List (paginated, filter by collection) |
| `GET`    | `/documents/{id}`       | Poll status + progress |
| `PATCH`  | `/documents/{id}`       | Rename / move between collections |
| `GET`    | `/documents/{id}/chunks`| Chunk viewer for citations |
| `DELETE` | `/documents/{id}`       | Soft delete + remove file |
| `POST`/`GET`/`PATCH`/`DELETE` | `/collections…` | Knowledge bases |

### Streaming wire format (identical for SSE & WS)

```jsonc
{ "type": "user_message",      "data": { "id": "…", "content": "…", "generation_id": "…" }, "seq": 1 }
{ "type": "citations",         "data": [ { "document_name": "spec.pdf", "score": 0.84, "excerpt": "…" } ] }
{ "type": "token",             "data": "Hel" }
{ "type": "token",             "data": "lo" }
{ "type": "tool_call",         "data": { "tool": "calculator", "input": "…", "phase": "end", "output": "…" } }
{ "type": "approval_required", "data": { "approval_id": "…", "tool_calls": [ { "tool": "fetch_url", "input": {…} } ] } }
{ "type": "usage",             "data": { "prompt_tokens": 1240, "completion_tokens": 312, "total_tokens": 1552 } }
{ "type": "done",              "data": { "text": "Hello!", "latency_ms": 820, "interrupted": false } }
{ "type": "assistant_message", "data": { "id": "…", "content": "Hello!", "citations": [], "interrupted": false, "memories_used": [] } }
```
Every frame carries `generation_id` and `seq` (the SSE `id:`), so a client can resume from
`GET /sessions/{id}/generations/{generation_id}/events?after=<seq>` after a dropped connection.

Errors: `{ "error": { "code", "message", "details" }, "request_id": "…" }` plus an `X-Request-Id` header.

---

## 📱 Flutter app

The official cross-platform client lives in its own repository:
[**yosefyosef13054/NEXUSBOT-APP**](https://github.com/yosefyosef13054/NEXUSBOT-APP) — Riverpod,
go_router with real `/chat/:id` routes, Dio with single-flight token refresh, SSE and WebSocket
streaming with stop, document upload with progress, account management, and a crimson theme
that matches the backend.

```bash
git clone https://github.com/yosefyosef13054/NEXUSBOT-APP.git
cd NEXUSBOT-APP
flutter pub get
flutter run --dart-define=API_BASE_URL=http://localhost:8000/api/v1
```

The integration patterns are documented standalone in [`docs/flutter_integration.md`](docs/flutter_integration.md) if you want to embed NexusBot into an existing Flutter codebase.

---

## 🛠 Tech stack

| Layer | Choice | Why |
|---|---|---|
| API framework      | **FastAPI** + **Uvicorn**         | Async, fast, automatic OpenAPI |
| AI orchestration   | **LangChain 0.3** + **LangGraph** | Runnable composition, agent graph with checkpoints |
| LLMs               | **OpenAI · Anthropic · Google · Azure · Ollama** (+ fake mode) | Catalog + factory in `app/ai/providers/` |
| Embeddings         | **OpenAI** (+ fake mode)          | `app/ai/llm.py` |
| Database           | **PostgreSQL 16** + **pgvector**  | Single DB for relational + vector |
| Retrieval          | **pgvector HNSW + Postgres FTS**  | Hybrid, fused with RRF; optional reranker |
| Cache / Broker     | **Redis 7**                       | Rate limit, cache, Celery broker |
| Background jobs    | **Celery** + in-process tasks     | Ingestion off the hot path; titles/summaries after the reply |
| Migrations         | **Alembic**                       | `0001_initial`, `0002_phase1_foundation` |
| ORM                | **SQLAlchemy 2 (async)**          | Modern typed mapping |
| Validation         | **Pydantic v2**                   | Boundary contracts |
| Logging / metrics  | **structlog** + **prometheus-client** | JSON logs with request ids, `/metrics` |
| Error tracking     | **Sentry** (optional)             | Drop-in if `SENTRY_DSN` set |
| Tokenization       | **tiktoken**                      | Token accounting & cost estimation |
| Containers         | **Docker** + **Docker Compose**   | One-command bring-up |
| Quality            | **ruff**, **mypy**, **pytest**    | CI on every push |

---

## 📁 Project structure

<details>
<summary>Click to expand</summary>

```
nexusbot/
├─ app/
│  ├─ api/v1/endpoints/      # REST + SSE + WebSocket routes
│  │  ├─ auth.py · orgs.py · usage.py · admin.py · assistants.py · memories.py
│  │  ├─ sessions.py · chat.py (messages, branches, generations, feedback) · documents.py · collections.py
│  │  ├─ ws.py · health.py · models.py
│  │  └─ router.py
│  ├─ ai/                    # LangChain layer
│  │  ├─ providers/          # Model catalog, registry (routing), factory per provider
│  │  ├─ llm.py              # Chat/embedding factories (+ fake mode)
│  │  ├─ fake.py             # Deterministic model (scriptable tool calls) for tests & keyless dev
│  │  ├─ orchestrator.py     # generate() / stream() / resume_stream() — cancellation, approvals, fallbacks
│  │  ├─ callbacks.py        # Streaming token / tool / usage collector
│  │  ├─ prompts/            # Versioned system prompts + composer
│  │  ├─ memory/             # Short-term window + rolling summary
│  │  ├─ chains/             # chat · rag
│  │  ├─ agents/             # LangGraph graph · runner · Postgres checkpointer
│  │  ├─ tools/              # registry (approval flags) · context · calculator · web_search · fetch_url · db
│  │  └─ rag/                # loader · splitter · embedder · retriever (hybrid) · rerank · query · pipeline
│  ├─ services/              # auth · org · quota · assistant · memory · feedback · chat (tree, generation jobs, approvals) · sessions · documents · collections · usage
│  ├─ models/                # SQLAlchemy ORM (UUIDs, soft-delete)
│  ├─ schemas/               # Pydantic request/response contracts
│  ├─ core/                  # security · deps (Principal) · exceptions · rate_limit · streams (generation log) · idempotency · middleware · metrics · background · logger
│  ├─ db/                    # Async engine + sessions (API pool, worker NullPool)
│  ├─ cache/                 # Redis client
│  ├─ workers/               # Celery app + tasks
│  ├─ utils/                 # tokens · files
│  ├─ config.py              # Centralized settings (pydantic-settings)
│  └─ main.py                # FastAPI factory + middleware + lifespan
├─ alembic/                  # DB migrations
├─ evals/                    # Eval harness: datasets · graders · runner · CLI
├─ docs/                     # api.md · flutter_integration.md
├─ postman/                  # Importable collection
├─ assets/                   # Logo · banner · architecture diagram
├─ tests/                    # unit · integration · live-socket tests
├─ .github/workflows/ci.yml
├─ Dockerfile · docker-compose.yml · docker-compose.prod.yml
├─ requirements.txt · requirements-dev.txt · pyproject.toml
├─ .env.example
└─ README.md · CHANGELOG.md
```

</details>

---

## ⚙️ Configuration

Every knob lives in [`.env.example`](.env.example). The important ones:

| Variable | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `AZURE_OPENAI_*` / `OLLAMA_*` | — | Each configured provider is enabled; one is required unless `AI_FAKE_MODE=true` |
| `AI_FAKE_MODE`              | `false`        | Deterministic in-process model + embeddings, no network |
| `DEFAULT_CHAT_MODEL`        | `OPENAI_CHAT_MODEL` | Server default; `ENABLED_MODELS` / `DISABLED_MODELS` restrict the list |
| `MODEL_FALLBACKS`           | `{}`           | JSON `{"gpt-4o": ["claude-sonnet-5-5"]}` tried when the primary fails |
| `OPENAI_EMBEDDING_MODEL`    | `text-embedding-3-small` | Must match `OPENAI_EMBEDDING_DIM` |
| `MODEL_PRICING`             | `{}`           | JSON `{"model": [in_per_1k, out_per_1k]}` merged over built-in prices |
| `JWT_SECRET_KEY`            | `change-me`    | **≥ 32 random chars; production refuses the default** |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` / `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `30` / `30` | Refresh tokens rotate on use |
| `RAG_CHUNK_SIZE` / `_OVERLAP` / `_TOP_K` | `800` / `120` / `5` | Tune per corpus |
| `RAG_HYBRID_SEARCH` / `RAG_RERANKER` / `RAG_QUERY_REWRITE` | `true` / `none` / `false` | Hybrid retrieval, LLM reranking, standalone-query rewriting |
| `AGENT_MAX_STEPS` / `AGENT_MAX_TOOL_SECONDS` | `8` / `60` | Per-turn agent budgets |
| `TOOLS_REQUIRING_APPROVAL` / `APPROVAL_TTL_MINUTES` | `[]` / `30` | Human-in-the-loop gating (`fetch_url` always requires approval) |
| `EMBEDDING_BATCH_SIZE`      | `64`           | Chunks per embedding request during ingest |
| `CHAT_MAX_HISTORY_MESSAGES` | `20`           | Windowed memory size |
| `CHAT_SUMMARY_TRIGGER_TOKENS` | `3000`       | When the summarizer kicks in |
| `GENERATION_TIMEOUT_SECONDS`| `180`          | Hard cap per generation |
| `MEMORY_ENABLED` / `MEMORY_TOP_K` / `MEMORY_MAX_PER_USER` | `true` / `6` / `500` | Long-term user memory recall + extraction |
| `QUOTA_CACHE_SECONDS`       | `30`           | Month-to-date usage cache for org budgets |
| `STREAM_EVENTS_TTL_SECONDS` | `3600`         | How long a generation can be resumed after a disconnect |
| `IDEMPOTENCY_TTL_SECONDS`   | `86400`        | Lifetime of `Idempotency-Key` results |
| `RATE_LIMIT_PER_MINUTE`     | `60`           | Per user (chat, uploads) |
| `AUTH_RATE_LIMIT_PER_MINUTE`| `10`           | Per IP (register, login) |
| `MAX_UPLOAD_MB`             | `25`           | Upload guard |
| `ALLOWED_UPLOAD_EXTENSIONS` | `.pdf,.docx,…` | Sniffed against file content |
| `METRICS_ENABLED`           | `true`         | Exposes `/metrics` |
| `VECTOR_BACKEND`            | `pgvector`     | Adapter slot (Pinecone/Weaviate stubs) |

---

## 🧪 Tests

```bash
pip install -r requirements-dev.txt
ruff check app tests && mypy app

# Unit tests (no infra):
pytest tests/test_smoke.py tests/test_unit.py

# Full suite — needs Postgres (pgvector) + Redis, e.g. `docker compose up postgres redis`:
DATABASE_URL=postgresql+asyncpg://chatbot:chatbot@127.0.0.1:5432/chatbot_test \
SYNC_DATABASE_URL=postgresql://chatbot:chatbot@127.0.0.1:5432/chatbot_test \
pytest
```

The suite runs with `AI_FAKE_MODE` so it never needs a provider key (the fake model can be
scripted to call tools). It covers the auth lifecycle (rotation, reuse detection, logout,
deletion), chat over REST/SSE/WebSocket, stop and disconnect/resume handling (including a real uvicorn
subprocess to exercise client disconnects over sockets), the agent loop with approvals and
budgets, model routing, hybrid retrieval, collections, URL ingestion with SSRF guards, upload
validation and dedup, workspaces and roles, quotas, assistants, feedback, memory recall and
extraction, branching, background / resumable generations, idempotency keys, the eval
harness, metrics and middleware. CI runs the same on every push with Postgres and Redis
service containers.

### Evals

```bash
AI_FAKE_MODE=true python -m evals.run --dataset evals/datasets/chat_smoke.jsonl
python -m evals.run --dataset evals/datasets/chat_smoke.jsonl --model gpt-4o-mini --fail-under 0.9 --out reports/smoke.json
```
Datasets are JSONL cases graded with deterministic checks or an LLM judge; see
[`evals/README.md`](evals/README.md).

---

## 🛣 Roadmap

- [x] **Phase 1 — foundation** (v1.1): everything above actually works, CI, metrics, account lifecycle
- [x] **Phase 2 — AI core** (v1.2): multi-provider models with real routing and fallbacks, LangGraph agent with human-in-the-loop approvals and persisted tool messages, hybrid retrieval + reranking + structured citations, URL ingestion, collections
- [x] **Phase 3 — product layer** (v1.3): workspaces & roles, assistants, quotas, feedback, admin API, user memory, conversation branching, resumable streams, eval harness
- [ ] **Phase 4 — multimodal & mobile depth**: attachments & vision, voice, artifacts, object storage, MCP tools, offline-first client, i18n/RTL, push, release pipeline

PRs welcome.

---

## 🤝 Contributing

1. Fork → create a feature branch from `main`
2. `cp .env.example .env` and `docker compose up` (or `AI_FAKE_MODE=true` for keyless work)
3. Add or update tests under `tests/`; keep `ruff`, `mypy` and `pytest` green
4. Open a PR with a clear description and screenshots if UI-relevant

Please keep PRs scoped. One reason NexusBot stays readable is that every layer has a single responsibility — don't blur the API/service/AI boundary in a single change.

---

## 📄 License

[MIT](LICENSE) — free for personal and commercial use. Attribution appreciated, not required.

---

## 🙏 Acknowledgements

NexusBot stands on the shoulders of:
[FastAPI](https://fastapi.tiangolo.com/) ·
[LangChain](https://www.langchain.com/) ·
[pgvector](https://github.com/pgvector/pgvector) ·
[SQLAlchemy](https://www.sqlalchemy.org/) ·
[Celery](https://docs.celeryq.dev/) ·
[Pydantic](https://docs.pydantic.dev/)

---

<div align="center">

<sub>If NexusBot saved you a week of plumbing, drop a ⭐ — it helps others find it.</sub>

<br/>

<img src="assets/logo.svg" alt="NexusBot" width="64" />

</div>
