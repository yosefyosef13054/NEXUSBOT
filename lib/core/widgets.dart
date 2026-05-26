import 'package:flutter/material.dart';
import 'package:flutter_svg/flutter_svg.dart';

import 'theme.dart';

/// Brand logo widget.
class NexusLogo extends StatelessWidget {
  const NexusLogo({super.key, this.size = 56});
  final double size;

  @override
  Widget build(BuildContext context) {
    return SvgPicture.asset(
      'assets/logo.svg',
      width: size,
      height: size,
      semanticsLabel: 'NexusBot',
    );
  }
}

/// Wordmark — `NEXUS` (theme text) + `BOT` (crimson).
class NexusWordmark extends StatelessWidget {
  const NexusWordmark({super.key, this.fontSize = 28});
  final double fontSize;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final style = TextStyle(
      fontSize: fontSize,
      fontWeight: FontWeight.w900,
      letterSpacing: fontSize * 0.06,
      color: t.textPrimary,
      height: 1,
    );
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Text('NEXUS', style: style),
        Text('BOT', style: style.copyWith(color: NexusColors.crimson)),
      ],
    );
  }
}

/// Crimson-gradient primary button.
class NexusButton extends StatelessWidget {
  const NexusButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.icon,
    this.loading = false,
    this.expand = true,
  });

  final String label;
  final VoidCallback? onPressed;
  final IconData? icon;
  final bool loading;
  final bool expand;

  @override
  Widget build(BuildContext context) {
    final disabled = onPressed == null || loading;
    final button = AnimatedOpacity(
      duration: const Duration(milliseconds: 120),
      opacity: disabled ? 0.55 : 1,
      child: DecoratedBox(
        decoration: BoxDecoration(
          gradient: NexusColors.redGradient,
          borderRadius: const BorderRadius.all(NexusRadius.md),
          boxShadow: disabled
              ? const []
              : const [
                  BoxShadow(
                    color: NexusColors.crimsonGlow,
                    blurRadius: 24,
                    spreadRadius: -4,
                    offset: Offset(0, 4),
                  ),
                ],
        ),
        child: Material(
          color: Colors.transparent,
          child: InkWell(
            onTap: disabled ? null : onPressed,
            borderRadius: const BorderRadius.all(NexusRadius.md),
            child: Padding(
              padding: const EdgeInsets.symmetric(
                horizontal: NexusSpacing.xl,
                vertical: NexusSpacing.md + 2,
              ),
              child: Row(
                mainAxisSize: expand ? MainAxisSize.max : MainAxisSize.min,
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  if (loading) ...[
                    const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(
                        strokeWidth: 2,
                        valueColor: AlwaysStoppedAnimation(Colors.white),
                      ),
                    ),
                    const SizedBox(width: NexusSpacing.md),
                  ] else if (icon != null) ...[
                    Icon(icon, size: 18, color: Colors.white),
                    const SizedBox(width: NexusSpacing.sm),
                  ],
                  Text(
                    label,
                    style: const TextStyle(
                      color: Colors.white,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 0.3,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
    return expand ? SizedBox(width: double.infinity, child: button) : button;
  }
}

class NexusGhostButton extends StatelessWidget {
  const NexusGhostButton({super.key, required this.label, required this.onPressed, this.icon});
  final String label;
  final VoidCallback? onPressed;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return OutlinedButton.icon(
      onPressed: onPressed,
      icon: icon == null ? const SizedBox.shrink() : Icon(icon, size: 16),
      label: Text(label),
      style: OutlinedButton.styleFrom(
        foregroundColor: t.textPrimary,
        side: BorderSide(color: t.border),
        backgroundColor: t.surfaceHigh,
        shape: const RoundedRectangleBorder(borderRadius: BorderRadius.all(NexusRadius.md)),
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
      ),
    );
  }
}

/// Faint ember-glow background for hero screens (splash, auth).
class EmberBackground extends StatelessWidget {
  const EmberBackground({super.key, required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return DecoratedBox(
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: t.isDark
              ? const [Color(0xFF050505), Color(0xFF180606)]
              : const [Color(0xFFFAFAFA), Color(0xFFFFF1F1)],
        ),
      ),
      child: Stack(
        children: [
          Positioned(
            top: -120,
            right: -120,
            child: Container(
              width: 420,
              height: 420,
              decoration: const BoxDecoration(
                shape: BoxShape.circle,
                gradient: NexusColors.emberGlow,
              ),
            ),
          ),
          Positioned.fill(child: child),
        ],
      ),
    );
  }
}

/// Generic empty state — used by session list, document list.
class EmptyState extends StatelessWidget {
  const EmptyState({
    super.key,
    required this.icon,
    required this.title,
    this.subtitle,
    this.action,
  });

  final IconData icon;
  final String title;
  final String? subtitle;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(NexusSpacing.xl),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              padding: const EdgeInsets.all(20),
              decoration: BoxDecoration(
                color: t.surfaceHigh,
                borderRadius: const BorderRadius.all(NexusRadius.lg),
                border: Border.all(color: t.border),
              ),
              child: Icon(icon, size: 36, color: NexusColors.crimson),
            ),
            const SizedBox(height: NexusSpacing.lg),
            Text(
              title,
              style: TextStyle(
                color: t.textPrimary,
                fontWeight: FontWeight.w700,
                fontSize: 18,
              ),
            ),
            if (subtitle != null) ...[
              const SizedBox(height: NexusSpacing.sm),
              Text(
                subtitle!,
                textAlign: TextAlign.center,
                style: TextStyle(color: t.textSecondary, fontSize: 14),
              ),
            ],
            if (action != null) ...[
              const SizedBox(height: NexusSpacing.xl),
              action!,
            ],
          ],
        ),
      ),
    );
  }
}

/// Shimmer-style skeleton block. Used in loading states.
class Skeleton extends StatefulWidget {
  const Skeleton({
    super.key,
    this.width = double.infinity,
    this.height = 16,
    this.radius = 8,
  });

  final double width;
  final double height;
  final double radius;

  @override
  State<Skeleton> createState() => _SkeletonState();
}

class _SkeletonState extends State<Skeleton> with SingleTickerProviderStateMixin {
  late final AnimationController _c =
      AnimationController(vsync: this, duration: const Duration(milliseconds: 1200))..repeat();

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return AnimatedBuilder(
      animation: _c,
      builder: (_, __) {
        return Container(
          width: widget.width,
          height: widget.height,
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(widget.radius),
            gradient: LinearGradient(
              colors: [t.surfaceHigh, t.surfaceHigher, t.surfaceHigh],
              stops: const [0, 0.5, 1],
              begin: Alignment(-1 + _c.value * 2, 0),
              end: Alignment(1 + _c.value * 2, 0),
            ),
          ),
        );
      },
    );
  }
}

/// Skeleton list row used by session/document loading states.
class SkeletonRow extends StatelessWidget {
  const SkeletonRow({super.key});

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      padding: const EdgeInsets.all(NexusSpacing.lg),
      decoration: BoxDecoration(
        color: t.surfaceHigh,
        borderRadius: const BorderRadius.all(NexusRadius.lg),
        border: Border.all(color: t.border),
      ),
      child: Row(
        children: [
          const Skeleton(width: 44, height: 44, radius: 12),
          const SizedBox(width: NexusSpacing.md),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: const [
                Skeleton(width: 160, height: 14),
                SizedBox(height: 8),
                Skeleton(width: 90, height: 10),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
