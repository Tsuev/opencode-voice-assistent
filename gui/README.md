# GUI (future)

This folder is a placeholder. The GUI is intentionally not implemented yet —
the headless core and its HTTP contract come first.

Read [`../docs/gui.md`](../docs/gui.md) before starting. The short version:

- `jarvis serve` exposes `GET /api/state`, `GET /api/events` (SSE),
  `POST /api/speak`, `POST /api/listen`, `POST /api/ask`.
- The GUI only renders state and sends commands; it never touches audio or
  OpenCode directly.
- Recommended first step: a local web dashboard, then wrap it in Tauri for
  macOS and Windows.
