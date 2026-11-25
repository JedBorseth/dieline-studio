# Dieline Studio

An app that lets AI agents (and people) design packaging, such as corrugated boxes and folding cartons. It creates the flat dieline as **CF2** or **DXF** and checks that it folds correctly before it goes into ArtiosCAD.

**Status:** early scaffold. The Windows desktop app runs: chat on the left, a live CF2 preview on the right, one box style (RSC / FEFCO 0201) with placeholder allowances that aren't calibrated against ArtiosCAD yet.

## The idea in one paragraph

ArtiosCAD's automation API (CAD-X) is installed but locked by our license. Rather than pay to unlock it, we generate the dielines ourselves. A deterministic geometry engine turns a **box style + dimensions + board** into an exact dieline. Agents pick only those inputs. They never draw lines themselves. Automated checks confirm the geometry, manufacturability and folding. The result is exported as CF2 (main format) or DXF and imported into ArtiosCAD, which our current license already supports.

## Layout

| Path | What |
|---|---|
| `app/` | Desktop app: React UI (`src/`), Tauri 2 shell (`src-tauri/`) |
| `engine/` | Python engine: styles, geometry, CF2, checks, Claude tool loop, stdio JSON-RPC server |
| `docs/` | Context, plan, architecture |

## Developing on Windows 11

Prerequisites: [Rust](https://rustup.rs) (MSVC toolchain), Node 24 with pnpm, [uv](https://docs.astral.sh/uv/), and the Visual Studio C++ build tools. WebView2 ships with Windows 11.

```powershell
cd app
pnpm install
pnpm tauri dev        # desktop app; starts the engine with `uv run` from ../engine
```

Other commands:

```powershell
pnpm dev              # UI only, in a browser at http://localhost:1420 (the engine runs behind Vite)
pnpm typecheck; pnpm test
cd ..\engine; uv run pytest
```

Chat needs an Anthropic API key, entered in **Settings** and stored in Windows Credential Manager. The **Parameters** form in the preview pane drives the same engine without a key.

Environment overrides: `DIELINE_ENGINE_CMD` (JSON array, the command that starts the engine) and `DIELINE_OUTPUT_DIR` (export folder).

## Building the installer

```powershell
powershell -ExecutionPolicy Bypass -File engine\scripts\build-sidecar.ps1   # engine → single .exe sidecar
cd app
pnpm tauri build --config src-tauri/tauri.release.conf.json                 # NSIS + MSI installers
```

## Docs

- [docs/CONTEXT.md](docs/CONTEXT.md): research findings and the decisions behind them (ArtiosCAD licensing, CAD-X, file formats, constraints)
- [docs/PLAN.md](docs/PLAN.md): architecture and first milestones
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how the app, shell and engine fit together
- [AGENTS.md](AGENTS.md): working rules for coding agents in this repo
