import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/preferences.dart';
import 'chat_models.dart';
import 'chat_repository.dart';

/// Live state for a single chat screen.
class ChatState {
  const ChatState({
    required this.messages,
    required this.sending,
    this.useRag = true,
    this.useTools = false,
    this.error,
  });

  final List<ChatMessage> messages;
  final bool sending;
  final bool useRag;
  final bool useTools;
  final String? error;

  ChatState copyWith({
    List<ChatMessage>? messages,
    bool? sending,
    bool? useRag,
    bool? useTools,
    String? error,
    bool clearError = false,
  }) =>
      ChatState(
        messages: messages ?? this.messages,
        sending: sending ?? this.sending,
        useRag: useRag ?? this.useRag,
        useTools: useTools ?? this.useTools,
        error: clearError ? null : (error ?? this.error),
      );

  static const empty = ChatState(messages: [], sending: false);
}

class ChatController extends StateNotifier<ChatState> {
  ChatController(this._ref, this.sessionId) : super(ChatState.empty) {
    final prefs = _ref.read(prefsProvider);
    state = state.copyWith(useRag: prefs.ragByDefault, useTools: true);
    unawaited(_load());
  }

  final Ref _ref;
  final String sessionId;
  StreamSubscription<StreamEvent>? _activeStream;

  Future<void> _load() async {
    try {
      final repo = await _ref.read(chatRepositoryProvider.future);
      final history = await repo.loadHistory(sessionId);
      state = state.copyWith(messages: history);
    } catch (e) {
      state = state.copyWith(error: e.toString());
    }
  }

  void toggleRag(bool v) => state = state.copyWith(useRag: v);
  void toggleTools(bool v) => state = state.copyWith(useTools: v);

  /// Cancel an in-flight stream (user tapped the stop button in the composer).
  Future<void> stop() async {
    await _activeStream?.cancel();
    _activeStream = null;
    _replaceLastAssistant((m) => m.copyWith(streaming: false));
    state = state.copyWith(sending: false);
  }

  Future<void> send(String content) async {
    final trimmed = content.trim();
    if (trimmed.isEmpty || state.sending) return;

    // Optimistically append the user message + a streaming placeholder.
    final optimisticUser = ChatMessage(
      id: 'pending-user-${DateTime.now().microsecondsSinceEpoch}',
      sessionId: sessionId,
      role: MessageRole.user,
      content: trimmed,
      createdAt: DateTime.now(),
    );
    final placeholder = ChatMessage.streamingPlaceholder(sessionId: sessionId);
    state = state.copyWith(
      sending: true,
      clearError: true,
      messages: [...state.messages, optimisticUser, placeholder],
    );

    try {
      final repo = await _ref.read(chatRepositoryProvider.future);
      final prefs = _ref.read(prefsProvider);

      // Non-streaming path — fall through to the regular POST endpoint.
      if (!prefs.streamingEnabled) {
        final assistant = await repo.sendBlocking(
          sessionId: sessionId,
          content: trimmed,
          useRag: state.useRag,
          useTools: state.useTools,
        );
        _replaceLastAssistant((_) => assistant);
        state = state.copyWith(sending: false);
        return;
      }

      final buffer = StringBuffer();
      final tools = <ToolCall>[];

      _activeStream = repo
          .streamMessage(
            sessionId: sessionId,
            content: trimmed,
            transport: prefs.transport,
            useRag: state.useRag,
            useTools: state.useTools,
          )
          .listen((evt) {
        switch (evt) {
          case TokenEvent(:final token):
            buffer.write(token);
            _replaceLastAssistant(
              (m) => m.copyWith(content: buffer.toString(), streaming: true),
            );
          case CitationsEvent(:final citations):
            _setCitations(citations);
          case ToolCallEvent(:final data):
            final phase = data['phase'] as String?;
            if (phase == 'end') {
              tools.add(ToolCall(
                tool: data['tool'] as String? ?? 'tool',
                input: data['input'],
                output: data['output'] as String?,
              ));
              _replaceLastAssistant((m) => m.copyWith(toolCalls: List.of(tools)));
            }
          case UserMessageEvent(:final data):
            _replaceFirstPendingUser(data);
          case AssistantMessageEvent(:final data):
            // Final canonical version straight from the server.
            _replaceLastAssistant(
              (_) => ChatMessage(
                id: data['id'] as String,
                sessionId: sessionId,
                role: MessageRole.assistant,
                content: data['content'] as String,
                tokens: 0,
                createdAt: DateTime.tryParse(data['created_at'] as String? ?? '') ?? DateTime.now(),
                citations: (data['citations'] as List? ?? const [])
                    .cast<Map>()
                    .map((m) => Citation.fromJson(m.cast<String, dynamic>()))
                    .toList(),
                toolCalls: (data['tool_calls'] as List? ?? const [])
                    .cast<Map>()
                    .map((m) => ToolCall.fromJson(m.cast<String, dynamic>()))
                    .toList(),
              ),
            );
            state = state.copyWith(sending: false);
          case ErrorEvent(:final message):
            _replaceLastAssistant((m) => m.copyWith(content: '⚠ $message', streaming: false));
            state = state.copyWith(sending: false, error: message);
          case DoneEvent():
          case UsageEvent():
          case UnknownEvent():
            break;
        }
      }, onDone: () {
        if (state.sending) state = state.copyWith(sending: false);
      }, onError: (e) {
        state = state.copyWith(sending: false, error: e.toString());
      });
    } catch (e) {
      state = state.copyWith(sending: false, error: e.toString());
    }
  }

  void _replaceLastAssistant(ChatMessage Function(ChatMessage) update) {
    final msgs = List<ChatMessage>.of(state.messages);
    for (var i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role == MessageRole.assistant) {
        msgs[i] = update(msgs[i]);
        state = state.copyWith(messages: msgs);
        return;
      }
    }
  }

  void _replaceFirstPendingUser(Map<String, dynamic> data) {
    final msgs = List<ChatMessage>.of(state.messages);
    for (var i = 0; i < msgs.length; i++) {
      if (msgs[i].id.startsWith('pending-user-')) {
        msgs[i] = ChatMessage(
          id: data['id'] as String,
          sessionId: sessionId,
          role: MessageRole.user,
          content: data['content'] as String,
          createdAt: DateTime.tryParse(data['created_at'] as String? ?? '') ?? msgs[i].createdAt,
        );
        state = state.copyWith(messages: msgs);
        return;
      }
    }
  }

  void _setCitations(List<Citation> citations) {
    _replaceLastAssistant((m) => m.copyWith(citations: citations));
  }

  Future<void> regenerate() async {
    if (state.sending) return;
    state = state.copyWith(sending: true);
    try {
      final repo = await _ref.read(chatRepositoryProvider.future);
      final msg = await repo.regenerate(sessionId);
      final msgs = List<ChatMessage>.of(state.messages);
      // Replace the last assistant message
      for (var i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role == MessageRole.assistant) {
          msgs[i] = msg;
          break;
        }
      }
      state = state.copyWith(messages: msgs, sending: false);
    } catch (e) {
      state = state.copyWith(sending: false, error: e.toString());
    }
  }

  @override
  void dispose() {
    _activeStream?.cancel();
    super.dispose();
  }
}

final chatControllerProvider =
    StateNotifierProvider.autoDispose.family<ChatController, ChatState, String>(
  (ref, sessionId) => ChatController(ref, sessionId),
);
