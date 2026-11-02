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
`validation_error` (422), `rate_limited` (429, with `Retry-After`), `quota_exceeded` (429, see
*Usage & quotas*), `ai_provider_error` (502), `service_unavailable` (503), `internal_error` (500).

**Workspaces.** Every resource (sessions, documents, collections, assistants, usage) belongs to
an organization. Each user has a personal org; pick the active one per request with
`X-Org-Id: <uuid>` (absent → personal org; non-members get **403**). See *Organizations*.

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

## Organizations (workspaces)

Each user gets a **personal org** at registration (`UserRead.personal_org_id`). Users can create
shared orgs and invite members by email. Roles: `owner` (creator, cannot be removed), `admin`
(manage members, settings, assistants) and `member`.

| Method | Endpoint | Body | Notes |
|---|---|---|---|
| `GET`    | `/orgs` | | the caller's orgs with `role`, `member_count`, `settings` |
| `POST`   | `/orgs` | `{name}` | **201** `OrgRead`, creator becomes owner |
| `GET`    | `/orgs/{id}` | | members only |
| `PATCH`  | `/orgs/{id}` | `{name?, settings?}` | admin/owner |
| `GET`    | `/orgs/{id}/members` | | `{items: [{user_id, email, full_name, role, joined_at}]}` |
| `POST`   | `/orgs/{id}/members` | `{email, role: admin\|member}` | admin/owner; **404** no such user, **409** already a member |
| `PATCH`  | `/orgs/{id}/members/{user_id}` | `{role}` | admin/owner |
| `DELETE` | `/orgs/{id}/members/{user_id}` | | admins remove anyone but the owner; members may remove themselves |
| `GET`    | `/orgs/{id}/usage?group_by=user\|model\|day&from=&to=` | | admin/owner, see *Usage & quotas* |

`settings` (all optional, `null` = unlimited / server default):
```json
{ "budget_tokens_per_month": 2000000, "budget_usd_per_month": 25.0,
  "default_model": "gpt-4o-mini", "allowed_models": ["gpt-4o-mini", "claude-haiku-5-5"] }
```
A turn whose requested / session / assistant model is not in `allowed_models` → **422** with
`details.allowed_models`. `default_model` sits between the user default and the server default
in the resolution order (request → session → assistant → user → org → server).

---

## Usage & quotas

### `GET /usage/me`
Month-to-date usage for the caller and the active org:
```json
{ "period": {"start": "2026-11-01T00:00:00+00:00", "end": "2026-12-01T00:00:00+00:00"},
  "user": {"tokens": 12345, "cost_usd": 0.42, "messages": 17},
  "org":  {"id": "…", "tokens": 99999, "cost_usd": 3.1, "messages": 120,
           "budget_tokens_per_month": null, "budget_usd_per_month": 10.0,
           "remaining_tokens": null, "remaining_usd": 6.9} }
```
When an org budget is exhausted every generation request (blocking, stream, background,
edit, regenerate, approval continuation) fails with **429**:
```json
{ "error": { "code": "quota_exceeded", "message": "Monthly token budget exhausted for this organization",
             "details": { "budget_tokens_per_month": 1000000, "used_tokens": 1000312,
                          "resets_at": "2026-12-01T00:00:00+00:00" } } }
```
Month-to-date usage is cached for `QUOTA_CACHE_SECONDS` and kept current as turns are
logged, so a budget trips on the next turn.

### `GET /orgs/{id}/usage?group_by=user|model|day`
Admin report: `{period, group_by, totals: {tokens, cost_usd, messages}, groups: [{key, tokens, cost_usd, messages}]}`
(`key` is the member email, model id or `YYYY-MM-DD`).

### `GET /admin/usage?group_by=org|user|model|day` · `GET /admin/feedback?page=&page_size=`
Platform-wide reports for **server administrators** (`users.role == "admin"`); **403** otherwise.

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

## Assistants (personas)

Reusable personas shared inside an org: a system prompt, model, tools, collections, RAG /
tool / memory defaults. Attach one to a session (`POST /sessions {assistant_id}` /
`PATCH /sessions/{id} {assistant_id}`): its settings become the defaults for every turn and the
request body still overrides field by field (a field is "set" when present in the JSON).

| Method | Endpoint | Notes |
|---|---|---|
| `GET`    | `/assistants?page=&page_size=` | all assistants of the active org |
| `POST`   | `/assistants` | **201**; any member; `model` / `tool_names` / `collection_ids` validated |
| `GET`    | `/assistants/{id}` | |
| `PATCH`  | `/assistants/{id}` | creator or org admin (**403** otherwise); unknown fields → **422** |
| `DELETE` | `/assistants/{id}` | creator or org admin; sessions keep working with plain defaults |

```json
{ "id": "…", "org_id": "…", "name": "Support bot", "description": "…", "avatar": "🤖",
  "system_prompt": "You are …", "model": "gpt-4o-mini", "tool_names": ["calculator"],
  "collection_ids": ["…"], "use_rag": true, "use_tools": true, "temperature": 0.3,
  "memory_enabled": true, "is_default": false, "created_by": "…", "created_at": "…", "updated_at": "…" }
```
`is_default: true` is exclusive per org (setting it clears the previous default). A session's
own `system_prompt` wins over the assistant's.

---

## Memory (long-term, per user)

Durable facts, preferences and standing instructions about the user, recalled by similarity
into every turn (`MEMORY_ENABLED`, `MEMORY_TOP_K`; instructions are always included) and
**extracted automatically** after turns by a small model call. Recall and extraction need the
server setting, the user (`PATCH /auth/me {"memory_enabled": false}` opts out) and the
session's assistant (`memory_enabled`) to allow it. Memories live in the user's personal org or
the org they were learned in.

| Method | Endpoint | Body |
|---|---|---|
| `GET`    | `/memories?page=&page_size=` | |
| `POST`   | `/memories` | `{content, kind?: fact\|preference\|instruction, importance?: 1..5}` → **201**; **409** near-duplicate |
| `PATCH`  | `/memories/{id}` | `{content?, kind?, importance?}` |
| `DELETE` | `/memories/{id}` · `DELETE /memories` | one / all |

`MemoryRead`: `{id, content, kind, importance, source_message_id, created_at, last_used_at}`.
The memories injected into a reply are listed in `assistant_message.meta.memories_used`
(ids; always present, `[]` when none).

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

`SessionRead` carries `org_id`, `assistant_id` and `active_leaf_id` (the message the
conversation currently ends at — see *Branches*).

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

**Idempotency.** Send `Idempotency-Key: <uuid>` (one per send attempt, reused on retries) on
`/messages`, `/messages/stream`, `/messages/background` and `/messages/{id}/edit`. A retry
within `IDEMPOTENCY_TTL_SECONDS` (24 h) does not create a second turn: blocking requests get
the original response, streams and background turns replay the original generation. A retry
while the first attempt is still running → **409**. Keys are scoped to the user.

`MessageRead` carries `parent_id` (the message tree) and, in history and sibling responses,
`siblings: {index, count}` when the message has alternatives (edits of a user message,
regenerations of a reply); `meta.feedback` mirrors the caller's rating.

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
`: connected` comment frame, and then one JSON frame per event, each with an SSE `id:` (the
frame's sequence number in the generation log — keep the last one you saw):

```
id: 7\ndata: {"type":"token","data":"Hel","generation_id":"…","seq":7}\n\n
```

Every frame carries `generation_id`; the `user_message`, `done`, `assistant_message`,
`approval_required` and `error` payloads carry it inside `data` too.

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

**Disconnects do not stop the turn.** The generation runs detached from the connection and
every frame is written to a Redis-backed log (kept `STREAM_EVENTS_TTL_SECONDS`, 1 h). A client
that loses its connection re-attaches with the events endpoint below and receives exactly the
frames it missed; the reply is persisted either way. **Stopping is explicit:**
`POST /sessions/{id}/generations/{generation_id}/stop` (or the WebSocket `stop` frame) ends the
model call; the partial reply is persisted with `meta.status = "interrupted"`,
`interrupt_reason = "stop"` and the stream still finishes with `done` + `assistant_message`
(`interrupted: true`). Generations also time out after `GENERATION_TIMEOUT_SECONDS`
(`interrupt_reason = "timeout"`).

### Background turns and resumable streams

| Method | Endpoint | Notes |
|---|---|---|
| `POST` | `/sessions/{id}/messages/background` | `SendMessageRequest` → **202** `{generation_id, user_message}`; the reply is produced even if nobody is listening |
| `GET`  | `/sessions/{id}/generations/{generation_id}/events?after=<seq>` | SSE: replays frames with `seq > after` (also honours `Last-Event-ID`), then follows live until `assistant_message`; **404** unknown / expired |
| `GET`  | `/sessions/{id}/generations/active` | `{generation_id, started_at, status}` of the in-flight turn, or `null` — call it when a chat screen opens |
| `POST` | `/sessions/{id}/generations/{generation_id}/stop` | stop the turn (partial reply persisted); idempotent |

Mobile flow: start with `/messages/background` (or `/messages/stream`), remember
`generation_id` and the last `id:`, and on resume call `/generations/{generation_id}/events`
with `Last-Event-ID`.

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

### Branches: edit, regenerate, siblings

Messages form a tree. Editing a user message or regenerating a reply creates a **sibling**
instead of overwriting; the session's `active_leaf_id` selects which branch history shows.

| Method | Endpoint | Notes |
|---|---|---|
| `POST` | `/sessions/{id}/messages/{message_id}/edit` | `SendMessageRequest` (`content` required), `Accept: text/event-stream` → same frames as `/stream`; the edit becomes a sibling of `message_id` (same `parent_id`), is answered, and its branch becomes active. Only user messages (**422** otherwise) |
| `POST` | `/sessions/{id}/messages/regenerate` | `{temperature?, max_tokens?}` → `RegenerateResponse`; a new reply to the last user message of the active branch, **the previous reply is kept as a sibling**, same RAG/tool settings |
| `POST` | `/sessions/{id}/messages/regenerate/stream` | same, streamed |
| `GET`  | `/sessions/{id}/messages/{message_id}/siblings` | `{items: [MessageRead…], active_index}` — all alternatives of a message, oldest first |
| `POST` | `/sessions/{id}/branch` | `{leaf_message_id}` → `HistoryResponse` of the newly active branch (the chosen message's newest descendant becomes the leaf) |

### `GET /sessions/{id}/messages?cursor=ISO8601&limit=50`
Cursor-paginated history of the **active branch** (linear, oldest first; newest fetched first
via `cursor=created_at`). Messages with alternatives carry `siblings: {index, count}`.

### Feedback

`POST /sessions/{id}/messages/{message_id}/feedback` `{"rating": 1 | -1, "comment": "…"}` →
`{message_id, session_id, user_id, rating, comment, created_at}` (upsert, assistant messages
only). `DELETE …/feedback` removes it. The rating is mirrored in `MessageRead.meta.feedback`;
server admins list all feedback at `GET /admin/feedback`.

### `POST /search`
```json
{ "query": "invoice", "limit": 20 }
```
Full-text (stemmed, ranked) search over the user's messages in the active org. Results carry `highlight`
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
2. Send an auth frame within 10 s: `{"type":"auth","token":"<jwt>","org_id":"<uuid>"?}` or
   `{"type":"auth","api_key":"<key>"}` (`org_id` plays the role of `X-Org-Id`).
3. Receive `{"type":"auth_ok","data":{"user_id":"…","org_id":"…"}}`, or an `error` frame
   followed by close code 1008.

Query-string credentials (`?token=` / `?api_key=`) are still accepted for older clients but are
deprecated (they end up in access logs).

Client → server:
```json
{"type":"user_message","content":"hi","use_rag":true,"use_tools":false,"tool_names":null,
 "model":null,"collection_ids":null,"temperature":null,"max_tokens":null,
 "edit_of":"<user message id>"?,"idempotency_key":"<uuid>"?}
{"type":"approval","approval_id":"…","decision":"approve","tool_call_ids":null}
{"type":"regenerate","temperature":null,"max_tokens":null}
{"type":"stop","generation_id":"…"?}
{"type":"ping"}
```

Server → client: the same event frames as the SSE stream (each with `generation_id` and
`seq`), plus `auth_ok` and `pong`. Only fields present in a `user_message` frame override the
session assistant's defaults.

- `stop` ends the in-flight generation (the current one when `generation_id` is omitted); the
  server persists the partial reply and still sends `done` and `assistant_message` with
  `interrupted: true`.
- A `user_message` / `approval` / `regenerate` while another generation is running →
  `{"type":"error","data":{"code":"busy"}}`.
- Disconnecting mid-generation does **not** stop it: reconnect and replay from
  `GET /sessions/{id}/generations/{generation_id}/events?after=<seq>`.

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
