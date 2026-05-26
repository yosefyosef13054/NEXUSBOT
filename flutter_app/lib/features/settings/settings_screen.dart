import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/breakpoints.dart';
import '../../core/env.dart';
import '../../core/preferences.dart';
import '../../core/theme.dart';
import '../../shared/home_shell.dart';
import '../auth/auth_providers.dart';
import '../auth/auth_repository.dart';
import '../chat/chat_repository.dart';

const _availableModels = [
  ('gpt-4o-mini', 'GPT-4o mini', 'Fast · cheap · default'),
  ('gpt-4o', 'GPT-4o', 'High quality · slower'),
  ('gpt-4-turbo', 'GPT-4 Turbo', 'Reasoning · long context'),
];

class SettingsScreen extends ConsumerWidget {
  const SettingsScreen({super.key});

  Future<void> _logout(BuildContext context, WidgetRef ref) async {
    final repo = await ref.read(authRepositoryProvider.future);
    await repo.logout();
    ref.invalidate(currentUserProvider);
    if (!context.mounted) return;
    context.go('/login');
  }

  Future<void> _clearAllChats(BuildContext context, WidgetRef ref) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete all chats?'),
        content: const Text(
          'This deletes every chat session for your account. Documents are preserved.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.of(ctx).pop(false), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            style: FilledButton.styleFrom(backgroundColor: NexusColors.crimson),
            child: const Text('Delete all'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    final repo = await ref.read(chatRepositoryProvider.future);
    final sessions = await repo.listSessions();
    for (final s in sessions) {
      await repo.deleteSession(s.id);
    }
    ref.invalidate(sessionListProvider);
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Deleted ${sessions.length} chats')),
      );
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final user = ref.watch(currentUserProvider);
    final prefs = ref.watch(prefsProvider);
    final prefsCtrl = ref.read(prefsProvider.notifier);

    return Scaffold(
      backgroundColor: t.background,
      appBar: AppBar(
        leading: context.isMobile ? const MenuButton() : null,
        title: Text('Settings', style: TextStyle(color: t.textPrimary)),
      ),
      body: Align(
        alignment: Alignment.topCenter,
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 720),
          child: ListView(
            padding: const EdgeInsets.all(NexusSpacing.lg),
            children: [
              // ── Profile card ───────────────────────────────────────────
              Container(
                padding: const EdgeInsets.all(NexusSpacing.lg),
                decoration: BoxDecoration(
                  color: t.surfaceHigh,
                  borderRadius: const BorderRadius.all(NexusRadius.lg),
                  border: Border.all(color: t.border),
                ),
                child: Row(
                  children: [
                    Container(
                      width: 56,
                      height: 56,
                      decoration: const BoxDecoration(
                        gradient: NexusColors.redGradient,
                        shape: BoxShape.circle,
                      ),
                      alignment: Alignment.center,
                      child: Text(
                        user.value?.email.substring(0, 1).toUpperCase() ?? '?',
                        style: const TextStyle(
                          color: Colors.white,
                          fontSize: 24,
                          fontWeight: FontWeight.w800,
                        ),
                      ),
                    ),
                    const SizedBox(width: NexusSpacing.lg),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            user.value?.fullName?.isNotEmpty == true
                                ? user.value!.fullName!
                                : 'Signed in',
                            style: TextStyle(
                                color: t.textPrimary, fontSize: 16, fontWeight: FontWeight.w700),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            user.value?.email ?? '—',
                            style: TextStyle(color: t.textMuted, fontSize: 13),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ),

              const SizedBox(height: NexusSpacing.xl),
              _Section(
                label: 'APPEARANCE',
                children: [
                  _ThemeRow(value: prefs.themeMode, onChanged: prefsCtrl.setThemeMode),
                ],
              ),

              const SizedBox(height: NexusSpacing.xl),
              _Section(
                label: 'CHAT',
                children: [
                  _SwitchTile(
                    icon: Icons.stream_rounded,
                    title: 'Stream responses',
                    subtitle: 'Token-by-token delivery. Off = wait for full reply.',
                    value: prefs.streamingEnabled,
                    onChanged: prefsCtrl.setStreaming,
                  ),
                  const _Divider(),
                  _SwitchTile(
                    icon: Icons.menu_book_rounded,
                    title: 'Use RAG by default',
                    subtitle: 'Ground replies in your uploaded documents.',
                    value: prefs.ragByDefault,
                    onChanged: prefsCtrl.setRag,
                  ),
                  const _Divider(),
                  _ChoiceTile<StreamTransport>(
                    icon: Icons.swap_horiz_rounded,
                    title: 'Stream transport',
                    value: prefs.transport,
                    onChanged: prefsCtrl.setTransport,
                    options: const [
                      ('Server-Sent Events', StreamTransport.sse),
                      ('WebSocket', StreamTransport.websocket),
                    ],
                  ),
                ],
              ),

              const SizedBox(height: NexusSpacing.xl),
              _Section(
                label: 'MODEL',
                children: [
                  for (final m in _availableModels) ...[
                    _ModelTile(
                      id: m.$1,
                      name: m.$2,
                      description: m.$3,
                      selected: prefs.model == m.$1,
                      onTap: () => prefsCtrl.setModel(m.$1),
                    ),
                    if (m != _availableModels.last) const _Divider(),
                  ],
                ],
              ),

              const SizedBox(height: NexusSpacing.xl),
              _Section(
                label: 'CONNECTION',
                children: [
                  _InfoRow(icon: Icons.cloud_outlined, label: 'API base URL', value: Env.apiBaseUrl),
                ],
              ),

              const SizedBox(height: NexusSpacing.xl),
              _Section(
                label: 'ACCOUNT',
                children: [
                  _ActionTile(
                    icon: Icons.delete_sweep_rounded,
                    title: 'Clear all chats',
                    subtitle: 'Remove every chat session.',
                    onTap: () => _clearAllChats(context, ref),
                    danger: true,
                  ),
                  const _Divider(),
                  _ActionTile(
                    icon: Icons.logout_rounded,
                    title: 'Sign out',
                    subtitle: user.value?.email ?? '',
                    onTap: () => _logout(context, ref),
                    danger: true,
                  ),
                ],
              ),

              const SizedBox(height: NexusSpacing.xxxl),
              Center(
                child: Text(
                  'NexusBot v1.0.0 · FastAPI · LangChain · Flutter',
                  style: TextStyle(color: t.textMuted, fontSize: 11),
                ),
              ),
              const SizedBox(height: NexusSpacing.xxxl),
            ],
          ),
        ),
      ),
    );
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Settings primitives
// ────────────────────────────────────────────────────────────────────────────

class _Section extends StatelessWidget {
  const _Section({required this.label, required this.children});
  final String label;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.only(left: 4, bottom: NexusSpacing.sm),
          child: Text(
            label,
            style: TextStyle(
              color: t.textMuted,
              fontSize: 11,
              fontWeight: FontWeight.w800,
              letterSpacing: 3,
            ),
          ),
        ),
        Container(
          decoration: BoxDecoration(
            color: t.surfaceHigh,
            borderRadius: const BorderRadius.all(NexusRadius.lg),
            border: Border.all(color: t.border),
          ),
          child: Column(children: children),
        ),
      ],
    );
  }
}

class _Divider extends StatelessWidget {
  const _Divider();
  @override
  Widget build(BuildContext context) {
    return Divider(color: context.tokens.borderSubtle, height: 1, indent: 56);
  }
}

class _SwitchTile extends StatelessWidget {
  const _SwitchTile({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.value,
    required this.onChanged,
  });

  final IconData icon;
  final String title;
  final String subtitle;
  final bool value;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return ListTile(
      contentPadding: const EdgeInsets.symmetric(horizontal: NexusSpacing.lg, vertical: 4),
      leading: Icon(icon, color: NexusColors.crimson, size: 20),
      title: Text(title, style: TextStyle(color: t.textPrimary, fontWeight: FontWeight.w700)),
      subtitle: Text(subtitle, style: TextStyle(color: t.textMuted, fontSize: 12)),
      trailing: Switch(
        value: value,
        onChanged: onChanged,
        activeColor: NexusColors.crimson,
      ),
      onTap: () => onChanged(!value),
    );
  }
}

class _ChoiceTile<T> extends StatelessWidget {
  const _ChoiceTile({
    required this.icon,
    required this.title,
    required this.value,
    required this.onChanged,
    required this.options,
  });

  final IconData icon;
  final String title;
  final T value;
  final ValueChanged<T> onChanged;
  final List<(String label, T value)> options;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return ListTile(
      contentPadding: const EdgeInsets.symmetric(horizontal: NexusSpacing.lg, vertical: 4),
      leading: Icon(icon, color: NexusColors.crimson, size: 20),
      title: Text(title, style: TextStyle(color: t.textPrimary, fontWeight: FontWeight.w700)),
      trailing: DropdownButton<T>(
        value: value,
        underline: const SizedBox.shrink(),
        items: options.map((o) => DropdownMenuItem(value: o.$2, child: Text(o.$1))).toList(),
        onChanged: (v) {
          if (v != null) onChanged(v);
        },
      ),
    );
  }
}

class _ModelTile extends StatelessWidget {
  const _ModelTile({
    required this.id,
    required this.name,
    required this.description,
    required this.selected,
    required this.onTap,
  });

  final String id;
  final String name;
  final String description;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: Colors.transparent,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: NexusSpacing.lg, vertical: 14),
          child: Row(
            children: [
              Container(
                width: 18,
                height: 18,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  border: Border.all(
                    color: selected ? NexusColors.crimson : t.border,
                    width: 2,
                  ),
                ),
                child: selected
                    ? Center(
                        child: Container(
                          width: 8,
                          height: 8,
                          decoration: const BoxDecoration(
                            color: NexusColors.crimson,
                            shape: BoxShape.circle,
                          ),
                        ),
                      )
                    : null,
              ),
              const SizedBox(width: NexusSpacing.md),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      name,
                      style: TextStyle(
                        color: t.textPrimary,
                        fontWeight: FontWeight.w700,
                        fontSize: 14,
                      ),
                    ),
                    Text(description, style: TextStyle(color: t.textMuted, fontSize: 12)),
                  ],
                ),
              ),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                decoration: BoxDecoration(
                  color: t.surfaceHigher,
                  borderRadius: const BorderRadius.all(NexusRadius.pill),
                ),
                child: Text(
                  id,
                  style: TextStyle(
                    color: t.textMuted,
                    fontSize: 10,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _InfoRow extends StatelessWidget {
  const _InfoRow({required this.icon, required this.label, required this.value});
  final IconData icon;
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: NexusSpacing.lg, vertical: 14),
      child: Row(
        children: [
          Icon(icon, color: NexusColors.crimson, size: 20),
          const SizedBox(width: NexusSpacing.md),
          Expanded(child: Text(label, style: TextStyle(color: t.textSecondary, fontSize: 13))),
          Flexible(
            child: Text(
              value,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(color: t.textPrimary, fontSize: 13, fontWeight: FontWeight.w600),
            ),
          ),
        ],
      ),
    );
  }
}

class _ActionTile extends StatelessWidget {
  const _ActionTile({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.onTap,
    this.danger = false,
  });

  final IconData icon;
  final String title;
  final String subtitle;
  final VoidCallback onTap;
  final bool danger;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final color = danger ? NexusColors.crimson : t.textPrimary;
    return ListTile(
      contentPadding: const EdgeInsets.symmetric(horizontal: NexusSpacing.lg, vertical: 4),
      leading: Icon(icon, color: color, size: 20),
      title: Text(title, style: TextStyle(color: color, fontWeight: FontWeight.w700)),
      subtitle: subtitle.isEmpty
          ? null
          : Text(subtitle, style: TextStyle(color: t.textMuted, fontSize: 12)),
      trailing: Icon(Icons.chevron_right_rounded, color: t.textMuted, size: 20),
      onTap: onTap,
    );
  }
}

class _ThemeRow extends StatelessWidget {
  const _ThemeRow({required this.value, required this.onChanged});
  final ThemeMode value;
  final ValueChanged<ThemeMode> onChanged;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    const options = [
      (Icons.brightness_auto_rounded, 'System', ThemeMode.system),
      (Icons.light_mode_rounded, 'Light', ThemeMode.light),
      (Icons.dark_mode_rounded, 'Dark', ThemeMode.dark),
    ];
    return Padding(
      padding: const EdgeInsets.all(NexusSpacing.md),
      child: Row(
        children: options.map((opt) {
          final selected = value == opt.$3;
          return Expanded(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 4),
              child: Material(
                color: Colors.transparent,
                child: InkWell(
                  onTap: () => onChanged(opt.$3),
                  borderRadius: const BorderRadius.all(NexusRadius.md),
                  child: AnimatedContainer(
                    duration: const Duration(milliseconds: 160),
                    padding: const EdgeInsets.symmetric(vertical: 14),
                    decoration: BoxDecoration(
                      color: selected ? NexusColors.crimson.withOpacity(0.12) : t.surfaceHigher,
                      borderRadius: const BorderRadius.all(NexusRadius.md),
                      border: Border.all(
                        color: selected ? NexusColors.crimson : Colors.transparent,
                      ),
                    ),
                    child: Column(
                      children: [
                        Icon(opt.$1, size: 18, color: selected ? NexusColors.crimson : t.textSecondary),
                        const SizedBox(height: 6),
                        Text(
                          opt.$2,
                          style: TextStyle(
                            color: selected ? t.textPrimary : t.textSecondary,
                            fontSize: 12,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          );
        }).toList(),
      ),
    );
  }
}
