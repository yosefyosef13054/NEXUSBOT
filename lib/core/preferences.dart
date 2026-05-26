import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Streaming transport for assistant replies.
enum StreamTransport { sse, websocket }

/// User preferences persisted to SharedPreferences.
///
/// Single source of truth for app-level settings (theme, transport, model,
/// streaming toggle). Every screen reads through Riverpod providers below;
/// no direct SharedPreferences calls elsewhere in the app.
class AppPrefs {
  const AppPrefs({
    required this.themeMode,
    required this.transport,
    required this.model,
    required this.streamingEnabled,
    required this.ragByDefault,
  });

  final ThemeMode themeMode;
  final StreamTransport transport;
  final String model;
  final bool streamingEnabled;
  final bool ragByDefault;

  AppPrefs copyWith({
    ThemeMode? themeMode,
    StreamTransport? transport,
    String? model,
    bool? streamingEnabled,
    bool? ragByDefault,
  }) =>
      AppPrefs(
        themeMode: themeMode ?? this.themeMode,
        transport: transport ?? this.transport,
        model: model ?? this.model,
        streamingEnabled: streamingEnabled ?? this.streamingEnabled,
        ragByDefault: ragByDefault ?? this.ragByDefault,
      );

  static const defaults = AppPrefs(
    themeMode: ThemeMode.system,
    transport: StreamTransport.sse,
    model: 'gpt-4o-mini',
    streamingEnabled: true,
    ragByDefault: true,
  );
}

class _PrefKeys {
  static const themeMode = 'pref.themeMode';
  static const transport = 'pref.transport';
  static const model = 'pref.model';
  static const streaming = 'pref.streaming';
  static const rag = 'pref.rag';
}

class PrefsController extends StateNotifier<AppPrefs> {
  PrefsController(this._prefs) : super(_read(_prefs));

  final SharedPreferences _prefs;

  static AppPrefs _read(SharedPreferences p) => AppPrefs(
        themeMode: ThemeMode.values[
            p.getInt(_PrefKeys.themeMode) ?? ThemeMode.system.index],
        transport: StreamTransport.values[
            p.getInt(_PrefKeys.transport) ?? StreamTransport.sse.index],
        model: p.getString(_PrefKeys.model) ?? AppPrefs.defaults.model,
        streamingEnabled: p.getBool(_PrefKeys.streaming) ?? true,
        ragByDefault: p.getBool(_PrefKeys.rag) ?? true,
      );

  Future<void> setThemeMode(ThemeMode m) async {
    state = state.copyWith(themeMode: m);
    await _prefs.setInt(_PrefKeys.themeMode, m.index);
  }

  Future<void> setTransport(StreamTransport t) async {
    state = state.copyWith(transport: t);
    await _prefs.setInt(_PrefKeys.transport, t.index);
  }

  Future<void> setModel(String model) async {
    state = state.copyWith(model: model);
    await _prefs.setString(_PrefKeys.model, model);
  }

  Future<void> setStreaming(bool v) async {
    state = state.copyWith(streamingEnabled: v);
    await _prefs.setBool(_PrefKeys.streaming, v);
  }

  Future<void> setRag(bool v) async {
    state = state.copyWith(ragByDefault: v);
    await _prefs.setBool(_PrefKeys.rag, v);
  }
}

final sharedPreferencesProvider = FutureProvider<SharedPreferences>((_) {
  return SharedPreferences.getInstance();
});

final prefsProvider = StateNotifierProvider<PrefsController, AppPrefs>((ref) {
  final prefs = ref.watch(sharedPreferencesProvider).valueOrNull;
  if (prefs == null) {
    // Synchronous fallback while SharedPreferences loads — uses defaults; the
    // listener below swaps in real prefs when available.
    return PrefsController(_DummyPrefs());
  }
  return PrefsController(prefs);
});

/// Shim used only until SharedPreferences resolves (≈ a frame at cold start).
class _DummyPrefs implements SharedPreferences {
  @override
  dynamic noSuchMethod(Invocation invocation) => null;
}
