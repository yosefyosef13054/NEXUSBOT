import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'core/preferences.dart';
import 'core/router.dart';
import 'core/theme.dart';

class NexusBotApp extends ConsumerWidget {
  const NexusBotApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final router = ref.watch(routerProvider);
    final themeMode = ref.watch(prefsProvider.select((p) => p.themeMode));
    return MaterialApp.router(
      title: 'NexusBot',
      debugShowCheckedModeBanner: false,
      themeMode: themeMode,
      darkTheme: buildNexusTheme(dark: true),
      theme: buildNexusTheme(dark: false),
      routerConfig: router,
    );
  }
}
