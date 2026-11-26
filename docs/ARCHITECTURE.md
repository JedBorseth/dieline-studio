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
│    rpc.py ─ designs.py ─ styles/ ─ features.py ─ artwork.py ─ cf2/dxf ─ checks│
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
| `engine.info` | Version, styles, boards, feature defaults, the export capability table, settings |
| `box.create` | Parameters → design (CF2 text, checks, summary), without storing it |
| `design.replace` | The form's Generate and a reopened job: a new design for the session (`designstore.py`), with an id and revision 1 |
| `design.get` | The session's current design, or null |
| `box.export` | Write every part's `.cf2` and/or `.dxf` (`formats`) plus the `.dieline.json` manifest, from the session's design at an exact `design_id` and `revision` (refused if it has changed since), or from `params`. Refuses an existing name with error `-32001` unless `overwrite`; a held folder lock is `-32003`, an abandoned one `-32004` (retry with `break_lock`). The result says whether the sample-table files are `published` or `pending`. `dxf_expand_perfs` writes perforations as dashed cuts (unverified, so blocked for now). |
| `export.plan` | What an export would write (files per part, replaced files, whether the sample table needs a pickup) and whether it can, without writing; `formats` gives each format's own readiness |
| `export.pickup` | The designer confirmed the sample table is idle: move a pending set in (`job_path`, `txn`) |
| `export.recover` | At start: finish or undo exports a crash interrupted |
| `job.open` | Reopen a job record: `mode: "exported"` shows the record's own snapshot (v2), `"regenerate"` rebuilds every part with today's rules and lists what changed; both say whether the files on disk are still that set |
| `settings.get` / `settings.update` | Model, effort, export folder, API key (write-only) |
| `chat.send` / `chat.cancel` / `chat.reset` | Start, stop or clear a chat turn; progress arrives as events |
| `chat.set_design` | Older name for `design.replace` |
| `layout.choose` | The designer clicked a product-mode layout card: commit that candidate (refused once it has expired) |
| `artwork.approve_import` | The designer approved an artwork import on its chat card (`without_unsupported` when the file had content the importer can't read) |
| `artwork.acknowledge` | The designer acknowledged suspicious-artwork flags in the checks panel, on the design id and revision shown; only keys the checks offer |

Notifications use method `event` with a `type`: `engine.ready`, `chat.text`, `chat.thinking`, `chat.tool_start`, `chat.tool_result`, `chat.error`, `chat.turn_end` and `design`. The shell adds `engine.exit`.

## Designs, revisions and parts

The engine holds each chat session's design (`designstore.py`): an id, a revision, its parts (each with a stable id, label, quantity and the standard's parameters), groups and fit links. Every change, from the form, a reopened job or the chat, is built and checked in full outside the lock and committed with a compare-and-swap, so an edit made meanwhile is never overwritten. A chat turn is bound to the design it started from (or last read); a change after an outside edit is refused until the model looks again. A tool call's id makes a retry safe. Exports work from an exact id and revision. Each part also carries a preview scene: its segments tagged with their operation, provenance (structure, feature, library artwork, imported artwork or agent sketch), `feature_id` and `face_id` (a structure hinge's face is the face it hinges), and stale features' ghosts. A part's payload also lists its features as resolved (faces, copies, a plain-words summary, status) and each export target's readiness (`targets`). A reopened job of several parts comes back as one design, with its part ids (`design.replace` with `parts`).

## Partitions

Slotted partition sets (`partitions.py`, drawn by `styles/partitions.py`). Every input mode becomes one grid: the installed clear cells along the long and short axes, the walls between them (each a piece with its board) and the rules. `plan_set` does the arithmetic once (cells + walls = box span, piece length = span − clearance, slot centres from the first wall), sizes each slot from the **mating** piece's caliper, and splits the walls into piece types: one part per distinct plan, with its quantity, in a `partition_set` group. A part's parameters are the set's grid plus its role, so it rebuilds alone. Slots are cut into the piece's one closed outline: lead-in mouths, square or full-radius bottoms (round when at least twice the 1/8" bend radius). Each piece has a face map with one face, `piece`. The rules are industry defaults (`partition-rules-1`), and every partition carries a "not calibrated" warning.

`partsets.py` holds the design changes (add a set, `replace_set` keeping the group id and matching pieces by role, fit links, stacks, removing a set). `layouts.py` keeps product-mode candidates per chat, each tied to the design revision, board values and rule version it was made from. `expressions.py` evaluates typed length expressions (`D - 1/2`, `2 * CAL`) for partition values.

The design's own checks (`fit.py`, run with every payload): each set's crossings (interlock, reciprocity, small cells), each fit link (container inside vs contained outside), stacks (tiers plus pads against the depth), two sets in one box without a stack, and the "not checked" notes.

## Styles, boards and units

The styles replicate R&L's ArtiosCAD ServerLib standards and are named after them: `reg-glio-nx` (RSC family, with flap-style, joint and panel-order options), `reg-gli-ext` (extended glue lug), `hsc-gl` and `tube-glued` (`styles/slotted.py`). Their formulas are the standards' own variable formulas. They compute in **inches**, and inch designs are written to CF2 as `UI`. Boards are ArtiosCAD board codes (`32 B KR`, …) carrying CAL, IL, OG and CRRV (`boards.py`). Dimensions accept fractions (`"14 1/8"`), and the UI shows inches as fractions.

Exports:

- CF2 for ArtiosCAD.
- A `<name>.dieline.json` manifest next to every export (`exports.py`): every part with its quantity, units, files and their hashes, the inputs with defaults resolved, the board values, the design id and revision, and each part's as-exported CF2. One part keeps `<name>.cf2`; several are `<name>-<piece no. or part id>.cf2`. Open… shows a v2 record as exported and offers to regenerate it with current rules; a v1 record can only regenerate.

Publishing (`publish.py`) changes each destination whole or not at all. Every root an export touches is locked: one `.dieline.lock` per root, always in the hidden `.<folder>.dieline-pending/` beside it, so a folder used as the export folder by one designer and the sample table by another still has one lock. The lock names its workstation and process; an abandoned one is broken only for the exact lock the designer confirmed. With the locks held: a path another job's manifest lists (or, at a sample table, another export folder's job claims in the table's ownership index) is refused; an existing file no manifest owns needs the designer's say-so; and an old file whose hash differs from its manifest blocks the replacement. A previous manifest is validated in full before any path in it is used. New files are staged in `.dieline-staging/<txn>/`, a journal records every move, old files move to `superseded/<name> <time>/`, and replacing the manifest (which carries the transaction id) is the commit point. A crash leaves the journal: the next export of that name, or app start, finishes a committed transaction or moves every file back. A journal that doesn't fit what its transaction could have done, or a file that isn't where the journal says, is never guessed at: the journal is kept and that name is blocked until the designer checks it.

The sample-table folder (Settings) is a second transaction after the main one commits. Its files are staged before the main commit, in `.<folder>.dieline-pending/` beside it, with its journal, its archive (`superseded/`) and its ownership index: nothing but finished DXFs ever lands in the folder. One new file moves in at once; several, or a replacement, wait until the designer confirms the table isn't picking up (`export.pickup`, offered in a row of its own under the preview, also for the chat's exports). The manifest records what is physically at the table, so a newer export over a set still waiting replaces the right files.
- DXF R12 in the format the shop sends to the AOLCUT sample table (`dxf.py`): inches, layer `Design`, colour 1 cut, 2 crease. Entities are in cutting order: creases, interior cuts, then the outer perimeter. CF2 writes full circles as two half arcs.

Features (phase 2 of [DESIGN-SYSTEM.md](DESIGN-SYSTEM.md)): a part's features are parameters and placements stored on it (`BoxParams.features`, each with an engine-made id `f1`, `f2`, ...), drawn after the standard:

- `placement.py`: the placement engine. A selector, references on the face (x from a vertical line, y from a horizontal one), `to: near_edge | far_edge | centre` (required), patterns (row, column, grid, spread), and mirroring of opposite faces. Anything with a bounding box around a datum can be placed with it; phase 4's artwork will be.
- `expressions.py`: typed length and scalar expressions for distances and sizes (`W/2 - 1/4`, `2*CAL`, `2:12`, `50mm`), never run through `eval`.
- `shapes.py`: library outlines in lines and true arcs: obround, rounded rectangle, circle, D-shape, a four-centre oval for ellipses, hand-flap profiles (with the secant hinge) and notch profiles.
- `features.py`: handholes, hand flaps, vents, windows, extra scores, notches, perforations and tear strips, built as typed operations that declare their contacts (a hand-flap cut ends on its hinge, a score on the outline, a perforation crosses a crease). Scores, perforations and tear strips follow a straight route across hinged faces, with continuous stationing over jumped slots. A feature that can't be placed is stale: ghosted, not manufactured, export blocked.
Artwork (phase 4 of [DESIGN-SYSTEM.md](DESIGN-SYSTEM.md)): decorative cut-outs and lettering on one face, drawn after the features (`BoxParams.artwork`, ids `a1`, `a2`, ...):

- `curves.py`: Béziers, elliptical arcs and (rational) splines fitted with biarcs within 0.002 in at final size; chords only where arcs can't fit, counted. SVG endpoint arcs to centre form, with the radius correction.
- `glyphs.py`: the app's own skeleton font (A–Z, 0–9, basic punctuation). Stroke text is the skeleton; stencil text thickens it and cuts every junction back to a stencil break, so no counter is ever an island.
- `artwork_sources.py`: the shape library (circle, ellipse, rounded rectangle, polygon, star, heart, smiley, arrow), text, and the agent's sketch on a 0–100 canvas (SVG coordinates, M L Q C A Z); scaling, fitting and closing.
- `artwork_import.py`: SVG (defusedxml) and ASCII DXF, with transforms, `use`, blocks, bulges, splines and ellipses; lists what it can't read; stores the parsed geometry with the file's hash.
- `artwork.py`: placement on one face (placement.py), a rule per outline, and islands across artwork and feature holes with their fixes: bridges (corridors across the flutes first), stencil, partial cut, drop-out.
- `artwork_checks.py`: one aggregated result per tooling check (bend radius from the source curvature, acute corners, short straights, thin webs), edge clearance and keep-outs, islands, and the suspicious-artwork flags the designer acknowledges.
- `artwork_tools.py`: `set_artwork`, imports waiting for the designer, and acknowledgements.

- `rules.py`: a rule spec per operation (cut, crease with its side, perforation with cut, tie, phase and crossing policy, cut-score, pointage) and each target's capability table. Only cut and crease at 2 pt are verified in CF2, and cut and crease in the sample-table DXF; anything else blocks that target, with the reason, while the design stays drawn. The DXF can also write perforations as dashed cuts (a tie centred on each crease), a representation of its own that's unverified and goes through the topology rules.

Faces: every standard carries a face map (`faces.py`), built from the same blank as the drawing: named faces with outside-view frames, assembled and material centres, reference lines, keep-out zones, and the hinge graph (directed axes, IL/OG bend offsets). HSC_GL has a `use` option (tray or lid) that changes which way is up, never the drawing.

Checks:

- topology (`topology.py`): every segment belongs to an operation (outline, hinge, hole…) that declares its deliberate contacts. Undeclared crossings, overlaps, tangencies, open cut ends and broken arcs refuse the design before it exists. Loose board (an island, or a cut dividing the blank) is drawn but blocks export. Near misses warn. Each output file is read back and checked again after rounding, and a target that fails is blocked. Faces are traced exactly through lines and arcs, with no tessellation.
- the standard's own L ≥ W check
- a warning when a value the standard rounds to 1/16 falls between sixteenths
- which R&L machine's sheet limits take the blank: a screen that warns, never a reason to block an export. The slotter can't turn a blank: its height runs with the feed. Skip-feed is reported.
- two checks from the shop's Preflight profile: matching panels within CAL + 1/32, and 2-pt rules only up to 3/16" caliper
- board-value and calibration warnings
- feature checks (`feature_checks.py`), each naming the copy ("vent 3 of 4 on P3") with a region to zoom to: clearance from the face's scores, slots and edges and from other features, handhole carry and side webs, the combined ligament above a handhole, flutes along a handhole's long side or a tear strip, the glue landing zone, overlap bands without a matching cut-out, perforation cuts on creases and short ties on thick board, tear-strip width, tab position and joint. Their limits are provisional and flagged.
- machines report what they can make as well as the sheets they take: a part with a die-cut feature lists only machines that can die-cut (the slotter's die-cutting isn't confirmed, so it isn't listed)
- partition pieces: slot fit, residual web, mouths and rims, height against the box depth, orientation notes, calibration (`partitions.piece_checks`). Refused before a set exists: slots that don't overlap (usable depths, round bottoms counted), a slot that cuts through, a slot narrower than its mating board, slots or mouths that run together, cells of 0 or less, a 1 × n set without `confirm_degenerate`, and 1 × 1.
- the design's own checks (`fit.py`): partition sets, fit links and stacks, shown above the part tabs
- artwork (`artwork_checks.py`): islands (fail until bridged, stencilled, partial-cut or accepted as a drop-out), aggregated tooling checks with a count, the worst value and up to five places, edge clearance and keep-outs, an info note for agent-drawn artwork, and suspicious-artwork flags that block export until the designer acknowledges them. An acknowledgement is for the artwork exactly as drawn; the store refuses any chat change that adds one.

Each check has a category; geometry, feature, fold, partition, artwork and fit are actionable and shown apart from calibration, board and machine notices.

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
| `create_box(style, L, W, D, units, board, options, action?, label?, quantity?, features?, dry_run?)` | Generate, check and show in the preview. `action: add` adds another part instead of replacing the design. The result includes a picture of the part, so every design is looked at. |
| `update_part(part?, ..., features?)` / `remove_part(part)` | Change or remove one part; omitted fields keep their values. A resize re-resolves every feature (one that no longer fits goes stale) and can carry feature changes. A partition piece can only be relabelled; `remove_part` with a set or stack id removes the group (a set takes its pieces). |
| `set_features(part?, items?, remove?, dry_run?)` | Features by batch upsert: an item without an id is new, one with an id changes that feature (fields left out keep their values), `remove` deletes. A feature the call adds or changes that can't be placed is refused. The result has each feature's faces and plain-words summary, and the picture shows the measured offsets. |
| `create_partition(input_mode, ..., action?, set_id?, contained_in?, confirm_degenerate?, label?, dry_run?)` | A slotted partition set from `box_cells`, `cell_sizes` or `pieces`: one part per distinct piece in a `partition_set` group, a fit link when `contained_in` names its box, and a read-back of the layout, cells, pieces, flute and "not calibrated". Actions: `add`, `replace_design`, `replace_set` (keeps the group id). `product` mode returns layout candidates as chat cards and draws nothing. |
| `choose_layout(candidate_id, message_id, ...)` | Commits a product-mode candidate, only after the designer named it in that message (recorded in the set). The designer's card click does the same without the model. |
| `link_fit(container, contained)` / `unlink_fit(link)` | Fit links between parts and groups, checked on every revision |
| `create_stack(tiers, pads?, pad_board?, contained_in?)` | Partition sets in tiers with layer pads, checked against the box's depth |
| `set_artwork(part?, items?, remove?, dry_run?)` | Decorative artwork by batch upsert, like set_features: library shapes, stencil or stroke text, the agent's sketch, or an import. Moving or resizing by id doesn't resend a sketch. An import is read from the reference folders as `read_file` is and waits for the designer's approval on its chat card; the agent can't approve it or acknowledge a flag. |
| `export_files(filename?, formats?, replace?, dxf_expand_perfs?)` | Write every part's CF2 and/or DXF plus the manifest; sample-table files that need a pickup wait for the designer |
| `view_design(region?, path?)` | Picture of the current design (or a reference CF2/DXF): inch ruler, score-to-score distances labelled, faces named from the face map (GL, P4 … P1), feature offsets marked edge or centre, optional zoom window (`render.py`) |
| `list_faces()` | The named faces of the current design (`faces.py`): P1–P4 with end/side roles, flaps, glue lug, lug extensions; each with a frame seen from outside the folded box, reference lines, keep-outs (glue landing, overlap bands), hinge, and the selectors that apply |
| `get_geometry(line_type?, region?, path?)` | Exact segments and score positions, read-only (`render.py`) |
| `search_files(query, extensions?)` | Read-only search of the reference folders (default: the work OneDrive folder synced on the PC). It matches words in paths, and inside ArtiosCAD descriptions for files already on the PC (`files.py`). |
| `read_file(path, page?)` | ARD text facts (description, board, standard, blank size), CF2/DXF picture and scores, PDF/Excel/Word/text by page, images. Confined to the reference folders. |
| `web_search(query)` | Client-side search through the provider chosen in Settings: Exa (via its hosted MCP endpoint) or Tavily, keyless or with the user's key. Modelled on OpenCode (`web.py`). |
| `web_fetch(url, format?)` | Client-side fetch following OpenCode's webfetch rules, plus a block on private and local-network addresses (`web.py`) |

The model never writes structural coordinates. It reads them to check its work; the one exception is an artwork sketch on its own 0–100 canvas, placed by the engine and labelled agent-drawn everywhere (the checks, the preview, the picture and the job record). Feature tools take parameters and placements; the system prompt adds the placement rules (outside view, always edge or centre, ask when the request doesn't say) and a read-back after every feature change. The model can mention a feature as `[handhole f1](feature:p1/f1)`; the chat shows it as a chip that highlights it in the preview. `create_box`, `create_partition` (the set's plan view and each piece) and `view_design` return PNGs, and the chat's tool cards show the same picture, so the designer sees what the model checked. Tool messages are text-only in this API, so the pictures follow in a user message. Tool inputs are validated before anything runs. A cancelled or failed turn is dropped from the conversation, so the history stays append-only.

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

- The CF2 `UM`/`UI` units keyword and the arc direction flag. Cut = 1 and crease = 2 are confirmed by the shop's CF2 import tuning. 3 and 4 are not, so perforations, cut-scores, reverse creases and mixed pointage block CF2 and DXF exports until an import and cut fixture verifies them.
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
- Export locks are whole-folder, not per path: two exports to one folder take turns (each takes a second or so). A lock is judged abandoned on this workstation when its process isn't running; from another workstation only after ten minutes. A reused process id can make an abandoned local lock look live until that process ends.
- Every file move that must not overwrite (archiving, installing, putting a file back) is one atomic no-replace rename: Windows' rename, Linux `renameat2(RENAME_NOREPLACE)` or macOS `renamex_np(RENAME_EXCL)`. A system or file system without one refuses the export rather than moving in two steps.
- The sample table's ownership index is a cross-check kept beside the table; a claim counts only while the claiming manifest still lists the file.
- Stop ends a turn immediately; a worker stuck in a network read is abandoned and ends by itself (read timeout 120 s) without touching the session.
