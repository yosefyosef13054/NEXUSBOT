# Flutter Integration Guide

This backend is API-first. Drop the snippets below into any Flutter app (mobile or web).

> Recommended packages:
> ```yaml
> dependencies:
>   dio: ^5.7.0
>   web_socket_channel: ^3.0.0
>   flutter_client_sse: ^2.0.2   # or roll your own with `http`
>   shared_preferences: ^2.3.0
> ```

---

## 1. API client (Dio + JWT refresh)

```dart
// lib/api/api_client.dart
import 'package:dio/dio.dart';
import 'package:shared_preferences/shared_preferences.dart';

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
  String? _accessToken;
  String? _refreshToken;

  Future<void> loadTokens() async {
    final prefs = await SharedPreferences.getInstance();
    _accessToken = prefs.getString('access_token');
    _refreshToken = prefs.getString('refresh_token');
  }

  Future<void> setTokens(String access, String refresh) async {
    _accessToken = access;
    _refreshToken = refresh;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('access_token', access);
    await prefs.setString('refresh_token', refresh);
  }

  String? get accessToken => _accessToken;
  String? get refreshToken => _refreshToken;
}

class _AuthInterceptor extends Interceptor {
  _AuthInterceptor(this.client);
  final ApiClient client;

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    if (client.accessToken != null && options.headers['Authorization'] == null) {
      options.headers['Authorization'] = 'Bearer ${client.accessToken}';
    }
    handler.next(options);
  }

  @override
  void onError(DioException err, ErrorInterceptorHandler handler) async {
    if (err.response?.statusCode == 401 && client.refreshToken != null) {
      try {
        final r = await client.dio.post('/auth/refresh',
            data: {'refresh_token': client.refreshToken});
        await client.setTokens(r.data['access_token'], r.data['refresh_token']);
        final req = err.requestOptions;
        req.headers['Authorization'] = 'Bearer ${client.accessToken}';
        final retried = await client.dio.fetch(req);
        return handler.resolve(retried);
      } catch (_) { /* fall through */ }
    }
    handler.next(err);
  }
}
```

---

## 2. Auth

```dart
Future<void> login(ApiClient api, String email, String pass) async {
  final r = await api.dio.post('/auth/login',
      data: {'email': email, 'password': pass});
  await api.setTokens(r.data['access_token'], r.data['refresh_token']);
}
```

---

## 3. Create a session + send a message

```dart
Future<String> createSession(ApiClient api) async {
  final r = await api.dio.post('/sessions', data: {'title': null});
  return r.data['id'] as String;
}

Future<Map<String, dynamic>> sendMessage(
  ApiClient api, String sessionId, String content) async {
  final r = await api.dio.post('/sessions/$sessionId/messages',
      data: {'content': content, 'use_rag': true});
  return r.data;
}
```

---

## 4. Server-Sent Events streaming

```dart
import 'package:flutter_client_sse/flutter_client_sse.dart';
import 'dart:convert';

void streamMessage(ApiClient api, String sessionId, String content,
    void Function(Map<String, dynamic>) onEvent) {
  SSEClient.subscribeToSSE(
    method: SSERequestType.POST,
    url: '${api.baseUrl}/sessions/$sessionId/messages/stream',
    header: {
      'Authorization': 'Bearer ${api.accessToken}',
      'Content-Type': 'application/json',
      'Accept': 'text/event-stream',
    },
    body: {'content': content, 'use_rag': true},
  ).listen((event) {
    if (event.data == null || event.data!.isEmpty) return;
    onEvent(jsonDecode(event.data!));
  });
}
```

Render UI as events arrive:

```dart
streamMessage(api, sid, 'Hello!', (evt) {
  switch (evt['type']) {
    case 'token':       append(evt['data']);            break;
    case 'citations':   showCitations(evt['data']);     break;
    case 'tool_call':   showToolBadge(evt['data']);     break;
    case 'done':        markComplete(evt['data']);      break;
    case 'error':       showError(evt['data']);         break;
  }
});
```

---

## 5. WebSocket

```dart
import 'package:web_socket_channel/web_socket_channel.dart';
import 'dart:convert';

class ChatSocket {
  ChatSocket(this.base, this.token, this.sessionId);
  final String base;     // e.g. ws://10.0.2.2:8000/api/v1
  final String token;
  final String sessionId;
  WebSocketChannel? _ch;

  Stream<Map<String, dynamic>> connect() {
    final uri = Uri.parse('$base/ws/sessions/$sessionId?token=$token');
    _ch = WebSocketChannel.connect(uri);
    return _ch!.stream.map((raw) => jsonDecode(raw as String) as Map<String, dynamic>);
  }

  void send(String content, {bool useRag = true, bool useTools = false}) {
    _ch?.sink.add(jsonEncode({
      'type': 'user_message',
      'content': content,
      'use_rag': useRag,
      'use_tools': useTools,
    }));
  }

  void ping() => _ch?.sink.add(jsonEncode({'type': 'ping'}));
  Future<void> close() async => _ch?.sink.close();
}
```

Auto-reconnect skeleton:

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
Future<Map<String, dynamic>> uploadDocument(
  ApiClient api, String path, {String? sessionId}) async {
  final form = FormData.fromMap({
    'file': await MultipartFile.fromFile(path),
    if (sessionId != null) 'session_id': sessionId,
  });
  final r = await api.dio.post('/documents', data: form);
  return r.data; // contains document.id + status (pending)
}

Future<bool> waitUntilReady(ApiClient api, String docId,
    {Duration interval = const Duration(seconds: 2),
     int maxAttempts = 60}) async {
  for (var i = 0; i < maxAttempts; i++) {
    final r = await api.dio.get('/documents/$docId');
    final s = r.data['status'] as String;
    if (s == 'ready') return true;
    if (s == 'failed') return false;
    await Future.delayed(interval);
  }
  return false;
}
```

---

## 7. Error envelope

Every error from the API has the same shape:

```dart
class ApiError {
  ApiError(this.code, this.message, [this.details]);
  final String code, message;
  final Map<String, dynamic>? details;

  factory ApiError.from(DioException e) {
    final body = e.response?.data;
    if (body is Map && body['error'] is Map) {
      final m = body['error'] as Map;
      return ApiError(m['code'] ?? 'unknown', m['message'] ?? '',
          (m['details'] as Map?)?.cast<String, dynamic>());
    }
    return ApiError('network_error', e.message ?? 'Network error');
  }
}
```

Common codes you'll handle:
- `unauthorized`, `forbidden`, `not_found`
- `rate_limited` (back off using `details.window_seconds`)
- `validation_error`
- `ai_provider_error` (transient — retry once)

---

## 8. Production tips for Flutter

- Use `X-API-Key` for server-to-server / desktop integrations; JWT for end-user logins.
- On Android emulator, `http://10.0.2.2:8000` reaches the host.
- For SSE on iOS, ensure `Info.plist` allows the HTTP host (or use TLS).
- Keep the SSE/WS event handlers idempotent — UI re-renders shouldn't break if the same `done` arrives twice.
- Persist `session_id` alongside chat state so refresh restores the conversation.
