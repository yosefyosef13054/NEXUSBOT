# API Reference

Base URL: `http://localhost:8000/api/v1`

All authenticated endpoints accept **either**:
- `Authorization: Bearer <access_token>` (JWT, 30 min by default), **or**
- `X-API-Key: <plaintext_key>` (long-lived, scoped to the issuing user)

All errors are returned in a single, stable envelope. Every response (success or error)
also carries an `X-Request-Id` header; send your own `X-Request-Id` to have it echoed.

```json
{
  "error": { "code": "validation_error", "message": "…", "details": {} },
  "request_id": "c0ffee0123456789abcdef0123456789"
}
```

Error codes: `unauthorized` (401), `forbidden` (403), `not_found` (404), `conflict` (409),
`validation_error` (422), `rate_limited` (429, with `Retry-After`), `ai_provider_error` (502),
`service_unavailable` (503), `internal_error` (500).

---

## Auth

### `POST /auth/register`
```json
{ "email": "a@b.com", "password": "min-8-chars", "full_name": "Optional" }
```
**201** → `UserRead`. Rate limited per IP (`AUTH_RATE_LIMIT_PER_MINUTE`).

### `POST /auth/login`
```json
{ "email": "a@b.com", "password": "…" }
```
**200** → `{ access_token, refresh_token, token_type, expires_in }`. Rate limited per IP.

### `POST /auth/refresh`
```json
{ "refresh_token": "…" }
```
**200** → a **new** token pair. Refresh tokens **rotate**: the presented token is retired and
must be replaced by the returned one.

- Presenting a retired token again within `REFRESH_TOKEN_REUSE_GRACE_SECONDS` (default 15 s)
  is treated as a client retry and succeeds.
- Presenting it later is treated as theft: the whole token family (every token descended
  from that login) is revoked and the response is **401** `unauthorized` with
  `"Refresh token reuse detected"`. The user must sign in again.

### `POST /auth/logout`
```json
{ "refresh_token": "…" }
```
Revokes that refresh token (sign out this device). Always **200**. Access tokens are stateless
and remain valid until they expire.

### `POST /auth/logout-all`
Auth required. Revokes every refresh token of the user.

### `GET /auth/me`
Returns the current user.

### `PATCH /auth/me`
```json
{ "full_name": "Ada Lovelace" }
```

### `POST /auth/change-password`
```json
{ "current_password": "…", "new_password": "min-8-chars" }
```
**200** → a new token pair for this device; every other refresh token is revoked.

### `DELETE /auth/me`
```json
{ "password": "…" }
```
Deactivates the account (required for app-store compliance): revokes all tokens and API keys,
soft-deletes sessions and documents, removes uploaded files, and anonymizes the e-mail so it can
register again. **401** if the password is wrong.

### `POST /auth/api-keys`
```json
{ "name": "flutter-prod" }
```
**201** → returns the **plaintext key once** (`plaintext_key`). The server keeps only a SHA-256 hash.

### `DELETE /auth/api-keys/{key_id}`
Revokes the key.

---

## Models & tools

### `GET /models`
Models the server can serve right now — every provider with credentials configured
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, Azure, Ollama), restricted by
`ENABLED_MODELS` / `DISABLED_MODELS`. In `AI_FAKE_MODE` only `nexusbot-fake` is listed.

```json
{ "default_model": "gpt-4o-mini",
  "items": [ { "id": "claude-sonnet-5-5", "provider": "anthropic", "display_name": "Claude Sonnet 5.5",
               "context_window": 1000000, "max_output_tokens": 128000,
               "supports_tools": true, "supports_vision": true, "supports_streaming": true,
               "input_usd_per_1m": 2.0, "output_usd_per_1m": 10.0, "tags": ["balanced", "coding"],
               "is_default": false } ] }
```

A turn's model resolves **request `model` → session `model` → user `default_model` →
server default**. Any explicitly requested model that is not available is a **422** with
`details.available`. `MODEL_FALLBACKS` lets a failing provider fall back to another model
when nothing was streamed yet.

### `GET /tools`
```json
{ "items": [ { "name": "calculator", "description": "…", "requires_approval": false },
             { "name": "fetch_url", "description": "…", "requires_approval": true } ] }
```

---

## Sessions

### `POST /sessions`
```json
{ "title": null, "system_prompt": "You are a helpful…", "model": "gpt-4o-mini",
  "meta": { "collection_ids": ["<uuid>"] } }
```
`model` is validated against `GET /models`. `meta.collection_ids` is the chat's default
knowledge scope (validated as the user's collections).
A session created without a title is called `New chat` until the first turn completes; a title
is then generated **in the background** (re-fetch the session list a moment later).

### `GET /sessions?page=1&page_size=20`
### `GET /sessions/{session_id}`
### `PATCH /sessions/{session_id}`
```json
{ "title": "New title", "system_prompt": "…", "model": "claude-sonnet-5-5", "meta": { "collection_ids": [] } }
```
### `DELETE /sessions/{session_id}`
Soft delete.

---

## Chat

### `POST /sessions/{id}/messages` — full response
```json
{
  "content": "Summarize the uploaded PDF",
  "use_rag": true,
  "use_tools": false,
  "tool_names": null,
  "model": null,
  "collection_ids": null,
  "temperature": 0.3,
  "max_tokens": 800
}
```
**200** → `SendMessageResponse` with `user_message`, `assistant_message`, `tool_messages[]`
(one `role=tool` message per executed tool call), `citations[]`, `tool_calls[]`, `usage` and
`approval` (non-null when the turn is parked, see *Approvals*).

`model` overrides the session/user/server model for this turn. `collection_ids` restricts
retrieval to those collections (plus documents scoped to this session); `null` uses the
session default (`meta.collection_ids`) or all of the user's documents.

`usage` is provider-reported where available:
`{ "prompt_tokens", "completion_tokens", "embedding_tokens", "total_tokens" }`.
`prompt_tokens` covers the whole prompt (system prompt, summary, retrieved context, history,
and the new message), not just the user text.

Unknown `tool_names` → **422** with `details.unknown` and `details.available`.
Rate limited **per user** (`RATE_LIMIT_PER_MINUTE`).

`assistant_message.meta` contains `model`, `provider`, `latency_ms`, `citations`,
`tool_calls`, `usage`, `status` (`ok` | `interrupted` | `error` | `awaiting_approval`),
`interrupt_reason`, `approval` (while parked) and `generation` (the settings used for the
turn — RAG, tools, model, collections — replayed by `regenerate`).

Citations are structured:
```json
{ "document_id": "…", "document_name": "spec.pdf", "chunk_id": "…", "chunk_index": 3,
  "page": 4, "section": "2. Scope > 2.1 Goals", "start_index": 1200, "end_index": 1980,
  "collection_id": null, "score": 0.83, "match": "hybrid", "excerpt": "…" }
```
`match` is `vector`, `keyword` or `hybrid`; keyword-only hits report `score` 0.5.

### Tool messages
Every executed tool call is persisted as its own message with `"role": "tool"` between the
user message and the assistant reply:
```json
{ "role": "tool", "content": "<tool output>", "meta": { "tool": "calculator", "call_id": "call_1",
  "input": {"expression": "2+2"}, "status": "ok", "latency_ms": 12 } }
```
`meta.status` is `ok`, `error` or `denied`. Tool messages are not fed back to the model as
history; the assistant's reply already incorporates them.

### `POST /sessions/{id}/messages/stream` — Server-Sent Events

Same body. Send `Accept: text/event-stream`. The response is `text/event-stream`, starts with a
`: connected` comment frame, and then one JSON frame per event:

```
data: {"type":"token","data":"Hel"}\n\n
```

| type | data |
|---|---|
| `user_message` | persisted user message (`id`, `content`, `created_at`, …) |
| `citations` | retrieved chunks for this turn |
| `token` | next assistant token (string) |
| `tool_call` | `{ tool, input, run_id, phase: start\|end\|error, output?, error?, latency_ms? }` |
| `usage` | cumulative `{ prompt_tokens, completion_tokens, total_tokens }` |
| `approval_required` | the turn is parked until the user decides (see *Approvals*) |
| `error` | `{ code, message }` — the stream still ends with `done` + `assistant_message` |
| `done` | `{ text, latency_ms, model, provider, prompt_tokens, completion_tokens, interrupted, interrupt_reason, status, thread_id }` |
| `assistant_message` | final persisted message incl. `citations`, `tool_calls`, `usage`, `model`, `interrupted`, `status`, `approval` |

**Stopping:** closing the connection cancels the model call immediately. The partial reply is
persisted with `meta.status = "interrupted"` and `interrupt_reason = "disconnect"`, so the next
history load is consistent with what the client displayed. Generations also time out after
`GENERATION_TIMEOUT_SECONDS` (`interrupt_reason = "timeout"`).

### Approvals (human-in-the-loop)

Tools flagged `requires_approval` (`fetch_url`, anything in `TOOLS_REQUIRING_APPROVAL`) make the
agent pause before running them. The stream emits

```json
{ "type": "approval_required",
  "data": { "approval_id": "…", "session_id": "…", "message_id": "…", "status": "pending",
            "expires_at": "2026-11-02T13:00:00+00:00",
            "tool_calls": [ { "call_id": "call_1", "tool": "fetch_url", "input": {"url": "…"},
                              "description": "Fetches a public web page…" } ] } }
```
and then ends normally with `done` (`status: "awaiting_approval"`) and `assistant_message`
(`status: "awaiting_approval"`, `approval` set). The assistant message is a placeholder that is
**updated in place** when the decision arrives. Blocking turns return the same information in
`SendMessageResponse.approval`.

#### `POST /sessions/{id}/approvals/{approval_id}`
```json
{ "decision": "approve" | "deny", "tool_call_ids": null }
```
Send `Accept: text/event-stream`: the response **streams the rest of the turn** (`tool_call`,
`token`, `usage`, `done`, `assistant_message` with the placeholder's id). `deny` tells the
model the tool was refused; it finishes without it. **404** unknown approval, **409** already
resolved or expired (`APPROVAL_TTL_MINUTES`). Over WebSocket send
`{"type":"approval","approval_id":"…","decision":"approve"}`.

#### `GET /sessions/{id}/approvals?status=pending`
Pending (or `approved` | `denied` | `expired`) approvals, for reconciling after an app restart.

### `POST /sessions/{id}/messages/regenerate`
```json
{ "temperature": 0.7 }
```
Soft-deletes the previous assistant turn and generates a new one against the same user input,
using the same RAG/tool settings as the original turn.

### `GET /sessions/{id}/messages?cursor=ISO8601&limit=50`
Cursor-paginated history (newest fetched first via `cursor=created_at`).

### `POST /search`
```json
{ "query": "invoice", "limit": 20 }
```
Full-text (stemmed, ranked) search over the user's messages. Results carry `highlight`
(matched terms wrapped in `<mark>…</mark>`) and `rank`; very short or unparseable queries fall
back to substring matching.

---

## Documents (RAG)

### `POST /documents` — multipart
- `file` (required). Allowed extensions: `ALLOWED_UPLOAD_EXTENSIONS`
  (default `.pdf .docx .md .markdown .txt .html .htm .csv .json .log`).
  The client `Content-Type` is ignored; the bytes are sniffed and must match the extension.
- `session_id` (UUID, optional — scope the doc to a single chat)
- `collection_id` (UUID, optional — put the document in a collection)

**202 Accepted** → `{ document, queued: true }`. The document is queued on Celery; poll
`GET /documents/{id}` until `status == "ready"`. While `processing`, `meta.progress` is an
integer percentage.

Re-uploading a file with the same content (SHA-256, per user and scope) returns the existing
document with `queued: false`. `meta.sha256` is set on every document.

**422** `validation_error` for unsupported, mismatched, empty or oversized files
(`MAX_UPLOAD_MB`). **503** `service_unavailable` when the ingestion queue is down.

### `POST /documents/from-url`
```json
{ "url": "https://example.com/article", "session_id": null, "collection_id": null }
```
Downloads a public HTML / PDF / text resource and registers it like an upload (**202**, same
dedup). Private and loopback addresses, redirects to them, non-http schemes, unsupported
content types and oversized bodies (`WEB_FETCH_MAX_BYTES`) are **422**.

### `GET /documents?page=1&page_size=20&collection_id=<uuid>`
### `GET /documents/{id}`
### `PATCH /documents/{id}`
```json
{ "name": "renamed.pdf", "collection_id": null }
```
`collection_id: null` removes the document from its collection.

### `GET /documents/{id}/chunks?page=1&page_size=50` · `GET /documents/{id}/chunks/{chunk_id}`
Chunk text with `page`, `section`, `start_index`, `end_index` — enough to open a citation at
its source span.

### `DELETE /documents/{id}`
Soft-deletes the row and removes the stored file.

---

## Collections (knowledge bases)

| Method | Endpoint | Body / query | Notes |
|---|---|---|---|
| `POST`   | `/collections` | `{name, description?}` | **201** `CollectionRead` |
| `GET`    | `/collections?page=1&page_size=50` | | `Page<CollectionRead>` with `document_count` |
| `GET`    | `/collections/{id}` | | |
| `PATCH`  | `/collections/{id}` | `{name?, description?}` | |
| `DELETE` | `/collections/{id}?delete_documents=false` | | documents are unassigned unless `true` |

Scope a chat with `SendMessageRequest.collection_ids` (per turn) or
`PATCH /sessions/{id} {"meta": {"collection_ids": [...]}}` (session default).

---

## WebSocket

`ws://host/api/v1/ws/sessions/{session_id}`

1. Connect **without** credentials in the URL.
2. Send an auth frame within 10 s: `{"type":"auth","token":"<jwt>"}` or
   `{"type":"auth","api_key":"<key>"}`.
3. Receive `{"type":"auth_ok","data":{"user_id":"…"}}`, or an `error` frame followed by close
   code 1008.

Query-string credentials (`?token=` / `?api_key=`) are still accepted for older clients but are
deprecated (they end up in access logs).

Client → server:
```json
{"type":"user_message","content":"hi","use_rag":true,"use_tools":false,"tool_names":null,
 "model":null,"collection_ids":null,"temperature":null}
{"type":"approval","approval_id":"…","decision":"approve","tool_call_ids":null}
{"type":"stop"}
{"type":"ping"}
```

Server → client: the same event frames as the SSE stream, plus `auth_ok` and `pong`.

- `stop` cancels the in-flight generation; the server persists the partial reply and still
  sends `done` and `assistant_message` with `interrupted: true`.
- A `user_message` while another generation is running → `{"type":"error","data":{"code":"busy"}}`.
- Disconnecting mid-generation behaves like SSE: the partial reply is persisted.

---

## Rate limits

Chat and upload endpoints are limited **per authenticated user** (`RATE_LIMIT_PER_MINUTE`,
default 60/min); `register`, `login` and `refresh` are limited **per client IP**
(`AUTH_RATE_LIMIT_PER_MINUTE`, default 10/min). Exceeding a limit returns:

```json
{ "error": { "code": "rate_limited", "message": "Too many requests",
             "details": { "limit": 60, "window_seconds": 60, "retry_after_seconds": 42 } },
  "request_id": "…" }
```
with a `Retry-After` header.

---

## Operations

- `GET /healthz` (liveness) and `GET /readyz` (checks Postgres + Redis).
- `GET /metrics` (root, not under `/api/v1`) — Prometheus exposition: HTTP requests/latency by
  route, model requests/tokens/cost, tool calls, retrievals, ingestions, active and interrupted
  generations. Disable with `METRICS_ENABLED=false`.
- `AI_FAKE_MODE=true` runs the whole API with a deterministic in-process model and embeddings
  (no provider key) — used by the test-suite and handy for UI development.
