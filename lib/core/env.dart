/// Runtime configuration.
///
/// Override at build time:
///   flutter run --dart-define=API_BASE_URL=http://192.168.1.42:8000/api/v1
class Env {
  const Env._();

  /// REST + SSE base URL.
  static const apiBaseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'http://localhost:8000/api/v1',
  );

  /// WebSocket base URL (no trailing slash). Defaults derived from [apiBaseUrl].
  static String get wsBaseUrl {
    final replaced = apiBaseUrl
        .replaceFirst('https://', 'wss://')
        .replaceFirst('http://', 'ws://');
    return replaced;
  }

  /// Set to true to use WebSocket transport instead of SSE for chat streaming.
  static const useWebSocketStreaming = bool.fromEnvironment(
    'USE_WEBSOCKET',
    defaultValue: false,
  );
}
