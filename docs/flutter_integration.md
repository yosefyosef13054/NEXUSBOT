# Flutter Integration Guide

This backend is API-first. Drop the snippets below into any Flutter app (mobile, desktop, web).
The official client, [NEXUSBOT-APP](https://github.com/yosefyosef13054/NEXUSBOT-APP), implements
all of this with Riverpod and go_router if you prefer a complete reference.

> Recommended packages:
> ```yaml
> dependencies:
>   dio: ^5.7.0
>   web_socket_channel: ^3.0.0
>   flutter_secure_storage: ^9.2.0   # tokens belong in the Keychain / Keystore
> ```
> No SSE package is needed: Dio can read the event stream itself (section 4), which also means
> the token-refresh interceptor applies to streaming requests.

---

## 1. API client (Dio + single-flight JWT refresh)

Refresh tokens **rotate**: every `/auth/refresh` returns a new pair and retires the old one, and a
retired token presented again later revokes the whole login. Two consequences for the client:

- always store the returned `refresh_token`;
- never run two refreshes at once — share one in-flight refresh between concurrent 401s.

```dart
// lib/api/api_client.dart
import 'dart:async';
import 'package:dio/dio.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

class ApiClient {
  ApiClient(this.baseUrl) {
    dio = Dio(BaseOptions(
      baseUrl: baseUrl,
      connectTimeout: const Duration(seconds: 15),
      receiveTimeout: const Duration(seconds: 60),
      headers: {'Content-Type': 'application/json'},
    ));
    dio.interceptors.add(_AuthInterceptor(this));
  }

  final String baseUrl;
  late final Dio dio;
  final _store = const FlutterSecureStorage();
  Future<String?>? _refreshInFlight;

  Future<String?> accessToken() => _store.read(key: 'access_token');
  Future<String?> refreshToken() => _store.read(key: 'refresh_token');

  Future<void> setTokens(String access, String refresh) async {
    await _store.write(key: 'access_token', value: access);
    await _store.write(key: 'refresh_token', value: refresh);
  }

  Future<void> clearTokens() async {
    await _store.delete(key: 'access_token');
    await _store.delete(key: 'refresh_token');
  }

  /// Exchanges the refresh token for a new pair. Concurrent callers share one call.
  Future<String?> refresh() => _refreshInFlight ??= _doRefresh().whenComplete(() => _refreshInFlight = null);

  Future<String?> _doRefresh() async {
    final current = await refreshToken();
    if (current == null) return null;
    try {
      // A bare Dio: no interceptors, so a failing refresh can never recurse.
      final r = await Dio(BaseOptions(baseUrl: baseUrl))
          .post('/auth/refresh', data: {'refresh_token': current});
      await setTokens(r.data['access_token'], r.data['refresh_token']);
      return r.data['access_token'] as String;
    } on DioException catch (e) {
      final status = e.response?.statusCode ?? 0;
      if (status == 401 || (status >= 400 && status < 500 && status != 429)) {
        await clearTokens(); // definitively rejected → sign the user out
      }
      return null; // network / 5xx: keep the tokens, retry later
    }
  }
}

class _AuthInterceptor extends Interceptor {
  _AuthInterceptor(this.client);
  final ApiClient client;

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) async {
    final token = await client.accessToken();
    if (token != null && options.headers['Authorization'] == null) {
      options.headers['Authorization'] = 'Bearer $token';
    }
    handler.next(options);
  }

  @override
  void onError(DioException err, ErrorInterceptorHandler handler) async {
    final req = err.requestOptions;
    final retried = req.extra['retried'] == true;
    if (err.response?.statusCode == 401 && !retried && !req.path.contains('/auth/')) {
      final token = await client.refresh();
      if (token != null) {
        req.headers['Authorization'] = 'Bearer $token';
        req.extra['retried'] = true;
        return handler.resolve(await client.dio.fetch(req));
      }
    }
    handler.next(err);
  }
}
```

---

## 2. Auth lifecycle

```dart
Future<void> login(ApiClient api, String email, String pass) async {
  final r = await api.dio.post('/auth/login', data: {'email': email, 'password': pass});
  await api.setTokens(r.data['access_token'], r.data['refresh_token']);
}

Future<void> logout(ApiClient api) async {
  final refresh = await api.refreshToken();
  if (refresh != null) {
    // Best effort: revoke this device's refresh token server-side.
    try { await api.dio.post('/auth/logout', data: {'refresh_token': refresh}); } catch (_) {}
  }
  await api.clearTokens();
}

Future<void> changePassword(ApiClient api, String current, String next) async {
  final r = await api.dio.post('/auth/change-password',
      data: {'current_password': current, 'new_password': next});
  await api.setTokens(r.data['access_token'], r.data['refresh_token']); // other devices are signed out
}

/// App-store rules require in-app account deletion. Password confirmation is mandatory.
Future<void> deleteAccount(ApiClient api, String password) async {
  await api.dio.delete('/auth/me', data: {'password': password});
  await api.clearTokens();
}
```

---

## 3. Create a session + send a message

```dart
Future<String> createSession(ApiClient api) async {
  final r = await api.dio.post('/sessions', data: {'title': null});
  return r.data['id'] as String;
}

Future<Map<String, dynamic>> sendMessage(ApiClient api, String sessionId, String content) async {
  final r = await api.dio.post('/sessions/$sessionId/messages',
      data: {'content': content, 'use_rag': true});
  return r.data; // user_message, assistant_message, citations, tool_calls, usage
}
```

Titles are generated in the background after the first reply: re-fetch `GET /sessions` a couple
of seconds after a turn completes.

---

## 4. Server-Sent Events streaming (through Dio)

```dart
import 'dart:async';
import 'dart:convert';
import 'package:dio/dio.dart';

/// Parses `data: <json>` frames out of a text/event-stream body.
Stream<Map<String, dynamic>> sseFrames(Stream<List<int>> bytes) async* {
  var buffer = '';
  await for (final chunk in utf8.decoder.bind(bytes)) {
    buffer += chunk;
    while (true) {
      final end = buffer.indexOf('\n\n');
      if (end < 0) break;
      final block = buffer.substring(0, end);
      buffer = buffer.substring(end + 2);
      final data = block
          .split('\n')
          .where((l) => l.startsWith('data:'))
          .map((l) => l.substring(5).trim())
          .join('\n');
      if (data.isNotEmpty) yield jsonDecode(data) as Map<String, dynamic>;
      // Lines starting with ':' are comments (the server sends ': connected' first).
    }
  }
}

/// Returns the event stream plus a token that cancels the generation when the user taps Stop.
(Stream<Map<String, dynamic>>, CancelToken) streamMessage(
    ApiClient api, String sessionId, String content) {
  final cancel = CancelToken();
  final events = () async* {
    final r = await api.dio.post<ResponseBody>(
      '/sessions/$sessionId/messages/stream',
      data: {'content': content, 'use_rag': true},
      cancelToken: cancel,
      options: Options(
        responseType: ResponseType.stream,
        receiveTimeout: const Duration(minutes: 5), // applied between chunks
        headers: {'Accept': 'text/event-stream'},
      ),
    );
    yield* sseFrames(r.data!.stream);
  }();
  return (events, cancel);
}
```

Render UI as events arrive:

```dart
final (events, cancel) = streamMessage(api, sid, 'Hello!');
events.listen((evt) {
  switch (evt['type']) {
    case 'token':             append(evt['data']);                  break;
    case 'citations':         showCitations(evt['data']);           break;
    case 'tool_call':         showToolBadge(evt['data']);           break;
    case 'usage':             showUsage(evt['data']);               break;
    case 'done':              markComplete(evt['data']);            break; // data.interrupted
    case 'assistant_message': replaceWithPersisted(evt['data']);    break;
    case 'error':             showError(evt['data']);               break;
  }
});
// Stop button: cancelling closes the connection. The server cancels the model call and
// persists the partial reply (status "interrupted"), so history stays consistent.
stopButton.onPressed = () => cancel.cancel('user stopped');
```

Note for Flutter **web**: Dio's browser adapter delivers the body only when the response completes,
so tokens do not arrive incrementally over SSE. Use the WebSocket transport on web.

---

## 5. WebSocket

Credentials go in the **first frame**, not the URL.

```dart
import 'dart:async';
import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';

class ChatSocket {
  ChatSocket(this.base, this.sessionId);
  final String base;      // e.g. ws://10.0.2.2:8000/api/v1
  final String sessionId;
  WebSocketChannel? _ch;

  /// Connects, authenticates and returns the event stream (auth_ok / pong filtered out).
  Future<Stream<Map<String, dynamic>>> connect(String accessToken) async {
    _ch = WebSocketChannel.connect(Uri.parse('$base/ws/sessions/$sessionId'));
    final frames = _ch!.stream
        .map((raw) => jsonDecode(raw as String) as Map<String, dynamic>)
        .asBroadcastStream();
    _ch!.sink.add(jsonEncode({'type': 'auth', 'token': accessToken}));
    final first = await frames.first.timeout(const Duration(seconds: 10));
    if (first['type'] != 'auth_ok') {
      throw StateError('auth failed: ${first['data']}'); // server closes with 1008
    }
    return frames.where((e) => e['type'] != 'pong');
  }

  void send(String content, {bool useRag = true, bool useTools = false}) {
    _ch?.sink.add(jsonEncode({
      'type': 'user_message', 'content': content,
      'use_rag': useRag, 'use_tools': useTools, 'tool_names': null, 'temperature': null,
    }));
  }

  /// Cancels the in-flight generation. Keep listening: `done` and `assistant_message`
  /// still arrive, with `interrupted: true` and the partial text.
  void stop() => _ch?.sink.add(jsonEncode({'type': 'stop'}));

  void ping() => _ch?.sink.add(jsonEncode({'type': 'ping'}));
  Future<void> close() async => _ch?.sink.close();
}
```

Sending a second `user_message` while one is in flight yields
`{"type":"error","data":{"code":"busy"}}`; the running generation continues.

Auto-reconnect skeleton (refresh the access token before reconnecting):

```dart
Future<void> withReconnect(Future<void> Function() run) async {
  var backoff = const Duration(seconds: 1);
  while (true) {
    try {
      await run();
      backoff = const Duration(seconds: 1);
    } catch (_) {
      await Future.delayed(backoff);
      backoff = Duration(seconds: (backoff.inSeconds * 2).clamp(1, 30));
    }
  }
}
```

---

## 6. Upload a document for RAG

```dart
Future<Map<String, dynamic>> uploadDocument(ApiClient api, String path, {String? sessionId}) async {
  final form = FormData.fromMap({
    'file': await MultipartFile.fromFile(path),
    if (sessionId != null) 'session_id': sessionId,
  });
  final r = await api.dio.post('/documents', data: form);
  // r.data['queued'] == false → identical content already uploaded; r.data['document'] is the existing one.
  return r.data;
}

Future<bool> waitUntilReady(ApiClient api, String docId,
    {Duration interval = const Duration(seconds: 2), int maxAttempts = 60,
     void Function(int percent)? onProgress}) async {
  for (var i = 0; i < maxAttempts; i++) {
    final r = await api.dio.get('/documents/$docId');
    final s = r.data['status'] as String;
    onProgress?.call((r.data['meta']?['progress'] as num?)?.toInt() ?? 0);
    if (s == 'ready') return true;
    if (s == 'failed') return false;
    await Future.delayed(interval);
  }
  return false;
}
```

Only the allow-listed extensions are accepted, and the bytes must match the extension (a `.pdf`
that is not a PDF is rejected with `validation_error`). The `Content-Type` you send is ignored.

---

## 7. Error envelope

Every error from the API has the same shape, and carries the request id you can quote in bug reports:

```dart
class ApiError {
  ApiError(this.code, this.message, {this.details, this.requestId});
  final String code, message;
  final Map<String, dynamic>? details;
  final String? requestId;

  factory ApiError.from(DioException e) {
    final body = e.response?.data;
    final requestId = e.response?.headers.value('x-request-id');
    if (body is Map && body['error'] is Map) {
      final m = body['error'] as Map;
      return ApiError(m['code'] ?? 'unknown', m['message'] ?? '',
          details: (m['details'] as Map?)?.cast<String, dynamic>(),
          requestId: body['request_id'] as String? ?? requestId);
    }
    return ApiError('network_error', e.message ?? 'Network error', requestId: requestId);
  }
}
```

Common codes you'll handle:
- `unauthorized` (after a failed refresh → go to login), `forbidden`, `not_found`
- `rate_limited` (back off using `details.retry_after_seconds` / the `Retry-After` header)
- `validation_error` (`details.unknown` for unknown tool names, `details.allowed_extensions` for uploads)
- `ai_provider_error` (transient — retry once), `service_unavailable` (ingestion queue down)

---

## 8. Production tips for Flutter

- Use `X-API-Key` for server-to-server / desktop integrations; JWT for end-user logins.
- Decode the access token's `exp` claim and refresh proactively before opening a long stream —
  a token that expires mid-stream cannot be refreshed transparently.
- On Android emulator, `http://10.0.2.2:8000` reaches the host.
- For SSE on iOS, ensure `Info.plist` allows the HTTP host (or use TLS).
- Keep the SSE/WS event handlers idempotent — UI re-renders shouldn't break if the same `done` arrives twice.
- Treat `done.data.text` as authoritative over the tokens you accumulated.
- Put the session id in the route (`/chat/:id`) so a web refresh restores the conversation.

---

## 9. Models, approvals and collections (Phase 2)

```dart
// Which models can this server serve? Show them in a picker; send the id on the session
// or on a single turn (`model` in the message body / WS frame).
final models = (await api.dio.get('/models')).data['items'] as List;

// Scope a chat to one or more knowledge bases (collections).
await api.dio.patch('/sessions/$sid', data: {'meta': {'collection_ids': [collectionId]}});

// Agent approvals: a turn can pause with an `approval_required` event (and end with
// done.status == "awaiting_approval"). Resolve it — the response streams the rest of the
// turn exactly like /messages/stream, and the final assistant_message has the same id as
// the placeholder you already rendered.
final (events, cancel) = streamApproval(api, sid, approvalId, 'approve');

(Stream<Map<String, dynamic>>, CancelToken) streamApproval(
    ApiClient api, String sessionId, String approvalId, String decision) {
  final cancel = CancelToken();
  final events = () async* {
    final r = await api.dio.post<ResponseBody>(
      '/sessions/$sessionId/approvals/$approvalId',
      data: {'decision': decision},
      cancelToken: cancel,
      options: Options(responseType: ResponseType.stream, headers: {'Accept': 'text/event-stream'}),
    );
    yield* sseFrames(r.data!.stream);
  }();
  return (events, cancel);
}
```

Rendering tips:
- `role == "tool"` history rows are tool runs (`meta.tool`, `meta.input`, `meta.status`): show
  them as collapsible chips between the user message and the reply, not as bubbles.
- Citations carry `page`, `section`, `start_index`/`end_index` and `chunk_id`; fetch the text
  with `GET /documents/{document_id}/chunks/{chunk_id}` to show the source span.
- After an app restart, `GET /sessions/{id}/approvals?status=pending` tells you which turns
  are still waiting for a decision.

---

## 10. Workspaces, assistants, memory, branching and resumable streams (Phase 3)

### Active workspace
Every request acts inside one organization. Keep the chosen org id in local prefs and send it
as `X-Org-Id` on every request (SSE included); for WebSocket put `"org_id"` in the `auth`
frame. Switching org must invalidate sessions, documents, collections, assistants and usage.

```dart
api.dio.options.headers['X-Org-Id'] = activeOrgId;         // null → personal org
final orgs = (await api.dio.get('/orgs')).data['items'];     // {id, name, role, member_count, settings}
```

### Quotas
Any generation request can fail with **429** `quota_exceeded` when the org budget is spent.
Show a banner and disable the composer until `details.resets_at`; `GET /usage/me` returns
month-to-date usage and the remaining budget for the banner / settings screen.

### Assistants
`GET /assistants` for the "new chat" picker (preselect `is_default`); create sessions with
`{'assistant_id': id}`. Per-turn request fields still override the assistant's defaults.

### Feedback and memory
```dart
await api.dio.post('/sessions/$sid/messages/$mid/feedback', data: {'rating': -1, 'comment': 'wrong year'});
await api.dio.delete('/sessions/$sid/messages/$mid/feedback');
final memories = (await api.dio.get('/memories')).data['items'];      // Memory screen
await api.dio.patch('/auth/me', data: {'memory_enabled': false});     // opt-out toggle
```
`assistant_message.meta.memories_used` lists the memory ids a reply drew on.

### Branching
`MessageRead` carries `parent_id` and `siblings {index, count}`; history is the active branch.

```dart
// Edit: same frames as /messages/stream; the new branch becomes active.
api.dio.post<ResponseBody>('/sessions/$sid/messages/$mid/edit', data: {'content': edited}, ...);
// "‹ 2/3 ›" switcher: list the alternatives, then make one active.
final sibs = (await api.dio.get('/sessions/$sid/messages/$mid/siblings')).data;   // {items, active_index}
final branch = (await api.dio.post('/sessions/$sid/branch', data: {'leaf_message_id': chosenId})).data; // HistoryResponse
// Regenerate keeps the old reply as a sibling:
api.dio.post<ResponseBody>('/sessions/$sid/messages/regenerate/stream', data: {}, ...);
```

### Resumable streams, background turns, idempotency
Each SSE frame now has an `id:` line (the sequence number) and every event carries
`generation_id`. Extend the SSE parser from section 4 to keep the last id:

```dart
// in sseFrames(): track `id:` lines alongside `data:` lines
if (line.startsWith('id:')) lastEventId = int.parse(line.substring(3).trim());
```

- Reconnect after a drop: `GET /sessions/$sid/generations/$gid/events` with header
  `Last-Event-ID: $lastEventId` (or `?after=`) replays the missed frames and follows live.
- When a chat opens: `GET /sessions/$sid/generations/active` → attach if a turn is running.
- Going to background on mobile: start turns with `POST /sessions/$sid/messages/background`
  (202 `{generation_id, user_message}`) and attach with the events endpoint; the reply is
  produced even if the app is suspended.
- Stop explicitly with `POST /sessions/$sid/generations/$gid/stop` (or the WS `stop` frame).
  Closing the connection no longer stops the turn.
- Send `Idempotency-Key: <uuid>` (one per attempt, reused on retries) on `/messages`,
  `/messages/stream`, `/messages/background` and `/messages/{id}/edit`: a retry replays the
  original turn instead of creating a duplicate.
