# GUI contract

Jarvis is deliberately split into a **headless core** and **frontends**. Today
the frontend is the CLI; tomorrow it can be a web app, Tauri, Electron or
PySide6. A GUI must never touch the microphone or the speakers directly — it
talks to the same local API the CLI uses.

```
┌─────────────────────┐        HTTP + SSE        ┌──────────────────┐
│  GUI / TUI / web    │ ◀──────────────────────▶ │  jarvis serve    │
└─────────────────────┘                          └────────┬─────────┘
                                                          │ in-process
                                                 ┌────────▼─────────┐
                                                 │  Runtime / core  │
                                                 │ audio, STT, TTS  │
                                                 │ OpenCode client  │
                                                 └──────────────────┘
```

## Why an API instead of importing Python

- The GUI can be written in any language (TypeScript/Svelte, Rust/Tauri,
  Python/PySide6).
- The audio loop keeps running when the window is closed.
- The same events can drive a web dashboard, a menu-bar app and a plugin.
- The contract is small and versioned independently of the GUI framework.

## Starting the API

```bash
jarvis init        # creates the config and a token in the Jarvis config dir
jarvis serve       # default http://127.0.0.1:47800
```

The bearer token is stored in the Jarvis config directory (`jarvis init` prints
the exact path; on macOS it is `~/Library/Application Support/jarvis/token`,
mode `0600`). Pass it as
`Authorization: Bearer <token>` on every request except `/api/health`.

`EventSource` cannot set headers, so `/api/events` also accepts
`?token=<token>`.

## Endpoints

| Method | Path | Body / query | Result |
|---|---|---|---|
| GET | `/api/health` | — | `{"ok": true, "version": "0.1.0"}` |
| GET | `/api/state` | — | state + sanitized config |
| GET | `/api/events` | `token` (query) | SSE stream |
| GET | `/api/config` | — | full config without secrets |
| GET | `/api/voices` | — | local voice catalogue |
| GET | `/api/models` | — | OpenCode `provider/model` list |
| POST | `/api/ask` | `{"text": "...", "speak": false}` | `{"reply": "..."}` |
| POST | `/api/speak` | `{"text": "...", "language": "ru"}` | `{"ok": true}` |
| POST | `/api/listen` | `{"enabled": true}` | `{"ok": true, "listening": true}` |
| POST | `/api/session/new` | — | `{"session_id": "ses_..."}` |

### State shape

```json
{
  "state": {
    "state": "idle",
    "wake_enabled": true,
    "session_id": "ses_...",
    "last_transcript": "открой калькулятор",
    "last_reply": "Открываю.",
    "error": null,
    "updated_at": 1793976526.38
  },
  "language": "ru",
  "config": {
    "wake": {"engine": "openwakeword", "phrase": "hey jarvis"},
    "stt": {"model": "small", "language": "auto"},
    "tts": {"ru": {"voice": "ru_RU-ruslan-medium"}, "en": {"voice": "bm_george"}},
    "speech": {"max_chars": 400, "max_sentence_chars": 180}
  },
  "session_id": "ses_...",
  "listening": true
}
```

`state.state` is one of:
`offline`, `idle`, `listening`, `transcribing`, `thinking`, `speaking`,
`paused`, `error`.

## SSE events

Every event is `{"type": "...", "ts": 123.45, "data": {...}}`.

| `type` | When | Useful fields |
|---|---|---|
| `hello` | immediately on connect | full snapshot in `data` |
| `state` | any state transition | `state`, `wake_enabled`, `last_reply` |
| `wake` | wake word detected | `phrase`, `score`, `engine` |
| `transcript` | command recognised | `text`, `final` |
| `assistant.delta` | streamed token(s) | `text`, `ordinal` |
| `assistant.text` | full text part finished | `text`, `ordinal` |
| `tool` | OpenCode tool lifecycle | `name`, `status` (`called`/`success`/`failed`) |
| `speech.start` | TTS started | `text`, `truncated` |
| `speech.end` | TTS finished | `text` |
| `permission` | dangerous action needs approval | `status`, `action`, `resources` |
| `error` | any failure | `message` |
| `log` | lifecycle/info | `message` |

### Browser example

```js
const base = "http://127.0.0.1:47800";
const token = localStorage.getItem("jarvis-token");

const state = await fetch(`${base}/api/state`, {
  headers: { Authorization: `Bearer ${token}` },
}).then((r) => r.json());

const source = new EventSource(`${base}/api/events?token=${token}`);
source.onmessage = (message) => {
  const event = JSON.parse(message.data);
  switch (event.type) {
    case "assistant.delta":
      appendToTranscript(event.data.text);
      break;
    case "speech.start":
      showSpeakingIndicator(event.data.text);
      break;
    case "state":
      updateStatusBar(event.data.state);
      break;
  }
};
```

## Recommended GUI stacks

| Stack | Pros | Cons |
|---|---|---|
| **Tauri + Svelte/React** | tiny binary, web UI, cross-platform | Rust toolchain |
| **Electron** | fastest to build, huge ecosystem | large binary |
| **PySide6 / PyQt** | same language as core, native widgets | heavier packaging, licensing |
| **Local web page** | zero install, easy debugging | no tray/menu integration |

Recommendation: start with a **local web page** (served by the same process or a
static file) to validate the UX, then wrap it in Tauri for macOS/Windows.

## GUI roadmap (after P4)

1. **Read-only dashboard** — state, last transcript, assistant deltas, tool list.
2. **Controls** — listen on/off, speak, new session, interrupt.
3. **Settings editor** — `config.toml` fields exposed as forms (never edit audio
   device names by hand).
4. **Permissions** — when `permission` arrives, show Allow once / Always / Reject
   and send the reply through the core.
5. **History** — paginate `GET /api/session/{id}/message` through a new proxy
   endpoint.
6. **Audio panel** — device pickers, input level meter, wake-word test.

## Non-goals

- The GUI must not open the microphone or speakers.
- The GUI must not call OpenCode directly; the core owns sessions and permissions.
- No secrets in the browser: keep the token in a secure local store, never in a
  URL that is logged or shared.
