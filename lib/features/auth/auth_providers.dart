import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_client.dart';
import '../../core/secure_storage.dart';
import 'auth_models.dart';
import 'auth_repository.dart';

/// Lazily loaded "current user". Returns null when no valid token exists.
final currentUserProvider = FutureProvider<User?>((ref) async {
  final access = await ref.watch(tokenStorageProvider).readAccess();
  if (access == null) return null;
  try {
    final repo = await ref.watch(authRepositoryProvider.future);
    return await repo.me();
  } on ApiException {
    return null;
  }
});

/// Convenience boolean for the router redirect.
final isAuthenticatedProvider = Provider<AsyncValue<bool>>((ref) {
  return ref.watch(currentUserProvider).whenData((u) => u != null);
});
