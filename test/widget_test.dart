import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:nexusbot/app.dart';

void main() {
  testWidgets('App boots without throwing', (WidgetTester tester) async {
    await tester.pumpWidget(const ProviderScope(child: NexusBotApp()));
    // Initial frame should render — we don't pump-and-settle because the
    // splash screen kicks off network calls that would never complete in test.
    expect(find.byType(MaterialApp), findsOneWidget);
  });
}
