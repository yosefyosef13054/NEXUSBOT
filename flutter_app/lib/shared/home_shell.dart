import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/breakpoints.dart';
import '../core/theme.dart';
import '../core/widgets.dart';
import '../features/chat/chat_repository.dart';
import '../features/chat/chat_screen.dart';
import '../features/documents/documents_screen.dart';
import '../features/settings/settings_screen.dart';
import 'sidebar.dart';

enum _HomeRoute { chat, documents, settings }

/// Owns "which session is open" and "which side panel is shown" for the
/// whole app shell. Adaptive: drawer on mobile, persistent sidebar on wide.
class HomeShell extends ConsumerStatefulWidget {
  const HomeShell({super.key});

  @override
  ConsumerState<HomeShell> createState() => HomeShellState();
}

class HomeShellState extends ConsumerState<HomeShell> {
  String? _selectedSessionId;
  _HomeRoute _route = _HomeRoute.chat;
  final _scaffoldKey = GlobalKey<ScaffoldState>();

  /// Opens the shell's navigation drawer (mobile layout). Called from the
  /// hamburger button, which lives inside an inner screen's Scaffold and so
  /// can't reach this drawer via `Scaffold.of`.
  void openDrawer() => _scaffoldKey.currentState?.openDrawer();

  void selectSession(String id) {
    setState(() {
      _selectedSessionId = id.isEmpty ? null : id;
      _route = _HomeRoute.chat;
    });
    if (context.isMobile) Navigator.of(context).maybePop();
  }

  void openSettings() {
    setState(() => _route = _HomeRoute.settings);
    if (context.isMobile) Navigator.of(context).maybePop();
  }

  void openDocuments() {
    setState(() => _route = _HomeRoute.documents);
    if (context.isMobile) Navigator.of(context).maybePop();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_selectedSessionId == null && _route == _HomeRoute.chat) {
      final sessions = ref.read(sessionListProvider).valueOrNull;
      if (sessions != null && sessions.isNotEmpty) {
        _selectedSessionId = sessions.first.id;
      }
    }
  }

  Widget _content() {
    switch (_route) {
      case _HomeRoute.documents:
        return const DocumentsScreen();
      case _HomeRoute.settings:
        return const SettingsScreen();
      case _HomeRoute.chat:
        if (_selectedSessionId == null) return const _NewChatPrompt();
        return ChatScreen(
          key: ValueKey(_selectedSessionId),
          sessionId: _selectedSessionId!,
        );
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final wide = context.isWide;

    final sidebar = Sidebar(
      selectedSessionId: _selectedSessionId,
      onSelectSession: selectSession,
      onOpenSettings: openSettings,
      onOpenDocuments: openDocuments,
      width: context.isDesktop ? 280 : 260,
    );

    if (wide) {
      return Scaffold(
        backgroundColor: t.background,
        body: Row(
          children: [
            sidebar,
            Expanded(
              child: AnimatedSwitcher(
                duration: const Duration(milliseconds: 180),
                switchInCurve: Curves.easeOut,
                switchOutCurve: Curves.easeIn,
                transitionBuilder: (child, anim) => FadeTransition(
                  opacity: anim,
                  child: SlideTransition(
                    position: Tween(begin: const Offset(0, 0.02), end: Offset.zero).animate(anim),
                    child: child,
                  ),
                ),
                child: KeyedSubtree(
                  key: ValueKey('${_route.name}-${_selectedSessionId ?? "new"}'),
                  child: _content(),
                ),
              ),
            ),
          ],
        ),
      );
    }

    return Scaffold(
      key: _scaffoldKey,
      backgroundColor: t.background,
      drawer: Drawer(
        backgroundColor: t.surface,
        width: 300,
        child: Sidebar(
          selectedSessionId: _selectedSessionId,
          onSelectSession: selectSession,
          onOpenSettings: openSettings,
          onOpenDocuments: openDocuments,
          width: 300,
          showClose: () => Navigator.of(context).maybePop(),
        ),
      ),
      body: _content(),
    );
  }
}

class _NewChatPrompt extends ConsumerStatefulWidget {
  const _NewChatPrompt();

  @override
  ConsumerState<_NewChatPrompt> createState() => _NewChatPromptState();
}

class _NewChatPromptState extends ConsumerState<_NewChatPrompt> {
  bool _creating = false;

  Future<void> _create() async {
    setState(() => _creating = true);
    try {
      final repo = await ref.read(chatRepositoryProvider.future);
      final s = await repo.createSession();
      ref.invalidate(sessionListProvider);
      context.findAncestorStateOfType<HomeShellState>()?.selectSession(s.id);
    } finally {
      if (mounted) setState(() => _creating = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Scaffold(
      backgroundColor: t.background,
      appBar: context.isMobile
          ? AppBar(
              leading: const MenuButton(),
              title: const NexusWordmark(fontSize: 18),
            )
          : null,
      body: EmptyState(
        icon: Icons.auto_awesome_rounded,
        title: 'Welcome to NexusBot',
        subtitle:
            'Start a conversation, upload documents to chat with them, or enable tools to let the AI take actions.',
        action: NexusButton(
          label: 'Start new chat',
          icon: Icons.add_rounded,
          loading: _creating,
          expand: false,
          onPressed: _creating ? null : _create,
        ),
      ),
    );
  }
}

/// Hamburger button that opens the shell drawer on mobile.
class MenuButton extends StatelessWidget {
  const MenuButton({super.key});

  @override
  Widget build(BuildContext context) {
    return IconButton(
      icon: const Icon(Icons.menu_rounded),
      onPressed: () => context.findAncestorStateOfType<HomeShellState>()?.openDrawer(),
    );
  }
}
