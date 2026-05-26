import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_client.dart';
import '../../core/secure_storage.dart';
import 'auth_models.dart';

class AuthRepository {
  AuthRepository(this._client, this._tokens);
  final ApiClient _client;
  final TokenStorage _tokens;

  Future<User> register({
    required String email,
    required String password,
    String? fullName,
  }) async {
    final res = await _client.request<Map<String, dynamic>>(
      '/auth/register',
      method: 'POST',
      data: {
        'email': email,
        'password': password,
        if (fullName != null && fullName.isNotEmpty) 'full_name': fullName,
      },
    );
    return User.fromJson(res.data!);
  }

  Future<User> login({required String email, required String password}) async {
    final res = await _client.request<Map<String, dynamic>>(
      '/auth/login',
      method: 'POST',
      data: {'email': email, 'password': password},
    );
    final pair = TokenPair.fromJson(res.data!);
    await _tokens.writeTokens(access: pair.accessToken, refresh: pair.refreshToken);
    return me();
  }

  Future<User> me() async {
    final res = await _client.request<Map<String, dynamic>>('/auth/me');
    return User.fromJson(res.data!);
  }

  Future<void> logout() async {
    await _tokens.clear();
  }
}

final authRepositoryProvider = FutureProvider<AuthRepository>((ref) async {
  final client = await ref.watch(apiClientProvider.future);
  return AuthRepository(client, ref.watch(tokenStorageProvider));
});
