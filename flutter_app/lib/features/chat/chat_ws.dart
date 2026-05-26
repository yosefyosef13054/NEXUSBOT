import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/web_socket_channel.dart';

import '../../core/env.dart';
import 'chat_models.dart';

/// WebSocket transport for `/api/v1/ws/sessions/{id}`.
///
/// Same event shape as the SSE transport so the chat controller doesn't care
/// which one is in use — the active transport is chosen in [ChatRepository].
class ChatWebSocket {
  ChatWebSocket._(this._channel);

  final WebSocketChannel _channel;
  StreamSubscription<dynamic>? _sub;
  final _events = StreamController<StreamEvent>.broadcast();

  static ChatWebSocket connect({required String sessionId, required String? token}) {
    final uri = Uri.parse(
        '${Env.wsBaseUrl}/ws/sessions/$sessionId${token != null ? "?token=$token" : ""}');
    final channel = WebSocketChannel.connect(uri);
    final ws = ChatWebSocket._(channel);
    ws._listen();
    return ws;
  }

  void _listen() {
    _sub = _channel.stream.listen(
      (raw) {
        try {
          final map = jsonDecode(raw as String) as Map<String, dynamic>;
          _events.add(StreamEvent.fromJson(map));
        } catch (e) {
          _events.add(ErrorEvent(message: 'Bad frame: $e'));
        }
      },
      onError: (e) => _events.add(ErrorEvent(message: '$e')),
      onDone: () => _events.close(),
    );
  }

  Stream<StreamEvent> get events => _events.stream;

  /// Send a user message frame matching the backend's expected schema.
  void sendUserMessage({
    required String content,
    bool useRag = true,
    bool useTools = false,
    List<String>? toolNames,
    double? temperature,
  }) {
    _channel.sink.add(jsonEncode({
      'type': 'user_message',
      'content': content,
      'use_rag': useRag,
      'use_tools': useTools,
      if (toolNames != null) 'tool_names': toolNames,
      if (temperature != null) 'temperature': temperature,
    }));
  }

  Future<void> close() async {
    await _sub?.cancel();
    await _channel.sink.close();
    if (!_events.isClosed) await _events.close();
  }
}
