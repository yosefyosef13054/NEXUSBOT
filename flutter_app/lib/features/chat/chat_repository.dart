import 'dart:async';
import 'dart:convert';

import 'package:flutter_client_sse/constants/sse_request_type_enum.dart';
import 'package:flutter_client_sse/flutter_client_sse.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_client.dart';
import '../../core/env.dart';
import '../../core/preferences.dart';
import '../../core/secure_storage.dart';
import 'chat_models.dart';
import 'chat_ws.dart';

class ChatRepository {
  ChatRepository(this._client, this._tokens);
  final ApiClient _client;
  final TokenStorage _tokens;

  // ---- Sessions ----

  Future<List<ChatSession>> listSessions({int page = 1, int pageSize = 50}) async {
    final res = await _client.request<Map<String, dynamic>>(
      '/sessions',
      query: {'page': page, 'page_size': pageSize},
    );
    return (res.data!['items'] as List)
        .cast<Map>()
        .map((m) => ChatSession.fromJson(m.cast<String, dynamic>()))
        .toList();
  }

  Future<ChatSession> createSession({String? title, String? systemPrompt}) async {
    final res = await _client.request<Map<String, dynamic>>(
      '/sessions',
      method: 'POST',
      data: {
        if (title != null) 'title': title,
        if (systemPrompt != null) 'system_prompt': systemPrompt,
      },
    );
    return ChatSession.fromJson(res.data!);
  }

  Future<void> deleteSession(String id) async {
    await _client.request<Map<String, dynamic>>('/sessions/$id', method: 'DELETE');
  }

  Future<ChatSession> renameSession(String id, String title) async {
    final res = await _client.request<Map<String, dynamic>>(
      '/sessions/$id',
      method: 'PATCH',
      data: {'title': title},
    );
    return ChatSession.fromJson(res.data!);
  }

  // ---- History ----

  Future<List<ChatMessage>> loadHistory(String sessionId, {int limit = 100}) async {
    final res = await _client.request<Map<String, dynamic>>(
      '/sessions/$sessionId/messages',
      query: {'limit': limit},
    );
    return (res.data!['items'] as List)
        .cast<Map>()
        .map((m) => ChatMessage.fromJson(m.cast<String, dynamic>()))
        .toList();
  }

  // ---- Non-streaming send (used when streaming is disabled in prefs) ----

  Future<ChatMessage> sendBlocking({
    required String sessionId,
    required String content,
    bool useRag = true,
    bool useTools = false,
  }) async {
    final res = await _client.request<Map<String, dynamic>>(
      '/sessions/$sessionId/messages',
      method: 'POST',
      data: {'content': content, 'use_rag': useRag, 'use_tools': useTools},
    );
    final m = (res.data!['assistant_message'] as Map).cast<String, dynamic>();
    return ChatMessage.fromJson(m);
  }

  // ---- Streaming ----

  /// Streams structured events from the server while the assistant generates.
  ///
  /// Closes the stream when an `assistant_message` event arrives, or on error.
  /// Transport (SSE vs WebSocket) is chosen by the caller.
  Stream<StreamEvent> streamMessage({
    required String sessionId,
    required String content,
    required StreamTransport transport,
    bool useRag = true,
    bool useTools = false,
    List<String>? toolNames,
    double? temperature,
  }) {
    return switch (transport) {
      StreamTransport.sse => _streamSse(
          sessionId: sessionId,
          content: content,
          useRag: useRag,
          useTools: useTools,
          toolNames: toolNames,
          temperature: temperature,
        ),
      StreamTransport.websocket => _streamWs(
          sessionId: sessionId,
          content: content,
          useRag: useRag,
          useTools: useTools,
          toolNames: toolNames,
          temperature: temperature,
        ),
    };
  }

  Stream<StreamEvent> _streamSse({
    required String sessionId,
    required String content,
    required bool useRag,
    required bool useTools,
    List<String>? toolNames,
    double? temperature,
  }) async* {
    final token = await _tokens.readAccess();
    final controller = StreamController<StreamEvent>();

    final sub = SSEClient.subscribeToSSE(
      method: SSERequestType.POST,
      url: '${Env.apiBaseUrl}/sessions/$sessionId/messages/stream',
      header: {
        if (token != null) 'Authorization': 'Bearer $token',
        'Accept': 'text/event-stream',
        'Content-Type': 'application/json',
        'Cache-Control': 'no-cache',
      },
      body: {
        'content': content,
        'use_rag': useRag,
        'use_tools': useTools,
        if (toolNames != null) 'tool_names': toolNames,
        if (temperature != null) 'temperature': temperature,
      },
    ).listen(
      (event) {
        final raw = event.data;
        if (raw == null || raw.isEmpty) return;
        try {
          final json = jsonDecode(raw) as Map<String, dynamic>;
          final evt = StreamEvent.fromJson(json);
          controller.add(evt);
          if (evt is AssistantMessageEvent || evt is ErrorEvent) {
            controller.close();
          }
        } catch (e) {
          controller.add(ErrorEvent(message: 'Bad frame: $e'));
          controller.close();
        }
      },
      onError: (e) {
        controller.add(ErrorEvent(message: e.toString()));
        controller.close();
      },
      onDone: () {
        if (!controller.isClosed) controller.close();
      },
    );

    controller.onCancel = () async {
      await sub.cancel();
      SSEClient.unsubscribeFromSSE();
    };

    yield* controller.stream;
  }

  Stream<StreamEvent> _streamWs({
    required String sessionId,
    required String content,
    required bool useRag,
    required bool useTools,
    List<String>? toolNames,
    double? temperature,
  }) async* {
    final token = await _tokens.readAccess();
    final ws = ChatWebSocket.connect(sessionId: sessionId, token: token);
    ws.sendUserMessage(
      content: content,
      useRag: useRag,
      useTools: useTools,
      toolNames: toolNames,
      temperature: temperature,
    );

    await for (final evt in ws.events) {
      yield evt;
      if (evt is AssistantMessageEvent || evt is ErrorEvent) {
        await ws.close();
        return;
      }
    }
  }

  // ---- Regenerate ----

  Future<ChatMessage> regenerate(String sessionId, {double? temperature}) async {
    final res = await _client.request<Map<String, dynamic>>(
      '/sessions/$sessionId/messages/regenerate',
      method: 'POST',
      data: {if (temperature != null) 'temperature': temperature},
    );
    final m = (res.data!['assistant_message'] as Map).cast<String, dynamic>();
    return ChatMessage.fromJson(m);
  }
}

final chatRepositoryProvider = FutureProvider<ChatRepository>((ref) async {
  final client = await ref.watch(apiClientProvider.future);
  return ChatRepository(client, ref.watch(tokenStorageProvider));
});

final sessionListProvider = FutureProvider.autoDispose<List<ChatSession>>((ref) async {
  final repo = await ref.watch(chatRepositoryProvider.future);
  return repo.listSessions();
});

final sessionHistoryProvider =
    FutureProvider.autoDispose.family<List<ChatMessage>, String>((ref, sessionId) async {
  final repo = await ref.watch(chatRepositoryProvider.future);
  return repo.loadHistory(sessionId);
});
