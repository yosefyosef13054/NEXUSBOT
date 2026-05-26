import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../core/widgets.dart';
import 'auth_providers.dart';
import 'auth_repository.dart';

class RegisterScreen extends ConsumerStatefulWidget {
  const RegisterScreen({super.key});

  @override
  ConsumerState<RegisterScreen> createState() => _RegisterScreenState();
}

class _RegisterScreenState extends ConsumerState<RegisterScreen> {
  final _formKey = GlobalKey<FormState>();
  final _name = TextEditingController();
  final _email = TextEditingController();
  final _password = TextEditingController();
  bool _loading = false;
  String? _error;

  @override
  void dispose() {
    _name.dispose();
    _email.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final repo = await ref.read(authRepositoryProvider.future);
      await repo.register(
        email: _email.text.trim(),
        password: _password.text,
        fullName: _name.text.trim(),
      );
      await repo.login(email: _email.text.trim(), password: _password.text);
      ref.invalidate(currentUserProvider);
      if (mounted) context.go('/');
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.arrow_back_rounded),
          onPressed: () => context.go('/login'),
        ),
      ),
      body: EmberBackground(
        child: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(NexusSpacing.xl),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 420),
                child: Form(
                  key: _formKey,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Center(
                        child: Column(
                          children: [
                            const NexusLogo(size: 56),
                            const SizedBox(height: NexusSpacing.lg),
                            const NexusWordmark(fontSize: 24),
                            const SizedBox(height: NexusSpacing.sm),
                            Text(
                              'CREATE ACCOUNT',
                              style: TextStyle(
                                color: NexusColors.crimson,
                                fontSize: 11,
                                fontWeight: FontWeight.w700,
                                letterSpacing: 4,
                              ),
                            ),
                          ],
                        ),
                      ),
                      const SizedBox(height: NexusSpacing.xxl),
                      TextFormField(
                        controller: _name,
                        textInputAction: TextInputAction.next,
                        decoration: const InputDecoration(
                          hintText: 'Full name (optional)',
                          prefixIcon: Icon(Icons.person_outline, size: 20),
                        ),
                      ),
                      const SizedBox(height: NexusSpacing.md),
                      TextFormField(
                        controller: _email,
                        keyboardType: TextInputType.emailAddress,
                        textInputAction: TextInputAction.next,
                        decoration: const InputDecoration(
                          hintText: 'Email',
                          prefixIcon: Icon(Icons.mail_outline, size: 20),
                        ),
                        validator: (v) {
                          final s = (v ?? '').trim();
                          if (s.isEmpty) return 'Email required';
                          if (!s.contains('@')) return 'Invalid email';
                          return null;
                        },
                      ),
                      const SizedBox(height: NexusSpacing.md),
                      TextFormField(
                        controller: _password,
                        obscureText: true,
                        textInputAction: TextInputAction.done,
                        onFieldSubmitted: (_) => _submit(),
                        decoration: const InputDecoration(
                          hintText: 'Password (min 8 chars)',
                          prefixIcon: Icon(Icons.lock_outline, size: 20),
                        ),
                        validator: (v) =>
                            (v ?? '').length < 8 ? 'Password must be at least 8 characters' : null,
                      ),
                      if (_error != null) ...[
                        const SizedBox(height: NexusSpacing.md),
                        _ErrorBanner(message: _error!),
                      ],
                      const SizedBox(height: NexusSpacing.xl),
                      NexusButton(
                        label: 'Create account',
                        icon: Icons.bolt_rounded,
                        loading: _loading,
                        onPressed: _loading ? null : _submit,
                      ),
                      const SizedBox(height: NexusSpacing.lg),
                      Row(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          Text(
                            'Already have an account? ',
                            style: TextStyle(color: context.tokens.textSecondary),
                          ),
                          TextButton(
                            onPressed: () => context.go('/login'),
                            child: const Text(
                              'Sign in',
                              style: TextStyle(color: NexusColors.crimson, fontWeight: FontWeight.w700),
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _ErrorBanner extends StatelessWidget {
  const _ErrorBanner({required this.message});
  final String message;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: const Color(0x33EF4444),
        borderRadius: const BorderRadius.all(NexusRadius.md),
        border: Border.all(color: NexusColors.crimson, width: 1),
      ),
      child: Row(
        children: [
          const Icon(Icons.error_outline, color: NexusColors.crimson, size: 18),
          const SizedBox(width: NexusSpacing.sm),
          Expanded(
            child: Text(
              message,
              style: TextStyle(color: context.tokens.textPrimary, fontSize: 13),
            ),
          ),
        ],
      ),
    );
  }
}
