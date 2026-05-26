import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:intl/intl.dart';
import 'package:markdown/markdown.dart' as md;

import '../../core/theme.dart';
import 'chat_models.dart';
import 'code_block.dart';

// ============================================================================
// MessageBubble — wraps the bubble with tool chips above, citations below,
// and a row of message-level actions (copy, regenerate).
// ============================================================================

class MessageBubble extends StatefulWidget {
  const MessageBubble({
    super.key,
    required this.message,
    required this.onRegenerate,
    required this.isLastAssistant,
  });

  final ChatMessage message;
  final VoidCallback? onRegenerate;
  final bool isLastAssistant;

  @override
  State<MessageBubble> createState() => _MessageBubbleState();
}

class _MessageBubbleState extends State<MessageBubble> {
  bool _showActions = false;
  bool _copied = false;

  bool get _isUser => widget.message.role == MessageRole.user;

  Future<void> _copy() async {
    await Clipboard.setData(ClipboardData(text: widget.message.content));
    if (!mounted) return;
    setState(() => _copied = true);
    Future.delayed(const Duration(milliseconds: 1400), () {
      if (mounted) setState(() => _copied = false);
    });
  }

  @override
  Widget build(BuildContext context) {
    final crossAxis = _isUser ? CrossAxisAlignment.end : CrossAxisAlignment.start;

    return MouseRegion(
      onEnter: (_) => setState(() => _showActions = true),
      onExit: (_) => setState(() => _showActions = false),
      child: GestureDetector(
        behavior: HitTestBehavior.translucent,
        onLongPress: () => setState(() => _showActions = !_showActions),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: NexusSpacing.lg, vertical: NexusSpacing.sm),
          child: Column(
            crossAxisAlignment: crossAxis,
            children: [
              if (widget.message.toolCalls.isNotEmpty) ...[
                _ToolCallStrip(toolCalls: widget.message.toolCalls),
                const SizedBox(height: NexusSpacing.xs),
              ],
              _BubbleContainer(
                isUser: _isUser,
                child: _BubbleContent(message: widget.message),
              ),
              if (widget.message.citations.isNotEmpty) ...[
                const SizedBox(height: NexusSpacing.sm),
                CitationsStrip(citations: widget.message.citations),
              ],
              AnimatedCrossFade(
                duration: const Duration(milliseconds: 150),
                crossFadeState:
                    (_showActions || widget.isLastAssistant) && !widget.message.streaming
                        ? CrossFadeState.showSecond
                        : CrossFadeState.showFirst,
                firstChild: const SizedBox(height: 4),
                secondChild: Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: _MessageActionsRow(
                    isUser: _isUser,
                    timestamp: widget.message.createdAt,
                    copied: _copied,
                    onCopy: _copy,
                    onRegenerate: !_isUser && widget.isLastAssistant ? widget.onRegenerate : null,
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

class _MessageActionsRow extends StatelessWidget {
  const _MessageActionsRow({
    required this.isUser,
    required this.timestamp,
    required this.copied,
    required this.onCopy,
    required this.onRegenerate,
  });

  final bool isUser;
  final DateTime timestamp;
  final bool copied;
  final VoidCallback onCopy;
  final VoidCallback? onRegenerate;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Row(
      mainAxisAlignment: isUser ? MainAxisAlignment.end : MainAxisAlignment.start,
      children: [
        Text(
          DateFormat.jm().format(timestamp.toLocal()),
          style: TextStyle(color: t.textMuted, fontSize: 11),
        ),
        const SizedBox(width: 8),
        _ActionIcon(
          icon: copied ? Icons.check_rounded : Icons.copy_rounded,
          tooltip: copied ? 'Copied' : 'Copy',
          color: copied ? NexusColors.success : t.textMuted,
          onTap: onCopy,
        ),
        if (onRegenerate != null) ...[
          const SizedBox(width: 4),
          _ActionIcon(
            icon: Icons.refresh_rounded,
            tooltip: 'Regenerate',
            color: t.textMuted,
            onTap: onRegenerate!,
          ),
        ],
      ],
    );
  }
}

class _ActionIcon extends StatelessWidget {
  const _ActionIcon({
    required this.icon,
    required this.tooltip,
    required this.color,
    required this.onTap,
  });
  final IconData icon;
  final String tooltip;
  final Color color;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: tooltip,
      child: InkWell(
        onTap: onTap,
        borderRadius: const BorderRadius.all(NexusRadius.sm),
        child: Padding(
          padding: const EdgeInsets.all(4),
          child: Icon(icon, size: 14, color: color),
        ),
      ),
    );
  }
}

// ============================================================================
// Bubble container + content
// ============================================================================

class _BubbleContainer extends StatelessWidget {
  const _BubbleContainer({required this.isUser, required this.child});
  final bool isUser;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final maxWidth = MediaQuery.sizeOf(context).width * 0.82;
    final radius = isUser
        ? const BorderRadius.only(
            topLeft: NexusRadius.lg,
            topRight: NexusRadius.lg,
            bottomLeft: NexusRadius.lg,
            bottomRight: NexusRadius.sm,
          )
        : const BorderRadius.only(
            topLeft: NexusRadius.lg,
            topRight: NexusRadius.lg,
            bottomRight: NexusRadius.lg,
            bottomLeft: NexusRadius.sm,
          );
    return ConstrainedBox(
      constraints: BoxConstraints(maxWidth: maxWidth.clamp(280, 720)),
      child: DecoratedBox(
        decoration: BoxDecoration(
          gradient: isUser ? NexusColors.redGradient : null,
          color: isUser ? null : t.surfaceHigh,
          borderRadius: radius,
          border: isUser ? null : Border.all(color: t.border),
          boxShadow: isUser
              ? const [
                  BoxShadow(
                    color: NexusColors.crimsonGlow,
                    blurRadius: 18,
                    spreadRadius: -6,
                    offset: Offset(0, 6),
                  ),
                ]
              : null,
        ),
        child: Padding(
          padding: const EdgeInsets.symmetric(
            horizontal: NexusSpacing.lg,
            vertical: NexusSpacing.md,
          ),
          child: child,
        ),
      ),
    );
  }
}

class _BubbleContent extends StatelessWidget {
  const _BubbleContent({required this.message});
  final ChatMessage message;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final isUser = message.role == MessageRole.user;

    if (message.streaming && message.content.isEmpty) {
      return const TypingDots();
    }

    if (isUser) {
      return SelectableText(
        message.content,
        style: const TextStyle(color: Colors.white, fontSize: 15, height: 1.45),
      );
    }

    return MarkdownBody(
      data: message.content.isEmpty ? '…' : message.content,
      selectable: true,
      builders: {'code': _CodeElementBuilder()},
      styleSheet: MarkdownStyleSheet(
        p: TextStyle(color: t.textPrimary, fontSize: 15, height: 1.55),
        strong: TextStyle(color: t.textPrimary, fontWeight: FontWeight.w700),
        code: GoogleFonts.jetBrainsMono(
          backgroundColor: t.surfaceHigher,
          color: NexusColors.crimson,
          fontSize: 13,
        ),
        codeblockPadding: EdgeInsets.zero,
        codeblockDecoration: const BoxDecoration(),
        blockquoteDecoration: const BoxDecoration(
          border: Border(left: BorderSide(color: NexusColors.crimson, width: 3)),
        ),
        blockquotePadding: const EdgeInsets.only(left: 12),
        h1: TextStyle(color: t.textPrimary, fontWeight: FontWeight.w800),
        h2: TextStyle(color: t.textPrimary, fontWeight: FontWeight.w800),
        listBullet: const TextStyle(color: NexusColors.crimson),
        a: const TextStyle(color: NexusColors.crimson, decoration: TextDecoration.underline),
      ),
    );
  }
}

/// Replaces fenced code blocks rendered by `flutter_markdown` with our
/// own `CodeBlock` widget (copy button + language pill).
class _CodeElementBuilder extends MarkdownElementBuilder {
  @override
  Widget? visitElementAfterWithContext(
    BuildContext context,
    md.Element element,
    TextStyle? preferredStyle,
    TextStyle? parentStyle,
  ) {
    final isFenced = element.attributes['class']?.startsWith('language-') ?? false;
    if (!isFenced) return null;
    final lang = (element.attributes['class'] ?? '').replaceFirst('language-', '');
    return CodeBlock(code: element.textContent, language: lang);
  }
}

// ============================================================================
// Typing dots
// ============================================================================

class TypingDots extends StatefulWidget {
  const TypingDots({super.key});

  @override
  State<TypingDots> createState() => _TypingDotsState();
}

class _TypingDotsState extends State<TypingDots> with SingleTickerProviderStateMixin {
  late final AnimationController _c =
      AnimationController(vsync: this, duration: const Duration(milliseconds: 900))..repeat();

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 36,
      height: 18,
      child: AnimatedBuilder(
        animation: _c,
        builder: (_, __) => Row(
          mainAxisSize: MainAxisSize.min,
          children: List.generate(3, (i) {
            final t = (_c.value + i * 0.2) % 1.0;
            final scale = 0.6 + (t < 0.5 ? t * 2 : (1 - t) * 2) * 0.4;
            return Padding(
              padding: const EdgeInsets.symmetric(horizontal: 2),
              child: Transform.scale(
                scale: scale,
                child: Container(
                  width: 6,
                  height: 6,
                  decoration: const BoxDecoration(
                    color: NexusColors.crimson,
                    shape: BoxShape.circle,
                  ),
                ),
              ),
            );
          }),
        ),
      ),
    );
  }
}

// ============================================================================
// Tool call strip
// ============================================================================

class _ToolCallStrip extends StatelessWidget {
  const _ToolCallStrip({required this.toolCalls});
  final List<ToolCall> toolCalls;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Wrap(
      spacing: NexusSpacing.xs,
      runSpacing: NexusSpacing.xs,
      children: toolCalls
          .map((tc) => Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                decoration: BoxDecoration(
                  color: t.surfaceHigh,
                  borderRadius: const BorderRadius.all(NexusRadius.pill),
                  border: Border.all(color: NexusColors.crimson.withOpacity(0.5)),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(Icons.build_rounded, size: 12, color: NexusColors.crimson),
                    const SizedBox(width: 4),
                    Text(
                      tc.tool,
                      style: TextStyle(
                        color: t.textPrimary,
                        fontSize: 11,
                        fontWeight: FontWeight.w700,
                        letterSpacing: 0.4,
                      ),
                    ),
                  ],
                ),
              ))
          .toList(),
    );
  }
}

// ============================================================================
// Citations strip
// ============================================================================

class CitationsStrip extends StatelessWidget {
  const CitationsStrip({super.key, required this.citations});
  final List<Citation> citations;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return SizedBox(
      height: 92,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        itemCount: citations.length,
        separatorBuilder: (_, __) => const SizedBox(width: NexusSpacing.sm),
        itemBuilder: (_, i) {
          final c = citations[i];
          return Container(
            width: 240,
            padding: const EdgeInsets.all(NexusSpacing.md),
            decoration: BoxDecoration(
              color: t.surfaceHigh,
              borderRadius: const BorderRadius.all(NexusRadius.md),
              border: Border.all(color: t.border),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    const Icon(Icons.description_outlined, size: 14, color: NexusColors.crimson),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text(
                        c.documentName,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          color: t.textPrimary,
                          fontWeight: FontWeight.w700,
                          fontSize: 12,
                        ),
                      ),
                    ),
                    Text(
                      '${(c.score * 100).toStringAsFixed(0)}%',
                      style: const TextStyle(
                        color: NexusColors.crimson,
                        fontSize: 11,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 6),
                Expanded(
                  child: Text(
                    c.excerpt,
                    maxLines: 3,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: t.textSecondary, fontSize: 11, height: 1.4),
                  ),
                ),
              ],
            ),
          );
        },
      ),
    );
  }
}

// ============================================================================
// Composer
// ============================================================================

class Composer extends StatefulWidget {
  const Composer({
    super.key,
    required this.onSend,
    required this.sending,
    required this.useRag,
    required this.useTools,
    required this.onToggleRag,
    required this.onToggleTools,
    required this.onStop,
  });

  final void Function(String) onSend;
  final bool sending;
  final bool useRag;
  final bool useTools;
  final ValueChanged<bool> onToggleRag;
  final ValueChanged<bool> onToggleTools;
  final VoidCallback onStop;

  @override
  State<Composer> createState() => _ComposerState();
}

class _ComposerState extends State<Composer> {
  final _controller = TextEditingController();
  final _focus = FocusNode();

  @override
  void dispose() {
    _controller.dispose();
    _focus.dispose();
    super.dispose();
  }

  void _send() {
    final text = _controller.text.trim();
    if (text.isEmpty || widget.sending) return;
    widget.onSend(text);
    _controller.clear();
    _focus.requestFocus();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return SafeArea(
      top: false,
      child: Container(
        decoration: BoxDecoration(
          color: t.background,
          border: Border(top: BorderSide(color: t.border)),
        ),
        padding: const EdgeInsets.fromLTRB(
            NexusSpacing.md, NexusSpacing.sm, NexusSpacing.md, NexusSpacing.md),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 920),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Row(
                children: [
                  _ToggleChip(
                    label: 'RAG',
                    active: widget.useRag,
                    icon: Icons.menu_book_rounded,
                    onTap: () => widget.onToggleRag(!widget.useRag),
                  ),
                  const SizedBox(width: NexusSpacing.sm),
                  _ToggleChip(
                    label: 'Tools',
                    active: widget.useTools,
                    icon: Icons.build_rounded,
                    onTap: () => widget.onToggleTools(!widget.useTools),
                  ),
                  const Spacer(),
                ],
              ),
              const SizedBox(height: NexusSpacing.sm),
              Container(
                decoration: BoxDecoration(
                  color: t.surfaceHigh,
                  borderRadius: const BorderRadius.all(NexusRadius.lg),
                  border: Border.all(color: t.border),
                ),
                padding: const EdgeInsets.symmetric(
                    horizontal: NexusSpacing.md, vertical: NexusSpacing.xs),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    Expanded(
                      child: TextField(
                        controller: _controller,
                        focusNode: _focus,
                        minLines: 1,
                        maxLines: 6,
                        textInputAction: TextInputAction.newline,
                        decoration: const InputDecoration(
                          hintText: 'Message NexusBot…',
                          border: InputBorder.none,
                          filled: false,
                          contentPadding: EdgeInsets.symmetric(vertical: 12),
                        ),
                      ),
                    ),
                    const SizedBox(width: 4),
                    _SendOrStop(
                      sending: widget.sending,
                      onSend: _send,
                      onStop: widget.onStop,
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ToggleChip extends StatelessWidget {
  const _ToggleChip({
    required this.label,
    required this.active,
    required this.icon,
    required this.onTap,
  });

  final String label;
  final bool active;
  final IconData icon;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: const BorderRadius.all(NexusRadius.pill),
        onTap: onTap,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 150),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
          decoration: BoxDecoration(
            color: active ? NexusColors.crimson.withOpacity(0.15) : t.surfaceHigh,
            borderRadius: const BorderRadius.all(NexusRadius.pill),
            border: Border.all(color: active ? NexusColors.crimson : t.border),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, size: 13, color: active ? NexusColors.crimson : t.textSecondary),
              const SizedBox(width: 6),
              Text(
                label,
                style: TextStyle(
                  color: active ? t.textPrimary : t.textSecondary,
                  fontSize: 12,
                  fontWeight: FontWeight.w700,
                  letterSpacing: 0.4,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _SendOrStop extends StatelessWidget {
  const _SendOrStop({
    required this.sending,
    required this.onSend,
    required this.onStop,
  });

  final bool sending;
  final VoidCallback onSend;
  final VoidCallback onStop;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return GestureDetector(
      onTap: sending ? onStop : onSend,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 6),
        width: 38,
        height: 38,
        decoration: BoxDecoration(
          gradient: sending ? null : NexusColors.redGradient,
          color: sending ? t.surfaceHigher : null,
          borderRadius: const BorderRadius.all(NexusRadius.md),
          boxShadow: sending
              ? null
              : const [
                  BoxShadow(
                    color: NexusColors.crimsonGlow,
                    blurRadius: 16,
                    spreadRadius: -4,
                    offset: Offset(0, 4),
                  ),
                ],
        ),
        child: Icon(
          sending ? Icons.stop_rounded : Icons.arrow_upward_rounded,
          color: sending ? t.textPrimary : Colors.white,
          size: 18,
        ),
      ),
    );
  }
}
