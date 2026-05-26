import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

/// Brand palette. Same crimson identity across light + dark.
class NexusColors {
  const NexusColors._();

  // Brand reds (identical in both themes)
  static const Color crimson = Color(0xFFEF4444);
  static const Color crimsonDeep = Color(0xFFB91C1C);
  static const Color crimsonDark = Color(0xFF7F1D1D);
  static const Color crimsonGlow = Color(0x55EF4444);

  // States
  static const Color success = Color(0xFF22C55E);
  static const Color warn = Color(0xFFF59E0B);
  static const Color danger = Color(0xFFEF4444);

  // Back-compat constants for code paths that still read direct colors.
  // Prefer `context.tokens` in new code.
  static const Color background = _darkBg;
  static const Color surface = _darkSurface;
  static const Color surfaceHigh = _darkSurfaceHigh;
  static const Color surfaceHigher = _darkSurfaceHigher;
  static const Color border = _darkBorder;
  static const Color borderSubtle = _darkBorderSubtle;
  static const Color textPrimary = _darkText;
  static const Color textSecondary = _darkTextSecondary;
  static const Color textMuted = _darkTextMuted;

  // ---- Dark palette ----
  static const Color _darkBg = Color(0xFF050505);
  static const Color _darkSurface = Color(0xFF0E0E10);
  static const Color _darkSurfaceHigh = Color(0xFF18181B);
  static const Color _darkSurfaceHigher = Color(0xFF27272A);
  static const Color _darkBorder = Color(0xFF27272A);
  static const Color _darkBorderSubtle = Color(0xFF1F1F23);
  static const Color _darkText = Color(0xFFF8FAFC);
  static const Color _darkTextSecondary = Color(0xFFA1A1AA);
  static const Color _darkTextMuted = Color(0xFF71717A);

  // ---- Light palette ----
  static const Color _lightBg = Color(0xFFFAFAFA);
  static const Color _lightSurface = Color(0xFFFFFFFF);
  static const Color _lightSurfaceHigh = Color(0xFFF4F4F5);
  static const Color _lightSurfaceHigher = Color(0xFFE4E4E7);
  static const Color _lightBorder = Color(0xFFE4E4E7);
  static const Color _lightBorderSubtle = Color(0xFFEFEFF1);
  static const Color _lightText = Color(0xFF09090B);
  static const Color _lightTextSecondary = Color(0xFF52525B);
  static const Color _lightTextMuted = Color(0xFF71717A);

  // Gradients
  static const LinearGradient redGradient = LinearGradient(
    colors: [crimson, crimsonDeep, crimsonDark],
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
  );

  static const LinearGradient surfaceGradient = LinearGradient(
    colors: [_darkSurfaceHigh, _darkSurface],
    begin: Alignment.topCenter,
    end: Alignment.bottomCenter,
  );

  static const RadialGradient emberGlow = RadialGradient(
    colors: [Color(0x55EF4444), Color(0x33B91C1C), Color(0x00000000)],
    stops: [0.0, 0.4, 1.0],
  );
}

/// Theme-aware semantic tokens. Read from `context.tokens`.
///
/// New code should ALWAYS use this rather than the back-compat constants on
/// [NexusColors]; the constants only exist so older widgets keep compiling
/// during the migration.
@immutable
class NexusTokens extends ThemeExtension<NexusTokens> {
  const NexusTokens({
    required this.background,
    required this.surface,
    required this.surfaceHigh,
    required this.surfaceHigher,
    required this.border,
    required this.borderSubtle,
    required this.textPrimary,
    required this.textSecondary,
    required this.textMuted,
    required this.isDark,
  });

  final Color background;
  final Color surface;
  final Color surfaceHigh;
  final Color surfaceHigher;
  final Color border;
  final Color borderSubtle;
  final Color textPrimary;
  final Color textSecondary;
  final Color textMuted;
  final bool isDark;

  static const NexusTokens dark = NexusTokens(
    background: NexusColors._darkBg,
    surface: NexusColors._darkSurface,
    surfaceHigh: NexusColors._darkSurfaceHigh,
    surfaceHigher: NexusColors._darkSurfaceHigher,
    border: NexusColors._darkBorder,
    borderSubtle: NexusColors._darkBorderSubtle,
    textPrimary: NexusColors._darkText,
    textSecondary: NexusColors._darkTextSecondary,
    textMuted: NexusColors._darkTextMuted,
    isDark: true,
  );

  static const NexusTokens light = NexusTokens(
    background: NexusColors._lightBg,
    surface: NexusColors._lightSurface,
    surfaceHigh: NexusColors._lightSurfaceHigh,
    surfaceHigher: NexusColors._lightSurfaceHigher,
    border: NexusColors._lightBorder,
    borderSubtle: NexusColors._lightBorderSubtle,
    textPrimary: NexusColors._lightText,
    textSecondary: NexusColors._lightTextSecondary,
    textMuted: NexusColors._lightTextMuted,
    isDark: false,
  );

  @override
  ThemeExtension<NexusTokens> copyWith() => this;

  @override
  ThemeExtension<NexusTokens> lerp(ThemeExtension<NexusTokens>? other, double t) {
    if (other is! NexusTokens) return this;
    return NexusTokens(
      background: Color.lerp(background, other.background, t)!,
      surface: Color.lerp(surface, other.surface, t)!,
      surfaceHigh: Color.lerp(surfaceHigh, other.surfaceHigh, t)!,
      surfaceHigher: Color.lerp(surfaceHigher, other.surfaceHigher, t)!,
      border: Color.lerp(border, other.border, t)!,
      borderSubtle: Color.lerp(borderSubtle, other.borderSubtle, t)!,
      textPrimary: Color.lerp(textPrimary, other.textPrimary, t)!,
      textSecondary: Color.lerp(textSecondary, other.textSecondary, t)!,
      textMuted: Color.lerp(textMuted, other.textMuted, t)!,
      isDark: t < 0.5 ? isDark : other.isDark,
    );
  }
}

extension TokensExt on BuildContext {
  NexusTokens get tokens => Theme.of(this).extension<NexusTokens>()!;
}

/// Spacing scale (4-pt grid).
class NexusSpacing {
  const NexusSpacing._();
  static const double xs = 4;
  static const double sm = 8;
  static const double md = 12;
  static const double lg = 16;
  static const double xl = 24;
  static const double xxl = 32;
  static const double xxxl = 48;
}

class NexusRadius {
  const NexusRadius._();
  static const Radius sm = Radius.circular(8);
  static const Radius md = Radius.circular(12);
  static const Radius lg = Radius.circular(16);
  static const Radius xl = Radius.circular(20);
  static const Radius pill = Radius.circular(999);
}

ThemeData buildNexusTheme({required bool dark}) {
  final tokens = dark ? NexusTokens.dark : NexusTokens.light;
  final base = dark ? ThemeData.dark(useMaterial3: true) : ThemeData.light(useMaterial3: true);

  final textTheme = GoogleFonts.interTextTheme(base.textTheme).apply(
    bodyColor: tokens.textPrimary,
    displayColor: tokens.textPrimary,
  );

  return base.copyWith(
    extensions: <ThemeExtension<dynamic>>[tokens],
    scaffoldBackgroundColor: tokens.background,
    canvasColor: tokens.surface,
    colorScheme: (dark ? const ColorScheme.dark() : const ColorScheme.light()).copyWith(
      primary: NexusColors.crimson,
      onPrimary: Colors.white,
      secondary: NexusColors.crimsonDeep,
      onSecondary: Colors.white,
      surface: tokens.surface,
      onSurface: tokens.textPrimary,
      error: NexusColors.danger,
      onError: Colors.white,
      outline: tokens.border,
      surfaceContainerHighest: tokens.surfaceHigh,
    ),
    textTheme: textTheme.copyWith(
      displayLarge: textTheme.displayLarge?.copyWith(
        fontWeight: FontWeight.w900,
        letterSpacing: -1,
      ),
      headlineLarge: textTheme.headlineLarge?.copyWith(fontWeight: FontWeight.w800),
      headlineMedium: textTheme.headlineMedium?.copyWith(fontWeight: FontWeight.w800),
      headlineSmall: textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w700),
      titleLarge: textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700),
      labelLarge: textTheme.labelLarge?.copyWith(
        fontWeight: FontWeight.w700,
        letterSpacing: 0.4,
      ),
    ),
    appBarTheme: AppBarTheme(
      backgroundColor: tokens.background,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
      scrolledUnderElevation: 0,
      centerTitle: false,
      iconTheme: IconThemeData(color: tokens.textPrimary),
      titleTextStyle: TextStyle(
        color: tokens.textPrimary,
        fontWeight: FontWeight.w800,
        fontSize: 18,
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: tokens.surfaceHigh,
      hintStyle: TextStyle(color: tokens.textMuted),
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
      enabledBorder: OutlineInputBorder(
        borderRadius: const BorderRadius.all(NexusRadius.md),
        borderSide: BorderSide(color: tokens.border, width: 1),
      ),
      focusedBorder: const OutlineInputBorder(
        borderRadius: BorderRadius.all(NexusRadius.md),
        borderSide: BorderSide(color: NexusColors.crimson, width: 1.4),
      ),
      errorBorder: const OutlineInputBorder(
        borderRadius: BorderRadius.all(NexusRadius.md),
        borderSide: BorderSide(color: NexusColors.danger, width: 1),
      ),
    ),
    cardTheme: CardThemeData(
      color: tokens.surfaceHigh,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        borderRadius: const BorderRadius.all(NexusRadius.lg),
        side: BorderSide(color: tokens.border, width: 1),
      ),
      elevation: 0,
    ),
    dividerTheme: DividerThemeData(
      color: tokens.borderSubtle,
      thickness: 1,
      space: 1,
    ),
    chipTheme: ChipThemeData(
      backgroundColor: tokens.surfaceHigh,
      side: BorderSide(color: tokens.border),
      labelStyle: TextStyle(color: tokens.textPrimary, fontSize: 12),
      shape: const StadiumBorder(),
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
    ),
    snackBarTheme: SnackBarThemeData(
      backgroundColor: tokens.surfaceHigher,
      contentTextStyle: TextStyle(color: tokens.textPrimary),
      behavior: SnackBarBehavior.floating,
    ),
    floatingActionButtonTheme: const FloatingActionButtonThemeData(
      backgroundColor: NexusColors.crimson,
      foregroundColor: Colors.white,
    ),
    splashColor: NexusColors.crimsonGlow,
    highlightColor: Colors.transparent,
    pageTransitionsTheme: const PageTransitionsTheme(
      builders: {
        TargetPlatform.android: ZoomPageTransitionsBuilder(),
        TargetPlatform.iOS: CupertinoPageTransitionsBuilder(),
        TargetPlatform.macOS: CupertinoPageTransitionsBuilder(),
        TargetPlatform.linux: FadeUpwardsPageTransitionsBuilder(),
        TargetPlatform.windows: FadeUpwardsPageTransitionsBuilder(),
      },
    ),
  );
}
