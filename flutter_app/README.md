<div align="center">

<img src="assets/logo.svg" alt="NexusBot" width="120" />

# NexusBot · Flutter Client

**Premium AI chat client for the NexusBot backend.**
Mobile-first · adaptive desktop · light + dark · streaming-first.

</div>

---

## ✨ What's in the box

### Authentication
- **JWT** access + refresh, persisted in `flutter_secure_storage`
- **Auto-refresh on 401** via Dio interceptor — silently retries the original request
- Splash → login → register → app flow with auth-aware `go_router` redirect
- Logout from the sidebar profile menu

### Chat
- **Token-by-token streaming** over **SSE** *or* **WebSocket** (toggle in Settings)
- **Stop button** on the composer cancels in-flight streams
- **Per-message actions** — copy, regenerate (last assistant), timestamps
- **Markdown rendering** with custom code-block widget: language pill, copy button, monospace (JetBrains Mono)
- **Citations strip** — horizontal cards under each assistant reply, with document name and score
- **Tool-call chips** above the bubble when the agent invokes a tool
- **RAG / Tools toggles** inline in the composer
- Suggestion chips on empty chat
- Auto-scroll, animated typing dots

### Sessions
- **Persistent sidebar** on tablet/desktop · drawer on mobile
- **Search** chats by title
- **Rename** and **delete** from per-row menu
- New-chat button minted as a primary gradient CTA
- Skeleton shimmer while sessions load

### Documents (RAG)
- Multipart upload via `file_picker` (PDF · DOCX · MD · TXT · HTML · CSV · JSON)
- **Real upload progress bar** wired to Dio's `onSendProgress`
- Status pills (`PENDING` · `INDEXING` · `READY` · `FAILED`)
- File-type icons, delete confirmation

### Settings
- **Theme picker** — System · Light · Dark (persisted)
- **Stream transport** — SSE or WebSocket
- **Streaming toggle** — on/off (off falls back to blocking POST)
- **RAG by default** toggle
- **Model picker** — `gpt-4o-mini` / `gpt-4o` / `gpt-4-turbo`
- **Clear all chats**, sign out

### Design system
- **Light + dark** themes, both branded crimson
- Theme tokens via `ThemeExtension` — `context.tokens.surface`, `.textPrimary`, etc.
- 4-pt spacing scale, semantic radius scale
- Inter (UI) + JetBrains Mono (code) via `google_fonts`
- Smooth `AnimatedSwitcher` page transitions inside the shell

### Responsive
| Width        | Layout |
|--------------|--------|
| < 720 px     | Hamburger drawer · full-screen content |
| 720–1100 px  | Persistent 260 px sidebar + content |
| > 1100 px    | Persistent 280 px sidebar + content |

---

## 🚀 Quickstart

```bash
# 1. Start the backend
cd .. && docker compose up

# 2. Fetch deps
cd flutter_app
flutter pub get

# 3. (one-time) Generate platform folders if missing
flutter create --org com.nexusbot --project-name nexusbot .

# 4. Run
flutter run
```

### Pointing at a non-default backend

```bash
# Android emulator
flutter run --dart-define=API_BASE_URL=http://10.0.2.2:8000/api/v1

# Device on the same Wi-Fi
flutter run --dart-define=API_BASE_URL=http://192.168.1.42:8000/api/v1

# Production
flutter build apk \
  --dart-define=API_BASE_URL=https://api.example.com/api/v1
```

---

## 🏗 Architecture

```
lib/
├── main.dart                          # entry + status-bar chrome
├── app.dart                           # MaterialApp.router · light + dark theme · ThemeMode from prefs
│
├── core/
│   ├── env.dart                       # build-time config
│   ├── preferences.dart               # ThemeMode · transport · model · toggles → SharedPreferences
│   ├── theme.dart                     # NexusColors · NexusTokens (ThemeExtension) · buildNexusTheme()
│   ├── breakpoints.dart               # context.layoutSize / .isMobile / .isWide
│   ├── widgets.dart                   # NexusLogo · NexusWordmark · NexusButton · Skeleton · EmptyState · EmberBackground
│   ├── secure_storage.dart            # JWT vault
│   ├── api_client.dart                # Dio + 401-refresh interceptor + ApiException
│   └── router.dart                    # go_router with auth-aware redirect
│
├── features/
│   ├── auth/
│   │   ├── auth_models.dart           # User · TokenPair
│   │   ├── auth_repository.dart       # /auth/login · /register · /refresh · /me
│   │   ├── auth_providers.dart        # currentUserProvider
│   │   ├── splash_screen.dart
│   │   ├── login_screen.dart
│   │   └── register_screen.dart
│   ├── chat/
│   │   ├── chat_models.dart           # ChatSession · ChatMessage · Citation · sealed StreamEvent
│   │   ├── chat_repository.dart       # REST · SSE · transport-agnostic streamMessage()
│   │   ├── chat_ws.dart               # WebSocket transport
│   │   ├── chat_controller.dart       # StateNotifier — folds stream events into UI state
│   │   ├── chat_widgets.dart          # MessageBubble · CitationsStrip · Composer · TypingDots
│   │   ├── code_block.dart            # Fenced-code widget with copy + language pill
│   │   └── chat_screen.dart
│   ├── documents/
│   │   ├── document_model.dart
│   │   ├── documents_repository.dart  # upload with onSendProgress
│   │   └── documents_screen.dart
│   └── settings/
│       └── settings_screen.dart       # theme · transport · model · streaming · RAG · clear data
│
└── shared/
    ├── sidebar.dart                   # persistent left rail · search · rename · delete · profile menu
    └── home_shell.dart                # adaptive (drawer ↔ sidebar) · owns selected session
```

**State**: Riverpod 2 (no codegen).
**Routing**: go_router 14 (auth-redirect + single shell route).
**Networking**: Dio 5 + `flutter_client_sse` + `web_socket_channel`.
**Persistence**: `flutter_secure_storage` (tokens) + `shared_preferences` (prefs).

---

## 🔌 Streaming wire format

Both transports emit the same JSON event shape. The chat controller folds each into UI state:

| `type`              | UI effect |
|---------------------|-----------|
| `user_message`      | Replace the optimistic user bubble with the canonical one |
| `citations`         | Render `CitationsStrip` under the assistant bubble |
| `token`             | Append to the live assistant bubble |
| `tool_call`         | Render a tool-call chip above the bubble |
| `usage`             | (telemetry — currently unused in UI) |
| `done`              | Stop the typing indicator |
| `assistant_message` | Replace the placeholder with the canonical persisted message |
| `error`             | Inline error banner above the composer |

See [`chat_controller.dart`](lib/features/chat/chat_controller.dart) for the full state machine.

---

## 🎨 Design tokens

Pulled from `context.tokens` (a `ThemeExtension<NexusTokens>`):

| Token            | Dark        | Light       |
|------------------|-------------|-------------|
| `background`     | `#050505`   | `#FAFAFA`   |
| `surface`        | `#0E0E10`   | `#FFFFFF`   |
| `surfaceHigh`    | `#18181B`   | `#F4F4F5`   |
| `surfaceHigher`  | `#27272A`   | `#E4E4E7`   |
| `border`         | `#27272A`   | `#E4E4E7`   |
| `textPrimary`    | `#F8FAFC`   | `#09090B`   |
| `textSecondary`  | `#A1A1AA`   | `#52525B`   |
| `textMuted`      | `#71717A`   | `#71717A`   |
| `crimson` (fixed)| `#EF4444`   | `#EF4444`   |

Fonts: Inter (UI), JetBrains Mono (code).
Radii: 8 / 12 / 16 / 20 / pill.
Spacing: 4-pt grid (xs … xxxl).

---

## 🧪 What this build deliberately skips

- Voice input
- Push notifications
- Offline cache / SQLite mirror
- In-app organization switcher (single-user accounts only)
- Per-session model override (uses the account default from Settings)
- Real syntax highlighting inside code blocks — the chrome (copy button, language pill) is built, swapping in `re_highlight` is a one-file change in [`code_block.dart`](lib/features/chat/code_block.dart)

---

## License

MIT — same as the parent NexusBot project.
