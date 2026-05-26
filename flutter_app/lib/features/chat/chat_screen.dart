import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/breakpoints.dart';
import '../../core/theme.dart';
import '../../core/widgets.dart';
import '../../shared/home_shell.dart';
import 'chat_controller.dart';
import 'chat_models.dart';
import 'chat_repository.dart';
import 'chat_widgets.dart';

class ChatScreen extends ConsumerStatefulWidget {
  const ChatScreen({super.key, required this.sessionId});
  final String sessionId;

  @override
  ConsumerState<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends ConsumerState<ChatScreen> {
  final _scrollCtrl = ScrollController();

  @override
  void dispose() {
    _scrollCtrl.dispose();
    super.dispose();
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scrollCtrl.hasClients) return;
      _scrollCtrl.animateTo(
        _scrollCtrl.position.maxScrollExtent,
        duration: const Duration(milliseconds: 200),
        curve: Curves.easeOut,
      );
    });
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final state = ref.watch(chatControllerProvider(widget.sessionId));
    final controller = ref.read(chatControllerProvider(widget.sessionId).notifier);
    final session = ref
        .watch(sessionListProvider)
        .valueOrNull
        ?.where((s) => s.id == widget.sessionId)
        .firstOrNull;

    ref.listen(chatControllerProvider(widget.sessionId), (_, __) => _scrollToBottom());

    final lastAssistantId = state.messages.lastWhere(
      (m) => m.role == MessageRole.assistant,
      orElse: () => ChatMessage(
        id: '',
        sessionId: '',
        role: MessageRole.assistant,
        content: '',
        createdAt: DateTime.now(),
      ),
    ).id;

    return Scaffold(
      backgroundColor: t.background,
      appBar: AppBar(
        leading: context.isMobile
            ? const MenuButton()
            : null,
        title: Row(
          children: [
            const NexusLogo(size: 22),
            const SizedBox(width: NexusSpacing.sm),
            Flexible(
              child: Text(
                session?.title ?? 'Chat',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  color: t.textPrimary,
                  fontWeight: FontWeight.w700,
                  fontSize: 16,
                ),
              ),
            ),
          ],
        ),
      ),
      body: Column(
        children: [
          Expanded(
            child: state.messages.isEmpty && !state.sending
                ? const _EmptyChat()
                : Align(
                    alignment: Alignment.topCenter,
                    child: ConstrainedBox(
                      constraints: const BoxConstraints(maxWidth: 880),
                      child: ListView.builder(
                        controller: _scrollCtrl,
                        padding: const EdgeInsets.symmetric(vertical: NexusSpacing.lg),
                        itemCount: state.messages.length,
                        itemBuilder: (_, i) {
                          final m = state.messages[i];
                          final isLastAssistant =
                              m.role == MessageRole.assistant && m.id == lastAssistantId;
                          return MessageBubble(
                            key: ValueKey(m.id),
                            message: m,
                            isLastAssistant: isLastAssistant && !state.sending,
                            onRegenerate: state.sending ? null : controller.regenerate,
                          );
                        },
                      ),
                    ),
                  ),
          ),
          if (state.error != null)
            Container(
              width: double.infinity,
              color: NexusColors.crimsonDark.withOpacity(0.18),
              padding: const EdgeInsets.symmetric(
                  horizontal: NexusSpacing.lg, vertical: NexusSpacing.sm),
              child: Row(
                children: [
                  const Icon(Icons.error_outline, size: 14, color: NexusColors.crimson),
                  const SizedBox(width: NexusSpacing.sm),
                  Expanded(
                    child: Text(
                      state.error!,
                      style: TextStyle(color: t.textPrimary, fontSize: 12),
                    ),
                  ),
                ],
              ),
            ),
          Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 920),
              child: Composer(
                onSend: controller.send,
                sending: state.sending,
                useRag: state.useRag,
                useTools: state.useTools,
                onToggleRag: controller.toggleRag,
                onToggleTools: controller.toggleTools,
                onStop: controller.stop,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _EmptyChat extends StatelessWidget {
  const _EmptyChat();

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Center(
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 480),
        child: Padding(
          padding: const EdgeInsets.all(NexusSpacing.xxl),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              const NexusLogo(size: 64),
              const SizedBox(height: NexusSpacing.lg),
              Text(
                'How can I help today?',
                style: TextStyle(
                  color: t.textPrimary,
                  fontSize: 22,
                  fontWeight: FontWeight.w800,
                ),
              ),
              const SizedBox(height: NexusSpacing.sm),
              Text(
                'Ask anything. Toggle RAG to ground answers in your documents, or Tools to let NexusBot run actions for you.',
                textAlign: TextAlign.center,
                style: TextStyle(color: t.textSecondary, fontSize: 13.5, height: 1.5),
              ),
              const SizedBox(height: NexusSpacing.xl),
              const Wrap(
                spacing: NexusSpacing.sm,
                runSpacing: NexusSpacing.sm,
                alignment: WrapAlignment.center,
                children: [
                  _SuggestionChip(icon: Icons.menu_book_rounded, label: 'Summarize a document'),
                  _SuggestionChip(icon: Icons.code_rounded, label: 'Explain a code snippet'),
                  _SuggestionChip(icon: Icons.lightbulb_outline_rounded, label: 'Brainstorm ideas'),
                  _SuggestionChip(icon: Icons.translate_rounded, label: 'Translate text'),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _SuggestionChip extends StatelessWidget {
  const _SuggestionChip({required this.icon, required this.label});
  final IconData icon;
  final String label;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: t.surfaceHigh,
        borderRadius: const BorderRadius.all(NexusRadius.pill),
        border: Border.all(color: t.border),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 14, color: NexusColors.crimson),
          const SizedBox(width: 6),
          Text(
            label,
            style: TextStyle(color: t.textSecondary, fontSize: 12, fontWeight: FontWeight.w600),
          ),
        ],
      ),
    );
  }
}
