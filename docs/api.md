# API Reference

Base URL: `http://localhost:8000/api/v1`

All authenticated endpoints accept **either**:
- `Authorization: Bearer <access_token>` (JWT, short-lived), **or**
- `X-API-Key: <plaintext_key>` (long-lived, scoped to the issuing user)

All errors are returned in a single, stable envelope:

```json
{ "error": { "code": "validation_error", "message": "…", "details": {} } }
```

---

## Auth

### `POST /auth/register`
```json
{ "email": "a@b.com", "password": "min-8-chars", "full_name": "Optional" }
```
**201** → `UserRead`

### `POST /auth/login`
```json
{ "email": "a@b.com", "password": "…" }
```
**200** → `{ access_token, refresh_token, token_type, expires_in }`

### `POST /auth/refresh`
```json
{ "refresh_token": "…" }
```

### `GET /auth/me`
Returns the current user.

### `POST /auth/api-keys`
```json
{ "name": "flutter-prod" }
```
**201** → returns the **plaintext key once** (`plaintext_key`). Store it client-side; the server only keeps a SHA-256 hash.

### `DELETE /auth/api-keys/{key_id}`
Revokes the key.

---

## Sessions

### `POST /sessions`
```json
{ "title": null, "system_prompt": "You are a helpful…", "model": "gpt-4o-mini" }
```

### `GET /sessions?page=1&page_size=20`
Paginated list of the user's sessions.

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
**200** → `SendMessageResponse` with `user_message`, `assistant_message`, `citations[]`, `tool_calls[]`, `usage`.

### `POST /sessions/{id}/messages/stream` — Server-Sent Events

Same body as above. The response is `text/event-stream`. Each frame:

```
data: {"type":"token","data":"Hel"}\n\n
```

Event types (`type` field):

| type | data |
|---|---|
| `user_message` | message metadata |
| `citations` | retrieved chunks for this turn |
| `token` | next assistant token (string) |
| `tool_call` | tool invocation lifecycle |
| `usage` | provider-reported token counts (best effort) |
| `done` | full assembled text + latency + model |
| `assistant_message` | final persisted assistant message |
| `error` | structured error |

### `POST /sessions/{id}/messages/regenerate`
```json
{ "temperature": 0.7 }
```
Soft-deletes the previous assistant turn and generates a new one against the same user input.

### `GET /sessions/{id}/messages?cursor=ISO8601&limit=50`
Cursor-paginated history (newest fetched first via `cursor=created_at`).

### `POST /search`
```json
{ "query": "invoice", "limit": 20 }
```

---

## Documents (RAG)

### `POST /documents` — multipart
- `file` (UploadFile, required)
- `session_id` (UUID, optional — scope the doc to a single chat)

**202 Accepted** → `{ document, queued: true }`. The document is queued on Celery; poll `GET /documents/{id}` until `status == "ready"`.

### `GET /documents?page=1&page_size=20`
### `GET /documents/{id}`
### `DELETE /documents/{id}`

---

## WebSocket

`ws://host/api/v1/ws/sessions/{session_id}?token=<jwt>` *or* `?api_key=<key>`

Client → server:
```json
{"type":"user_message","content":"hi","use_rag":true,"use_tools":false}
{"type":"ping"}
```

Server → client: same event shape as the SSE stream.

---

## Rate limits

Default `60 req/min` per principal on `/messages` and `/messages/stream`. Configure via `RATE_LIMIT_PER_MINUTE`. Exceeding the quota returns:

```json
{ "error": { "code": "rate_limited", "message": "Too many requests",
             "details": { "limit": 60, "window_seconds": 60 } } }
```
