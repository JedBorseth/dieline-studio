# Dieline Studio

An app that lets AI agents (and people) design packaging, such as corrugated boxes and folding cartons. It creates the flat dieline as **CF2** or **DXF** and checks that it folds correctly before it goes into ArtiosCAD.

**Status:** planning. No code yet.

## The idea in one paragraph

ArtiosCAD's automation API (CAD-X) is installed but locked by our license. Rather than pay to unlock it, we generate the dielines ourselves. A deterministic geometry engine turns a **box style + dimensions + board** into an exact dieline. Agents pick only those inputs. They never draw lines themselves. Automated checks confirm the geometry, manufacturability and folding. The result is exported as CF2 (main format) or DXF and imported into ArtiosCAD, which our current license already supports.

## Docs

- [docs/CONTEXT.md](docs/CONTEXT.md): research findings and the decisions behind them (ArtiosCAD licensing, CAD-X, file formats, constraints)
- [docs/PLAN.md](docs/PLAN.md): architecture and first milestones
- [AGENTS.md](AGENTS.md): working rules for coding agents in this repo
