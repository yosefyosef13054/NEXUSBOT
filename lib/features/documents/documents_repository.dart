import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_client.dart';
import 'document_model.dart';

class DocumentsRepository {
  DocumentsRepository(this._client);
  final ApiClient _client;

  Future<List<Document>> list({int page = 1, int pageSize = 50}) async {
    final res = await _client.request<Map<String, dynamic>>(
      '/documents',
      query: {'page': page, 'page_size': pageSize},
    );
    return (res.data!['items'] as List)
        .cast<Map>()
        .map((m) => Document.fromJson(m.cast<String, dynamic>()))
        .toList();
  }

  /// Upload with progress reporting (0..1). Caller can rebuild a progress bar
  /// from the `onProgress` callback.
  Future<Document> upload({
    required String path,
    required String filename,
    String? sessionId,
    void Function(double progress)? onProgress,
  }) async {
    final form = FormData.fromMap({
      'file': await MultipartFile.fromFile(path, filename: filename),
      if (sessionId != null) 'session_id': sessionId,
    });
    final res = await _client.dio.post<Map<String, dynamic>>(
      '/documents',
      data: form,
      onSendProgress: (sent, total) {
        if (total > 0 && onProgress != null) onProgress(sent / total);
      },
    );
    return Document.fromJson((res.data!['document'] as Map).cast<String, dynamic>());
  }

  Future<void> delete(String id) async {
    await _client.request<Map<String, dynamic>>('/documents/$id', method: 'DELETE');
  }
}

final documentsRepositoryProvider = FutureProvider<DocumentsRepository>((ref) async {
  final client = await ref.watch(apiClientProvider.future);
  return DocumentsRepository(client);
});

final documentListProvider = FutureProvider.autoDispose<List<Document>>((ref) async {
  final repo = await ref.watch(documentsRepositoryProvider.future);
  return repo.list();
});
