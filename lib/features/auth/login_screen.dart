import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../core/widgets.dart';
import 'auth_providers.dart';
import 'auth_repository.dart';

class LoginScreen extends ConsumerStatefulWidget {
  const LoginScreen({super.key});

  @override
  ConsumerState<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends ConsumerState<LoginScreen> {
  final _formKey = GlobalKey<FormState>();
  final _email = TextEditingController();
  final _password = TextEditingController();
  bool _loading = false;
  bool _obscure = true;
  String? _error;

  @override
  void dispose() {
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
                            const NexusLogo(size: 64),
                            const SizedBox(height: NexusSpacing.lg),
                            const NexusWordmark(fontSize: 28),
                            const SizedBox(height: NexusSpacing.sm),
                            Text(
                              'WELCOME BACK',
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
                      const SizedBox(height: NexusSpacing.xxxl),
                      TextFormField(
                        controller: _email,
                        autofillHints: const [AutofillHints.email],
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
                        autofillHints: const [AutofillHints.password],
                        obscureText: _obscure,
                        textInputAction: TextInputAction.done,
                        onFieldSubmitted: (_) => _submit(),
                        decoration: InputDecoration(
                          hintText: 'Password',
                          prefixIcon: const Icon(Icons.lock_outline, size: 20),
                          suffixIcon: IconButton(
                            icon: Icon(_obscure ? Icons.visibility_outlined : Icons.visibility_off_outlined,
                                size: 20),
                            onPressed: () => setState(() => _obscure = !_obscure),
                          ),
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
                        label: 'Sign in',
                        icon: Icons.arrow_forward_rounded,
                        loading: _loading,
                        onPressed: _loading ? null : _submit,
                      ),
                      const SizedBox(height: NexusSpacing.lg),
                      Row(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          Text(
                            'New to NexusBot? ',
                            style: TextStyle(color: context.tokens.textSecondary),
                          ),
                          TextButton(
                            onPressed: () => context.go('/register'),
                            child: const Text(
                              'Create an account',
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
