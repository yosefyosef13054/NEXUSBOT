import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme.dart';
import '../../core/widgets.dart';
import 'auth_providers.dart';

class SplashScreen extends ConsumerWidget {
  const SplashScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    ref.listen(currentUserProvider, (_, next) {
      next.whenOrNull(
        data: (user) {
          if (!context.mounted) return;
          context.go(user == null ? '/login' : '/');
        },
      );
    });

    return Scaffold(
      body: EmberBackground(
        child: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const NexusLogo(size: 92),
              const SizedBox(height: NexusSpacing.xl),
              const NexusWordmark(fontSize: 38),
              const SizedBox(height: NexusSpacing.sm),
              Text(
                'AI ENGINE · LOADING',
                style: TextStyle(
                  color: NexusColors.crimson,
                  fontSize: 11,
                  fontWeight: FontWeight.w700,
                  letterSpacing: 4,
                ),
              ),
              const SizedBox(height: NexusSpacing.xxl),
              const SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(
                  strokeWidth: 2,
                  valueColor: AlwaysStoppedAnimation(NexusColors.crimson),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
