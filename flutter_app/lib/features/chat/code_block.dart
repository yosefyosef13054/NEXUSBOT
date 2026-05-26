import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:google_fonts/google_fonts.dart';

import '../../core/theme.dart';

/// Self-contained code block widget used by the markdown renderer.
///
/// Shows: language pill, copy button, monospace body. Pluggable: when we wire
/// in real syntax highlighting (`re_highlight` etc.) we only touch the body
/// `Text` here — the chrome stays.
class CodeBlock extends StatefulWidget {
  const CodeBlock({super.key, required this.code, this.language});
  final String code;
  final String? language;

  @override
  State<CodeBlock> createState() => _CodeBlockState();
}

class _CodeBlockState extends State<CodeBlock> {
  bool _copied = false;

  Future<void> _copy() async {
    await Clipboard.setData(ClipboardData(text: widget.code));
    if (!mounted) return;
    setState(() => _copied = true);
    Future.delayed(const Duration(milliseconds: 1400), () {
      if (mounted) setState(() => _copied = false);
    });
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final mono = GoogleFonts.jetBrainsMono(
      color: t.textPrimary,
      fontSize: 13,
      height: 1.5,
    );
    final lang = (widget.language ?? '').trim();
    return Container(
      margin: const EdgeInsets.symmetric(vertical: NexusSpacing.sm),
      decoration: BoxDecoration(
        color: t.isDark ? const Color(0xFF09090B) : const Color(0xFFF4F4F5),
        borderRadius: const BorderRadius.all(NexusRadius.md),
        border: Border.all(color: t.border),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            decoration: BoxDecoration(
              border: Border(bottom: BorderSide(color: t.borderSubtle)),
            ),
            child: Row(
              children: [
                Container(
                  width: 8,
                  height: 8,
                  decoration: const BoxDecoration(
                    color: NexusColors.crimson,
                    shape: BoxShape.circle,
                  ),
                ),
                const SizedBox(width: 8),
                Text(
                  lang.isEmpty ? 'code' : lang.toLowerCase(),
                  style: TextStyle(
                    color: t.textMuted,
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 1.2,
                  ),
                ),
                const Spacer(),
                _CopyChip(copied: _copied, onTap: _copy),
              ],
            ),
          ),
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: SelectableText(widget.code, style: mono),
            ),
          ),
        ],
      ),
    );
  }
}

class _CopyChip extends StatelessWidget {
  const _CopyChip({required this.copied, required this.onTap});
  final bool copied;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: Colors.transparent,
      child: InkWell(
        onTap: onTap,
        borderRadius: const BorderRadius.all(NexusRadius.pill),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                copied ? Icons.check_rounded : Icons.copy_rounded,
                size: 12,
                color: copied ? NexusColors.success : t.textMuted,
              ),
              const SizedBox(width: 4),
              Text(
                copied ? 'Copied' : 'Copy',
                style: TextStyle(
                  color: copied ? NexusColors.success : t.textMuted,
                  fontSize: 10,
                  fontWeight: FontWeight.w700,
                  letterSpacing: 0.6,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
