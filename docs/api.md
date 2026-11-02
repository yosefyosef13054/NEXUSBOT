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

## Sessions

### `POST /sessions`
```json
{ "title": null, "system_prompt": "You are a helpful…", "model": "gpt-4o-mini" }
```
A session created without a title is called `New chat` until the first turn completes; a title
is then generated **in the background** (re-fetch the session list a moment later).

### `GET /sessions?page=1&page_size=20`
### `GET /sessions/{session_id}`
### `PATCH /sessions/{session_id}`
```json
{ "title": "New title", "system_prompt": "…", "meta": { "any": "json" } }
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
  "temperature": 0.3,
  "max_tokens": 800
}
```
**200** → `SendMessageResponse` with `user_message`, `assistant_message`, `citations[]`,
`tool_calls[]`, `usage`.

`usage` is provider-reported where available:
`{ "prompt_tokens", "completion_tokens", "embedding_tokens", "total_tokens" }`.
`prompt_tokens` covers the whole prompt (system prompt, summary, retrieved context, history,
and the new message), not just the user text.

Unknown `tool_names` → **422** with `details.unknown` and `details.available`.
Rate limited **per user** (`RATE_LIMIT_PER_MINUTE`).

`assistant_message.meta` contains `model`, `latency_ms`, `citations`, `tool_calls`, `usage`,
`status` (`ok` | `interrupted` | `error`), `interrupt_reason` and `generation` (the settings used
for the turn, replayed by `regenerate`).

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
| `error` | `{ code, message }` — the stream still ends with `done` + `assistant_message` |
| `done` | `{ text, latency_ms, model, prompt_tokens, completion_tokens, interrupted, interrupt_reason }` |
| `assistant_message` | final persisted message incl. `citations`, `tool_calls`, `usage`, `interrupted`, `status` |

**Stopping:** closing the connection cancels the model call immediately. The partial reply is
persisted with `meta.status = "interrupted"` and `interrupt_reason = "disconnect"`, so the next
history load is consistent with what the client displayed. Generations also time out after
`GENERATION_TIMEOUT_SECONDS` (`interrupt_reason = "timeout"`).

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

---

## Documents (RAG)

### `POST /documents` — multipart
- `file` (required). Allowed extensions: `ALLOWED_UPLOAD_EXTENSIONS`
  (default `.pdf .docx .md .markdown .txt .html .htm .csv .json .log`).
  The client `Content-Type` is ignored; the bytes are sniffed and must match the extension.
- `session_id` (UUID, optional — scope the doc to a single chat)

**202 Accepted** → `{ document, queued: true }`. The document is queued on Celery; poll
`GET /documents/{id}` until `status == "ready"`. While `processing`, `meta.progress` is an
integer percentage.

Re-uploading a file with the same content (SHA-256, per user and scope) returns the existing
document with `queued: false`. `meta.sha256` is set on every document.

**422** `validation_error` for unsupported, mismatched, empty or oversized files
(`MAX_UPLOAD_MB`). **503** `service_unavailable` when the ingestion queue is down.

### `GET /documents?page=1&page_size=20`
### `GET /documents/{id}`
### `DELETE /documents/{id}`
Soft-deletes the row and removes the stored file.

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
{"type":"user_message","content":"hi","use_rag":true,"use_tools":false,"tool_names":null,"temperature":null}
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
