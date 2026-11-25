# Dieline Studio

An app that lets AI agents (and people) design packaging, such as corrugated boxes and folding cartons. It creates the flat dieline as **CF2** or **DXF** for use in ArtiosCAD and on a sample table.

The Windows desktop app supports glued slotted boxes, extended glue lugs, half-slotted containers and sleeves. It checks geometry and manufacturing constraints. ArtiosCAD import, fold and sample-table acceptance checks are tracked in [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md).

The geometry engine generates dielines from a style, dimensions and board. The chat assistant can inspect previews, measure the resulting geometry, and search read-only reference folders, including the work OneDrive synced to the PC. Public web research uses Exa or Tavily independently of the OpenRouter chat model.

## Layout

| Path | What |
|---|---|
| `app/` | Desktop app: React UI (`src/`), Tauri 2 shell (`src-tauri/`) |
| `engine/` | Python engine: styles, geometry, CF2, checks, model tool loop (OpenRouter), stdio JSON-RPC server |
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

Chat needs an OpenRouter API key, entered in **Settings** and stored in Windows Credential Manager. The **Parameters** form in the preview pane drives the same engine without a key.

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
