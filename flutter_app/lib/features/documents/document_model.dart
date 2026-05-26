enum DocumentStatus { pending, processing, ready, failed, unknown }

class Document {
  const Document({
    required this.id,
    required this.name,
    required this.mimeType,
    required this.sizeBytes,
    required this.status,
    required this.createdAt,
    this.error,
  });

  factory Document.fromJson(Map<String, dynamic> j) => Document(
        id: j['id'] as String,
        name: j['name'] as String,
        mimeType: j['mime_type'] as String,
        sizeBytes: (j['size_bytes'] as num).toInt(),
        status: _parseStatus(j['status'] as String),
        error: j['error'] as String?,
        createdAt: DateTime.parse(j['created_at'] as String),
      );

  final String id;
  final String name;
  final String mimeType;
  final int sizeBytes;
  final DocumentStatus status;
  final String? error;
  final DateTime createdAt;
}

DocumentStatus _parseStatus(String s) {
  switch (s) {
    case 'pending':
      return DocumentStatus.pending;
    case 'processing':
      return DocumentStatus.processing;
    case 'ready':
      return DocumentStatus.ready;
    case 'failed':
      return DocumentStatus.failed;
    default:
      return DocumentStatus.unknown;
  }
}
