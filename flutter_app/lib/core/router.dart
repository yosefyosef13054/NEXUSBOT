import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/auth/auth_providers.dart';
import '../features/auth/login_screen.dart';
import '../features/auth/register_screen.dart';
import '../features/auth/splash_screen.dart';
import '../shared/home_shell.dart';

final routerProvider = Provider<GoRouter>((ref) {
  // Force-refresh the router whenever auth state flips.
  final auth = ref.watch(currentUserProvider);

  return GoRouter(
    initialLocation: '/splash',
    debugLogDiagnostics: false,
    refreshListenable: _ProviderRefresh(ref),
    redirect: (context, state) {
      final loc = state.matchedLocation;
      final isAuthRoute = loc == '/login' || loc == '/register';
      if (loc == '/splash') {
        return auth.when(
          loading: () => null,
          error: (_, __) => '/login',
          data: (u) => u == null ? '/login' : '/',
        );
      }
      // While auth is loading anywhere else, leave the route as-is.
      if (auth.isLoading) return null;
      final loggedIn = auth.valueOrNull != null;
      if (!loggedIn && !isAuthRoute) return '/login';
      if (loggedIn && isAuthRoute) return '/';
      return null;
    },
    routes: [
      GoRoute(path: '/splash', builder: (_, __) => const SplashScreen()),
      GoRoute(path: '/login', builder: (_, __) => const LoginScreen()),
      GoRoute(path: '/register', builder: (_, __) => const RegisterScreen()),
      GoRoute(path: '/', builder: (_, __) => const HomeShell()),
    ],
  );
});

/// Bridges Riverpod state changes to GoRouter's Listenable-based refresh.
class _ProviderRefresh extends ChangeNotifier {
  _ProviderRefresh(Ref ref) {
    ref.listen(currentUserProvider, (_, __) => notifyListeners());
  }
}
