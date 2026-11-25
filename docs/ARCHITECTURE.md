# Architecture

How the app is put together. For the reasons behind it see [CONTEXT.md](CONTEXT.md); for the roadmap see [PLAN.md](PLAN.md).

## Processes

```
┌──────────────────────── Dieline Studio (Windows 11) ────────────────────────┐
│                                                                             │
│  WebView2: React UI (app/src)                                               │
│  ┌──────────┬───────────────────────────┬─────────────────────────────────┐ │
│  │ Sidebar  │ Chat: messages, tool      │ Preview: CF2 → SVG, layers,     │ │
│  │ designs  │ cards, composer           │ checks, form, open/export CF2   │ │
│  └──────────┴─────────────┬─────────────┴──────────────┬──────────────────┘ │
│                           │ invoke("engine_request")   │ listen("engine-event")
│  Tauri shell (app/src-tauri/src/engine.rs)                                  │
│                           │ JSON-RPC lines on stdin/stdout                  │
│  Python sidecar: dieline-engine (engine/)                                   │
│    rpc.py ─ designs.py ─ styles/ ─ geometry.py ─ cf2.py ─ checks.py         │
│       └─ agent.py ── HTTPS ──► Anthropic API (user's API key)               │
└─────────────────────────────────────────────────────────────────────────────┘
```

- **Tauri shell.** Owns the window and the engine process. It starts the engine at launch, forwards requests from the UI, re-emits engine notifications as `engine-event`, and restarts the engine on the next request if it exits.
- **Engine.** All geometry, checks, file output and the Claude loop. The same process serves the chat and the plain parameter form, so both draw identical dielines.
- **Browser dev mode.** `pnpm dev` runs the UI in a normal browser. A Vite plugin (`app/vite-engine-bridge.ts`) spawns the engine and exposes the same protocol over HTTP and server-sent events. It's useful on machines without the Tauri toolchain and for automated screenshots.

## Engine protocol

Line-delimited JSON-RPC 2.0. TypeScript types are in `app/src/lib/protocol.ts`, the server is in `engine/src/dieline_engine/rpc.py`.

| Method | Purpose |
|---|---|
| `engine.info` | Version, styles, boards, settings |
| `box.create` | Parameters → design (CF2 text, checks, summary) |
| `box.export` | Regenerate from parameters and write a `.cf2` to the export folder |
| `settings.get` / `settings.update` | Model, effort, export folder, API key (write-only) |
| `chat.send` / `chat.cancel` / `chat.reset` | Start, stop or clear a chat turn; progress arrives as events |

Notifications use method `event` with a `type`: `engine.ready`, `chat.text`, `chat.thinking`, `chat.tool_start`, `chat.tool_result`, `chat.error`, `chat.turn_end` and `design`. The shell adds `engine.exit`.

## The agent

`agent.py` runs a streaming tool loop on the Messages API (default model Claude Opus 5.5, with model and effort set in Settings). Claude has three tools:

- `list_options`: styles and boards.
- `create_box(style, length, width, depth, units, board)`: generate, check and show in the preview.
- `export_cf2(filename?)`: write the current design to the export folder.

Claude never sees or writes coordinates. `create_box` returns the blank size, panel sizes and check results only. Tool inputs are validated with pydantic before anything runs. A cancelled or failed turn is dropped from the conversation, so the history stays append-only.

The API key is stored in Windows Credential Manager (via `keyring`) and never written to the settings file. Settings live in `%LOCALAPPDATA%\Dieline Studio\settings.json`; exports default to `Documents\Dieline Studio\exports` (or `DIELINE_OUTPUT_DIR`).

## Not yet confirmed

These are placeholders until checked against our ArtiosCAD (see PLAN.md):

- CF2 line-type codes (1 cut, 2 crease, 3 perforation, 4 score), the `UM`/`UI` units keywords and the arc direction flag (`cf2.py`, `app/src/lib/cf2.ts`).
- RSC allowances (`styles/rsc.py`) and board calipers (`boards.py`). Every RSC design carries a "not calibrated" warning until milestone 4.
- Sheet-size limit (`checks.py`).

## Known gaps

- Before distributing installers, generate the full license notices for bundled dependencies (npm, Python packages inside the sidecar, Rust crates) and ship them with `THIRD_PARTY_NOTICES.md`, which currently covers the T3 Code primitives and direct UI dependencies.
- Arcs aren't yet included in the line-crossing check.
- Stop ends a turn immediately; a worker stuck in a network read is abandoned and ends by itself (read timeout 120 s) without touching the session.
