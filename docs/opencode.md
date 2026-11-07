# OpenCode integration notes

## Agent

`jarvis init` installs `~/.config/opencode/agent/jarvis.md` (or
`./.opencode/agent/jarvis.md` with `--project`). The agent prompt enforces:

- answer in the language of the command;
- at most two short sentences;
- one short sentence before each tool call;
- no code, diffs, URLs, tables or markdown in the spoken reply;
- ask before dangerous actions.

Use it with:

```bash
jarvis ask "..." --agent jarvis
```

The language rule inside the agent is generated from `language = "ru" | "en"` in
the Jarvis config. Changing the language with `jarvis language ru|en` rewrites
the agent file so the model always answers in the selected language.

## Model

The default OpenCode model can be region-restricted. Pin a model explicitly:

```toml
[opencode]
model = "deepseek/deepseek-flash"   # or local-deepseek/deepseek-chat
```

List what is available:

```bash
opencode models
jarvis models
```

`jarvis doctor` warns when `opencode.model` is empty or missing from the list.

## Sessions

- `spawn` (default): a private `opencode serve` child on a free port. Voice
  sessions do not interfere with the TUI.
- `attach`: reuse the background service (`~/.config/opencode/service.json`).

The core creates one persistent session per process. `jarvis` has no reset flag
yet; use `jarvis ask --attach` with a fresh session or restart the process. A
`/api/session/new` endpoint already exists for the GUI.

## Permissions

OpenCode v2 exposes a real permission flow:

- the session event stream emits `permission.asked`;
- the reply endpoint is
  `POST /api/session/{id}/permission/{requestID}/reply`
  with `{"decision": "once" | "always" | "reject"}`.

P3 will wire this to a spoken confirmation. The core already forwards
`permission` events to the GUI and CLI, so the UI can be built in parallel.

## Tools (where to define them)

OpenCode owns the tools. Three extension points:

1. **Built-in** — `bash`, read/write/edit, search, etc.
2. **MCP servers** — register them in `~/.config/opencode/opencode.jsonc`:

   ```jsonc
   {
     "mcp": {
       "jarvis-system": {
         "type": "local",
         "command": ["uv", "run", "jarvis-system-mcp"],
         "enabled": true
       }
     }
   }
   ```

   P3 will ship `jarvis-system-mcp` with `open_app`, `open_game`,
   `media_control`, `set_volume`, `notify`, `lock_screen`.
3. **Plugins / custom tools** — `opencode plugin add`, or project tools under
   `.opencode/tool/*.ts`.

For voice, prefer MCP tools with **short output** ("Открыл Steam") over raw
`bash`, so the model does not narrate shell noise.

## Making the normal OpenCode TUI speak

The core exposes `POST /api/speak`. An OpenCode plugin can subscribe to
assistant-message completion and call it:

```ts
// pseudo-code — adapt to the OpenCode v2 plugin API
await fetch("http://127.0.0.1:47800/api/speak", {
  method: "POST",
  headers: {
    "content-type": "application/json",
    authorization: `Bearer ${process.env.JARVIS_TOKEN}`,
  },
  body: JSON.stringify({ text: assistantText }),
});
```

This is deliberately optional: the voice loop subscribes to the event stream
directly and does not need a plugin.

## OpenCode v2 API facts used by the core

- Auth: HTTP Basic, username `opencode`, password from the server banner or
  `~/.config/opencode/service.json`.
- `GET /api/event` is SSE; pass repeated `types=` query parameters (a single
  comma-separated value is rejected).
- Text streaming: `session.text.started` → `session.text.delta` →
  `session.text.ended`.
- Turn lifecycle: `session.execution.started/succeeded/failed/interrupted`.
- Tools: `session.tool.called/success/failed`.
- Interrupt: `POST /api/session/{id}/interrupt`.
