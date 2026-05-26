import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'env.dart';
import 'secure_storage.dart';

/// Typed wrapper around a Dio failure so the UI layer never imports `dio`.
class ApiException implements Exception {
  ApiException({required this.code, required this.message, this.details, this.statusCode});

  factory ApiException.fromDio(DioException e) {
    final body = e.response?.data;
    if (body is Map && body['error'] is Map) {
      final m = (body['error'] as Map).cast<String, dynamic>();
      return ApiException(
        code: (m['code'] ?? 'unknown') as String,
        message: (m['message'] ?? 'Request failed') as String,
        details: (m['details'] as Map?)?.cast<String, dynamic>(),
        statusCode: e.response?.statusCode,
      );
    }
    return ApiException(
      code: 'network_error',
      message: e.message ?? 'Network error',
      statusCode: e.response?.statusCode,
    );
  }

  final String code;
  final String message;
  final Map<String, dynamic>? details;
  final int? statusCode;

  bool get isAuth => code == 'unauthorized' || statusCode == 401;
  bool get isRateLimited => code == 'rate_limited' || statusCode == 429;

  @override
  String toString() => 'ApiException($code: $message)';
}

/// Authenticated API client.
///
/// - Injects `Authorization: Bearer <jwt>` on every request from secure storage.
/// - On 401, transparently refreshes the access token using the refresh token,
///   retries the original request, and only fails if the refresh itself fails.
class ApiClient {
  ApiClient({required this.dio, required this.tokens});

  final Dio dio;
  final TokenStorage tokens;

  static Future<ApiClient> create(TokenStorage tokens) async {
    final dio = Dio(
      BaseOptions(
        baseUrl: Env.apiBaseUrl,
        connectTimeout: const Duration(seconds: 15),
        receiveTimeout: const Duration(seconds: 60),
        sendTimeout: const Duration(seconds: 30),
        headers: {'Content-Type': 'application/json'},
        validateStatus: (s) => s != null && s < 500,
      ),
    );
    final client = ApiClient(dio: dio, tokens: tokens);
    dio.interceptors.add(_AuthInterceptor(client));
    return client;
  }

  /// Public wrapper that converts Dio exceptions to [ApiException].
  Future<Response<T>> request<T>(
    String path, {
    String method = 'GET',
    Object? data,
    Map<String, dynamic>? query,
    Options? options,
  }) async {
    try {
      return await dio.request<T>(
        path,
        data: data,
        queryParameters: query,
        options: (options ?? Options()).copyWith(method: method),
      );
    } on DioException catch (e) {
      throw ApiException.fromDio(e);
    }
  }
}

class _AuthInterceptor extends Interceptor {
  _AuthInterceptor(this.client);
  final ApiClient client;
  bool _refreshing = false;

  @override
  Future<void> onRequest(RequestOptions options, RequestInterceptorHandler handler) async {
    if (options.headers['Authorization'] == null) {
      final access = await client.tokens.readAccess();
      if (access != null) {
        options.headers['Authorization'] = 'Bearer $access';
      }
    }
    handler.next(options);
  }

  @override
  Future<void> onResponse(Response response, ResponseInterceptorHandler handler) async {
    // Treat 401 responses (returned without throwing because validateStatus < 500) as auth errors.
    if (response.statusCode == 401) {
      final retried = await _tryRefreshAndRetry(response.requestOptions);
      if (retried != null) {
        return handler.resolve(retried);
      }
    }
    handler.next(response);
  }

  @override
  Future<void> onError(DioException err, ErrorInterceptorHandler handler) async {
    if (err.response?.statusCode == 401) {
      final retried = await _tryRefreshAndRetry(err.requestOptions);
      if (retried != null) {
        return handler.resolve(retried);
      }
    }
    handler.next(err);
  }

  Future<Response?> _tryRefreshAndRetry(RequestOptions original) async {
    // Don't recurse on the refresh endpoint itself.
    if (original.path.contains('/auth/refresh')) return null;
    if (_refreshing) return null;
    _refreshing = true;
    try {
      final refresh = await client.tokens.readRefresh();
      if (refresh == null) return null;
      final res = await Dio(BaseOptions(baseUrl: Env.apiBaseUrl)).post(
        '/auth/refresh',
        data: {'refresh_token': refresh},
      );
      final access = res.data['access_token'] as String;
      final newRefresh = res.data['refresh_token'] as String;
      await client.tokens.writeTokens(access: access, refresh: newRefresh);
      original.headers['Authorization'] = 'Bearer $access';
      return await client.dio.fetch(original);
    } catch (_) {
      await client.tokens.clear();
      return null;
    } finally {
      _refreshing = false;
    }
  }
}

final apiClientProvider = FutureProvider<ApiClient>((ref) async {
  final tokens = ref.watch(tokenStorageProvider);
  return ApiClient.create(tokens);
});
