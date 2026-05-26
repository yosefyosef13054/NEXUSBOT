class User {
  const User({
    required this.id,
    required this.email,
    required this.role,
    this.fullName,
  });

  factory User.fromJson(Map<String, dynamic> json) => User(
        id: json['id'] as String,
        email: json['email'] as String,
        fullName: json['full_name'] as String?,
        role: json['role'] as String? ?? 'user',
      );

  final String id;
  final String email;
  final String? fullName;
  final String role;
}

class TokenPair {
  const TokenPair({required this.accessToken, required this.refreshToken, required this.expiresIn});

  factory TokenPair.fromJson(Map<String, dynamic> json) => TokenPair(
        accessToken: json['access_token'] as String,
        refreshToken: json['refresh_token'] as String,
        expiresIn: (json['expires_in'] as num).toInt(),
      );

  final String accessToken;
  final String refreshToken;
  final int expiresIn;
}
