import 'package:collection/collection.dart';

enum MessageRole {
  user,
  assistant,
  system,
  tool;

  static MessageRole fromString(String s) =>
      MessageRole.values.firstWhereOrNull((r) => r.name == s) ?? MessageRole.assistant;
}

class ChatSession {
  ChatSession({
    required this.id,
    required this.title,
    required this.createdAt,
    required this.updatedAt,
    this.systemPrompt,
    this.summary,
    this.model,
  });

  factory ChatSession.fromJson(Map<String, dynamic> j) => ChatSession(
        id: j['id'] as String,
        title: (j['title'] as String?) ?? 'Untitled',
        systemPrompt: j['system_prompt'] as String?,
        summary: j['summary'] as String?,
        model: j['model'] as String?,
        createdAt: DateTime.parse(j['created_at'] as String),
        updatedAt: DateTime.parse(j['updated_at'] as String),
      );

  final String id;
  final String title;
  final String? systemPrompt;
  final String? summary;
  final String? model;
  final DateTime createdAt;
  final DateTime updatedAt;
}

class Citation {
  const Citation({
    required this.documentId,
    required this.documentName,
    required this.chunkIndex,
    required this.score,
    required this.excerpt,
  });

  factory Citation.fromJson(Map<String, dynamic> j) => Citation(
        documentId: j['document_id'] as String,
        documentName: j['document_name'] as String,
        chunkIndex: (j['chunk_index'] as num).toInt(),
        score: (j['score'] as num).toDouble(),
        excerpt: j['excerpt'] as String,
      );

  final String documentId;
  final String documentName;
  final int chunkIndex;
  final double score;
  final String excerpt;
}

class ToolCall {
  const ToolCall({required this.tool, this.input, this.output});

  factory ToolCall.fromJson(Map<String, dynamic> j) => ToolCall(
        tool: (j['tool'] as String?) ?? 'tool',
        input: j['input'],
        output: j['output'] as String?,
      );

  final String tool;
  final Object? input;
  final String? output;
}

class ChatMessage {
  ChatMessage({
    required this.id,
    required this.sessionId,
    required this.role,
    required this.content,
    required this.createdAt,
    this.tokens = 0,
    this.citations = const [],
    this.toolCalls = const [],
    this.streaming = false,
  });

  factory ChatMessage.fromJson(Map<String, dynamic> j) {
    final meta = (j['meta'] as Map?)?.cast<String, dynamic>() ?? const {};
    final cits = (meta['citations'] as List? ?? const [])
        .cast<Map>()
        .map((m) => Citation.fromJson(m.cast<String, dynamic>()))
        .toList();
    final tcs = (meta['tool_calls'] as List? ?? const [])
        .cast<Map>()
        .map((m) => ToolCall.fromJson(m.cast<String, dynamic>()))
        .toList();
    return ChatMessage(
      id: j['id'] as String,
      sessionId: j['session_id'] as String,
      role: MessageRole.fromString(j['role'] as String),
      content: j['content'] as String,
      tokens: (j['tokens'] as num?)?.toInt() ?? 0,
      createdAt: DateTime.parse(j['created_at'] as String),
      citations: cits,
      toolCalls: tcs,
    );
  }

  /// Build a placeholder assistant message for streaming. Will be replaced
  /// with the persisted server message once the stream completes.
  factory ChatMessage.streamingPlaceholder({required String sessionId}) => ChatMessage(
        id: 'streaming-${DateTime.now().microsecondsSinceEpoch}',
        sessionId: sessionId,
        role: MessageRole.assistant,
        content: '',
        createdAt: DateTime.now(),
        streaming: true,
      );

  final String id;
  final String sessionId;
  final MessageRole role;
  final String content;
  final int tokens;
  final DateTime createdAt;
  final List<Citation> citations;
  final List<ToolCall> toolCalls;
  final bool streaming;

  ChatMessage copyWith({
    String? id,
    String? content,
    List<Citation>? citations,
    List<ToolCall>? toolCalls,
    bool? streaming,
  }) =>
      ChatMessage(
        id: id ?? this.id,
        sessionId: sessionId,
        role: role,
        content: content ?? this.content,
        tokens: tokens,
        createdAt: createdAt,
        citations: citations ?? this.citations,
        toolCalls: toolCalls ?? this.toolCalls,
        streaming: streaming ?? this.streaming,
      );
}

/// Discriminated streaming event from `/messages/stream`.
sealed class StreamEvent {
  const StreamEvent();

  factory StreamEvent.fromJson(Map<String, dynamic> j) {
    final type = j['type'] as String;
    final data = j['data'];
    switch (type) {
      case 'user_message':
        return UserMessageEvent(data: (data as Map).cast<String, dynamic>());
      case 'citations':
        return CitationsEvent(
          citations: (data as List)
              .cast<Map>()
              .map((m) => Citation.fromJson(m.cast<String, dynamic>()))
              .toList(),
        );
      case 'token':
        return TokenEvent(token: data as String);
      case 'tool_call':
        return ToolCallEvent(data: (data as Map).cast<String, dynamic>());
      case 'usage':
        return UsageEvent(data: (data as Map?)?.cast<String, dynamic>() ?? const {});
      case 'done':
        return DoneEvent(data: (data as Map).cast<String, dynamic>());
      case 'assistant_message':
        return AssistantMessageEvent(data: (data as Map).cast<String, dynamic>());
      case 'error':
        return ErrorEvent(message: (data is Map) ? (data['message'] as String? ?? 'error') : 'error');
      default:
        return UnknownEvent(type: type);
    }
  }
}

class UserMessageEvent extends StreamEvent {
  const UserMessageEvent({required this.data});
  final Map<String, dynamic> data;
}

class CitationsEvent extends StreamEvent {
  const CitationsEvent({required this.citations});
  final List<Citation> citations;
}

class TokenEvent extends StreamEvent {
  const TokenEvent({required this.token});
  final String token;
}

class ToolCallEvent extends StreamEvent {
  const ToolCallEvent({required this.data});
  final Map<String, dynamic> data;
}

class UsageEvent extends StreamEvent {
  const UsageEvent({required this.data});
  final Map<String, dynamic> data;
}

class DoneEvent extends StreamEvent {
  const DoneEvent({required this.data});
  final Map<String, dynamic> data;
}

class AssistantMessageEvent extends StreamEvent {
  const AssistantMessageEvent({required this.data});
  final Map<String, dynamic> data;
}

class ErrorEvent extends StreamEvent {
  const ErrorEvent({required this.message});
  final String message;
}

class UnknownEvent extends StreamEvent {
  const UnknownEvent({required this.type});
  final String type;
}
