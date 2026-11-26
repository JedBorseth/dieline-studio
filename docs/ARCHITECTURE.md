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
│    rpc.py ─ designs.py ─ styles/ ─ geometry.py ─ cf2.py/dxf.py ─ checks.py  │
│       └─ agent.py ── HTTPS ──► OpenRouter (user's API key) ──► model provider│
└─────────────────────────────────────────────────────────────────────────────┘
```

- **Tauri shell.** Owns the window and the engine process. It starts the engine at launch, forwards requests from the UI, re-emits engine notifications as `engine-event`, and restarts the engine on the next request if it exits.
- **Engine.** All geometry, checks, file output and the model's tool loop. The same process serves the chat and the plain parameter form, so both draw identical dielines.
- **Browser dev mode.** `pnpm dev` runs the UI in a normal browser. A Vite plugin (`app/vite-engine-bridge.ts`) spawns the engine and exposes the same protocol over HTTP and server-sent events. It's useful on machines without the Tauri toolchain and for automated screenshots.

## Engine protocol

Line-delimited JSON-RPC 2.0. TypeScript types are in `app/src/lib/protocol.ts`, the server is in `engine/src/dieline_engine/rpc.py`.

| Method | Purpose |
|---|---|
| `engine.info` | Version, styles, boards, settings |
| `box.create` | Parameters → design (CF2 text, checks, summary), without storing it |
| `design.replace` | The form's Generate and a reopened job: a new design for the session (`designstore.py`), with an id and revision 1 |
| `design.get` | The session's current design, or null |
| `box.export` | Write `.cf2` and/or `.dxf` (`formats`) plus a `.dieline.json` job record, from the session's design at an exact `design_id` and `revision` (refused if it has changed since), or from `params`. Refuses an existing name with error `-32001` unless `overwrite`. |
| `job.open` | Regenerate a design from a job record, listing what differs from the export |
| `settings.get` / `settings.update` | Model, effort, export folder, API key (write-only) |
| `chat.send` / `chat.cancel` / `chat.reset` | Start, stop or clear a chat turn; progress arrives as events |
| `chat.set_design` | Older name for `design.replace` |

Notifications use method `event` with a `type`: `engine.ready`, `chat.text`, `chat.thinking`, `chat.tool_start`, `chat.tool_result`, `chat.error`, `chat.turn_end` and `design`. The shell adds `engine.exit`.

## Designs, revisions and parts

The engine holds each chat session's design (`designstore.py`): an id, a revision, its parts (each with a stable id, label, quantity and the standard's parameters), groups and fit links. Every change, from the form, a reopened job or the chat, is built and checked in full outside the lock and committed with a compare-and-swap, so an edit made meanwhile is never overwritten. A chat turn is bound to the design it started from (or last read); a change after an outside edit is refused until the model looks again. A tool call's id makes a retry safe. Exports work from an exact id and revision. Each part also carries a preview scene: its segments tagged with their operation, for highlighting later. Exporting a design with several parts arrives with the export manifest.

## Styles, boards and units

The styles replicate R&L's ArtiosCAD ServerLib standards and are named after them: `reg-glio-nx` (RSC family, with flap-style, joint and panel-order options), `reg-gli-ext` (extended glue lug), `hsc-gl` and `tube-glued` (`styles/slotted.py`). Their formulas are the standards' own variable formulas. They compute in **inches**, and inch designs are written to CF2 as `UI`. Boards are ArtiosCAD board codes (`32 B KR`, …) carrying CAL, IL, OG and CRRV (`boards.py`). Dimensions accept fractions (`"14 1/8"`), and the UI shows inches as fractions.

Exports:

- CF2 for ArtiosCAD.
- A `<name>.dieline.json` job record next to every export: parameters, board values snapshot, sizes and checks. The UI's Open… regenerates the design from it and lists anything that changed since, such as corrected board values.

A name is one file set (CF2, DXF, job). An export never mixes sets. If any file of the name exists, it asks. Replace moves the whole old set to `superseded/<name> <time>/` first, and each new file is written to a temporary name and then renamed. DXF can go to a separate sample-table folder (Settings).
- DXF R12 in the format the shop sends to the AOLCUT sample table (`dxf.py`): inches, layer `Design`, colour 1 cut, 2 crease. Entities are in cutting order: creases, interior cuts, then the outer perimeter. CF2 writes full circles as two half arcs.

Faces: every standard carries a face map (`faces.py`), built from the same blank as the drawing: named faces with outside-view frames, assembled and material centres, reference lines, keep-out zones, and the hinge graph (directed axes, IL/OG bend offsets). HSC_GL has a `use` option (tray or lid) that changes which way is up, never the drawing.

Checks:

- topology (`topology.py`): every segment belongs to an operation (outline, hinge, hole…) that declares its deliberate contacts. Undeclared crossings, overlaps, tangencies, open cut ends and broken arcs refuse the design before it exists. Loose board (an island, or a cut dividing the blank) is drawn but blocks export. Near misses warn. Each output file is read back and checked again after rounding, and a target that fails is blocked. Faces are traced exactly through lines and arcs, with no tessellation.
- the standard's own L ≥ W check
- a warning when a value the standard rounds to 1/16 falls between sixteenths
- which R&L machine's sheet limits take the blank: a screen that warns, never a reason to block an export. The slotter can't turn a blank: its height runs with the feed. Skip-feed is reported.
- two checks from the shop's Preflight profile: matching panels within CAL + 1/32, and 2-pt rules only up to 3/16" caliper
- board-value and calibration warnings

## The agent

`agent.py` runs a streaming tool loop on OpenRouter's OpenAI-compatible chat completions API, through the `openai` SDK.
- **Model:** any OpenRouter model with tool calling and image input. The default is `anthropic/claude-opus-5.5`. Settings offers OpenRouter's live model list.
- **Effort:** sent as `reasoning.effort`.
- **Reasoning:** streams to the UI, and `reasoning_details` are replayed unchanged on later requests.
- **Caching:** Claude models get automatic prompt caching (`cache_control`), and a `session_id` keeps routing sticky so caches stay warm.

The model's tools:

| Tool | What it does |
|---|---|
| `list_options` | Styles (with options) and boards |
| `create_box(style, L, W, D, units, board, options, action?, label?, quantity?, dry_run?)` | Generate, check and show in the preview. `action: add` adds another part instead of replacing the design. The result includes a picture of the part, so every design is looked at. |
| `update_part(part?, ...)` / `remove_part(part)` | Change or remove one part; omitted fields keep their values |
| `export_files(filename?, formats?, replace?)` | Write CF2 and/or DXF plus the job record |
| `view_design(region?, path?)` | Picture of the current design (or a reference CF2/DXF): inch ruler, score-to-score distances labelled, optional zoom window (`render.py`) |
| `list_faces()` | The named faces of the current design (`faces.py`): P1–P4 with end/side roles, flaps, glue lug, lug extensions; each with a frame seen from outside the folded box, reference lines, keep-outs (glue landing, overlap bands), hinge, and the selectors that apply |
| `get_geometry(line_type?, region?, path?)` | Exact segments and score positions, read-only (`render.py`) |
| `search_files(query, extensions?)` | Read-only search of the reference folders (default: the work OneDrive folder synced on the PC). It matches words in paths, and inside ArtiosCAD descriptions for files already on the PC (`files.py`). |
| `read_file(path, page?)` | ARD text facts (description, board, standard, blank size), CF2/DXF picture and scores, PDF/Excel/Word/text by page, images. Confined to the reference folders. |
| `web_search(query)` | Client-side search through the provider chosen in Settings: Exa (via its hosted MCP endpoint) or Tavily, keyless or with the user's key. Modelled on OpenCode (`web.py`). |
| `web_fetch(url, format?)` | Client-side fetch following OpenCode's webfetch rules, plus a block on private and local-network addresses (`web.py`) |

The model never writes coordinates. It reads them to check its work. `create_box` and `view_design` return PNGs, and the chat's tool cards show the same picture, so the designer sees what the model checked. Tool messages are text-only in this API, so the pictures follow in a user message. Tool inputs are validated before anything runs. A cancelled or failed turn is dropped from the conversation, so the history stays append-only.

**Reference folders:** by default, only the work OneDrive synced on the PC (`OneDriveCommercial`). Personal OneDrive is never included unless added in Settings.

**File safety:**
- Paths are checked against the folders as written before any disk or network access, and again after links are resolved.
- A file is verified and read through the same open handle.
- Search never follows links or junctions, and never opens cloud-only files. Reading one downloads that file only.
- Parsers have fixed work limits: pages per read, spreadsheet rows and columns, image pixels, CF2 expansion, DXF entities.

**What's sent through OpenRouter to the model's provider:**
- Everything tool results contain: file paths, names, dates and sizes from searches; extracted text; coordinates; pictures.
- These stay in the chat history and are sent again on later turns. Switching models requires a new chat. OpenRouter may route a model through different providers.
- Searching inside ArtiosCAD descriptions happens on the PC, but the matching file paths are sent.

**Web:**
- Searches go from the PC to the chosen search provider (Exa or Tavily), and fetched sites see the request.
- Fetches never go to private, loopback or link-local addresses, including after redirects.
- Reference tool calls disable both web tools for the rest of the chat. Start a new chat for public research.
- The prompt tells the model to keep customer names out of queries, and to treat file and web text as data, not instructions.

The OpenRouter key and any search-provider keys are stored in Windows Credential Manager (via `keyring`) and never written to the settings file. Settings live in `%LOCALAPPDATA%\Dieline Studio\settings.json`; exports default to `Documents\Dieline Studio\exports` (or `DIELINE_OUTPUT_DIR`).

## Not yet confirmed

Still to check against our ArtiosCAD (see PLAN.md):

- The CF2 `UM`/`UI` units keyword and the arc direction flag. Cut = 1 and crease = 2 are confirmed by the shop's CF2 import tuning. 3 and 4 are not.
- Style formulas:
  - They're copied from the standards. Tests check them against real R&L output: 11 REG-GLIO-NX blank sizes, exported creases for two reference blanks, and two REG_GLI_EXT blanks.
  - A few variables round to 1/16 (their rounding mode is stored after each formula: REG-GLIO-NX CD; REG_GLI_EXT CD and flaps; HSC_GL DP and FL). The direction of modes 2 and 3 is unknown, so the engine computes them exactly and warns when one falls between sixteenths.
  - HSC_GL's script ends with a vertical mirror, so ArtiosCAD may draw its flap row at the bottom. No real HSC export has been compared yet.
  - HSC and tube have no real-blank comparison yet.
  - Every design carries a calibration warning until an ArtiosCAD rebuild comparison (milestone 4).
- Board values:
  - Only 32 B KR is read from ArtiosCAD.
  - 32 C KR, 48 BC KR and 71 BC KR are solved from real designs.
  - The rest are nominal (`boards.py` records the source).
- Machine sizes:
  - The slotter is from its nameplate. The die presses use the sheet limits in their ArtiosCAD definitions.
  - Flute direction, trims and multi-up layouts are not modelled.

## Known gaps

- Before distributing installers, generate the full license notices for bundled dependencies (npm, Python packages inside the sidecar, Rust crates) and ship them with `THIRD_PARTY_NOTICES.md`, which currently covers the T3 Code primitives and direct UI dependencies.
- Opening a raw CF2 shows it but doesn't change the chat's current design (a CF2 has no parameters), so "export this" in the chat still means the chat's last design.
- A design made in the form while a chat turn is running isn't handed to the chat (the UI warns).
- Exports are serialised within one app instance only. Two designers replacing the same name in a shared server folder at the same moment aren't coordinated, and a share dropping mid-replace can leave the set partly moved. The superseded copies stay, and nothing is deleted.
- Stop ends a turn immediately; a worker stuck in a network read is abandoned and ends by itself (read timeout 120 s) without touching the session.
