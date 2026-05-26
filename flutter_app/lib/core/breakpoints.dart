import 'package:flutter/material.dart';

/// Layout breakpoints used throughout the app.
///
/// Mobile = drawer navigation. Tablet/Desktop = persistent sidebar.
class Breakpoints {
  const Breakpoints._();
  static const double mobile = 720;
  static const double desktop = 1100;
}

enum LayoutSize { mobile, tablet, desktop }

extension LayoutSizeX on BuildContext {
  LayoutSize get layoutSize {
    final w = MediaQuery.sizeOf(this).width;
    if (w < Breakpoints.mobile) return LayoutSize.mobile;
    if (w < Breakpoints.desktop) return LayoutSize.tablet;
    return LayoutSize.desktop;
  }

  bool get isMobile => layoutSize == LayoutSize.mobile;
  bool get isTablet => layoutSize == LayoutSize.tablet;
  bool get isDesktop => layoutSize == LayoutSize.desktop;
  bool get isWide => layoutSize != LayoutSize.mobile;
}
