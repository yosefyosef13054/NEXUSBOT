import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../core/theme.dart';
import '../core/widgets.dart';
import '../features/auth/auth_providers.dart';
import '../features/auth/auth_repository.dart';
import '../features/chat/chat_models.dart';
import '../features/chat/chat_repository.dart';

/// Persistent left sidebar shown on tablet/desktop. Contains:
/// - Brand
/// - "New chat" button
/// - Search field
/// - Session list (with rename/delete actions)
/// - User profile dropdown (settings + sign out)
class Sidebar extends ConsumerStatefulWidget {
  const Sidebar({
    super.key,
    required this.selectedSessionId,
    required this.onSelectSession,
    required this.onOpenSettings,
    required this.onOpenDocuments,
    this.width = 280,
    this.showClose,
  });

  final String? selectedSessionId;
  final void Function(String sessionId) onSelectSession;
  final VoidCallback onOpenSettings;
  final VoidCallback onOpenDocuments;
  final double width;

  /// If set, shown as a close button in the header (used when the sidebar
  /// is rendered inside a drawer).
  final VoidCallback? showClose;

  @override
  ConsumerState<Sidebar> createState() => _SidebarState();
}

class _SidebarState extends ConsumerState<Sidebar> {
  final _search = TextEditingController();
  bool _creating = false;

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  Future<void> _newChat() async {
    setState(() => _creating = true);
    try {
      final repo = await ref.read(chatRepositoryProvider.future);
      final s = await repo.createSession();
      ref.invalidate(sessionListProvider);
      widget.onSelectSession(s.id);
    } finally {
      if (mounted) setState(() => _creating = false);
    }
  }

  Future<void> _rename(ChatSession s) async {
    final newTitle = await showDialog<String>(
      context: context,
      builder: (ctx) => _RenameDialog(initial: s.title),
    );
    if (!mounted) return;
    if (newTitle == null || newTitle.isEmpty || newTitle == s.title) return;
    final repo = await ref.read(chatRepositoryProvider.future);
    await repo.renameSession(s.id, newTitle);
    ref.invalidate(sessionListProvider);
  }

  Future<void> _delete(ChatSession s) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete chat?'),
        content: Text('“${s.title}” will be removed. This can’t be undone.'),
        actions: [
          TextButton(onPressed: () => Navigator.of(ctx).pop(false), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            style: FilledButton.styleFrom(backgroundColor: NexusColors.crimson),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    final repo = await ref.read(chatRepositoryProvider.future);
    await repo.deleteSession(s.id);
    ref.invalidate(sessionListProvider);
    if (widget.selectedSessionId == s.id) widget.onSelectSession('');
  }

  Future<void> _logout() async {
    final repo = await ref.read(authRepositoryProvider.future);
    await repo.logout();
    ref.invalidate(currentUserProvider);
    if (!mounted) return;
    context.go('/login');
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final user = ref.watch(currentUserProvider);
    final sessions = ref.watch(sessionListProvider);

    return Container(
      width: widget.width,
      decoration: BoxDecoration(
        color: t.surface,
        border: Border(right: BorderSide(color: t.border)),
      ),
      child: SafeArea(
        child: Column(
          children: [
            // ── Header ───────────────────────────────────────────────
            Padding(
              padding: const EdgeInsets.all(NexusSpacing.lg),
              child: Row(
                children: [
                  const NexusLogo(size: 30),
                  const SizedBox(width: NexusSpacing.sm),
                  const Expanded(child: NexusWordmark(fontSize: 18)),
                  if (widget.showClose != null)
                    IconButton(
                      icon: const Icon(Icons.close_rounded),
                      onPressed: widget.showClose,
                    ),
                ],
              ),
            ),

            // ── New chat button ──────────────────────────────────────
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: NexusSpacing.lg),
              child: NexusButton(
                label: 'New chat',
                icon: Icons.add_rounded,
                loading: _creating,
                onPressed: _creating ? null : _newChat,
              ),
            ),
            const SizedBox(height: NexusSpacing.md),

            // ── Search ───────────────────────────────────────────────
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: NexusSpacing.lg),
              child: TextField(
                controller: _search,
                onChanged: (_) => setState(() {}),
                decoration: InputDecoration(
                  hintText: 'Search chats…',
                  prefixIcon: const Icon(Icons.search_rounded, size: 18),
                  suffixIcon: _search.text.isEmpty
                      ? null
                      : IconButton(
                          icon: const Icon(Icons.close_rounded, size: 16),
                          onPressed: () => setState(_search.clear),
                        ),
                  contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                ),
              ),
            ),
            const SizedBox(height: NexusSpacing.md),

            // ── Section header ───────────────────────────────────────
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: NexusSpacing.lg),
              child: Row(
                children: [
                  Text(
                    'CHATS',
                    style: TextStyle(
                      color: t.textMuted,
                      fontSize: 10,
                      fontWeight: FontWeight.w800,
                      letterSpacing: 3,
                    ),
                  ),
                  const Spacer(),
                  if (sessions.value != null && sessions.value!.isNotEmpty)
                    Text(
                      '${_filtered(sessions.value!).length}',
                      style: TextStyle(color: t.textMuted, fontSize: 11),
                    ),
                ],
              ),
            ),
            const SizedBox(height: NexusSpacing.sm),

            // ── Sessions list ────────────────────────────────────────
            Expanded(
              child: sessions.when(
                loading: () => const _SessionSkeletons(),
                error: (e, _) => Center(
                  child: Text('$e', style: TextStyle(color: t.textMuted, fontSize: 12)),
                ),
                data: (list) {
                  final filtered = _filtered(list);
                  if (filtered.isEmpty) {
                    return Center(
                      child: Text(
                        _search.text.isEmpty ? 'No chats yet' : 'No matches',
                        style: TextStyle(color: t.textMuted, fontSize: 12),
                      ),
                    );
                  }
                  return ListView.builder(
                    padding: const EdgeInsets.symmetric(horizontal: NexusSpacing.sm),
                    itemCount: filtered.length,
                    itemBuilder: (_, i) {
                      final s = filtered[i];
                      final selected = widget.selectedSessionId == s.id;
                      return _SessionTile(
                        session: s,
                        selected: selected,
                        onTap: () => widget.onSelectSession(s.id),
                        onRename: () => _rename(s),
                        onDelete: () => _delete(s),
                      );
                    },
                  );
                },
              ),
            ),

            Divider(color: t.borderSubtle, height: 1),

            // ── Quick links ──────────────────────────────────────────
            _SidebarLink(
              icon: Icons.menu_book_rounded,
              label: 'Documents',
              onTap: widget.onOpenDocuments,
            ),
            _SidebarLink(
              icon: Icons.settings_rounded,
              label: 'Settings',
              onTap: widget.onOpenSettings,
            ),

            // ── Profile dropdown ─────────────────────────────────────
            user.when(
              loading: () => const Padding(
                padding: EdgeInsets.all(NexusSpacing.lg),
                child: SizedBox(
                  height: 16,
                  child: Skeleton(width: double.infinity, height: 16),
                ),
              ),
              error: (_, __) => const SizedBox.shrink(),
              data: (u) {
                if (u == null) return const SizedBox.shrink();
                return PopupMenuButton<String>(
                  offset: const Offset(0, -56),
                  onSelected: (v) {
                    if (v == 'settings') widget.onOpenSettings();
                    if (v == 'logout') _logout();
                  },
                  itemBuilder: (_) => const [
                    PopupMenuItem(value: 'settings', child: Text('Settings')),
                    PopupMenuItem(value: 'logout', child: Text('Sign out')),
                  ],
                  child: Container(
                    padding: const EdgeInsets.all(NexusSpacing.md),
                    decoration: BoxDecoration(
                      border: Border(top: BorderSide(color: t.border)),
                    ),
                    child: Row(
                      children: [
                        Container(
                          width: 32,
                          height: 32,
                          decoration: const BoxDecoration(
                            gradient: NexusColors.redGradient,
                            shape: BoxShape.circle,
                          ),
                          alignment: Alignment.center,
                          child: Text(
                            u.email.substring(0, 1).toUpperCase(),
                            style: const TextStyle(
                              color: Colors.white,
                              fontWeight: FontWeight.w800,
                              fontSize: 14,
                            ),
                          ),
                        ),
                        const SizedBox(width: NexusSpacing.md),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                u.fullName?.isNotEmpty == true ? u.fullName! : u.email.split('@').first,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(
                                  color: t.textPrimary,
                                  fontSize: 13,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                              Text(
                                u.email,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(color: t.textMuted, fontSize: 11),
                              ),
                            ],
                          ),
                        ),
                        Icon(Icons.unfold_more_rounded, color: t.textMuted, size: 18),
                      ],
                    ),
                  ),
                );
              },
            ),
          ],
        ),
      ),
    );
  }

  List<ChatSession> _filtered(List<ChatSession> all) {
    final q = _search.text.trim().toLowerCase();
    if (q.isEmpty) return all;
    return all.where((s) => s.title.toLowerCase().contains(q)).toList();
  }
}

class _SessionTile extends StatelessWidget {
  const _SessionTile({
    required this.session,
    required this.selected,
    required this.onTap,
    required this.onRename,
    required this.onDelete,
  });

  final ChatSession session;
  final bool selected;
  final VoidCallback onTap;
  final VoidCallback onRename;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.only(bottom: 2),
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          onTap: onTap,
          borderRadius: const BorderRadius.all(NexusRadius.md),
          child: AnimatedContainer(
            duration: const Duration(milliseconds: 140),
            padding: const EdgeInsets.symmetric(horizontal: NexusSpacing.md, vertical: 10),
            decoration: BoxDecoration(
              color: selected ? NexusColors.crimson.withOpacity(0.10) : Colors.transparent,
              borderRadius: const BorderRadius.all(NexusRadius.md),
              border: Border.all(
                color: selected ? NexusColors.crimson.withOpacity(0.4) : Colors.transparent,
              ),
            ),
            child: Row(
              children: [
                Icon(
                  Icons.chat_bubble_outline_rounded,
                  size: 15,
                  color: selected ? NexusColors.crimson : t.textMuted,
                ),
                const SizedBox(width: NexusSpacing.sm),
                Expanded(
                  child: Text(
                    session.title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      color: selected ? t.textPrimary : t.textSecondary,
                      fontSize: 13,
                      fontWeight: selected ? FontWeight.w700 : FontWeight.w500,
                    ),
                  ),
                ),
                PopupMenuButton<String>(
                  icon: Icon(Icons.more_horiz_rounded, size: 16, color: t.textMuted),
                  iconSize: 16,
                  padding: EdgeInsets.zero,
                  onSelected: (v) {
                    if (v == 'rename') onRename();
                    if (v == 'delete') onDelete();
                  },
                  itemBuilder: (_) => const [
                    PopupMenuItem(value: 'rename', child: Text('Rename')),
                    PopupMenuItem(value: 'delete', child: Text('Delete')),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _SidebarLink extends StatelessWidget {
  const _SidebarLink({required this.icon, required this.label, required this.onTap});
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: Colors.transparent,
      child: InkWell(
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: NexusSpacing.lg, vertical: 14),
          child: Row(
            children: [
              Icon(icon, size: 16, color: t.textSecondary),
              const SizedBox(width: NexusSpacing.md),
              Text(
                label,
                style: TextStyle(
                  color: t.textSecondary,
                  fontSize: 13,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _SessionSkeletons extends StatelessWidget {
  const _SessionSkeletons();

  @override
  Widget build(BuildContext context) {
    return ListView.builder(
      padding: const EdgeInsets.symmetric(horizontal: NexusSpacing.md, vertical: NexusSpacing.sm),
      itemCount: 6,
      itemBuilder: (_, __) => const Padding(
        padding: EdgeInsets.symmetric(vertical: 6),
        child: Skeleton(width: double.infinity, height: 32, radius: 10),
      ),
    );
  }
}

/// Rename dialog that owns its own [TextEditingController] and disposes it in
/// [State.dispose] — which Flutter calls only after the dialog's exit animation
/// finishes. Disposing the controller inline (right after `showDialog` returns)
/// crashes with a red screen, because the TextField is still mounted and using
/// the controller while the route animates out.
class _RenameDialog extends StatefulWidget {
  const _RenameDialog({required this.initial});
  final String initial;

  @override
  State<_RenameDialog> createState() => _RenameDialogState();
}

class _RenameDialogState extends State<_RenameDialog> {
  late final TextEditingController _ctrl = TextEditingController(text: widget.initial);

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  void _submit() => Navigator.of(context).pop(_ctrl.text.trim());

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Rename chat'),
      content: TextField(
        controller: _ctrl,
        autofocus: true,
        decoration: const InputDecoration(hintText: 'Chat name'),
        onSubmitted: (_) => _submit(),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('Cancel')),
        FilledButton(
          onPressed: _submit,
          style: FilledButton.styleFrom(backgroundColor: NexusColors.crimson),
          child: const Text('Save'),
        ),
      ],
    );
  }
}
