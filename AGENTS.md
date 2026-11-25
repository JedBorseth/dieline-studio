# Agent instructions

Read [docs/CONTEXT.md](docs/CONTEXT.md) and [docs/PLAN.md](docs/PLAN.md) before starting work.

## Rules

- **Agents never hand-write dieline coordinates.** All geometry comes from the deterministic engine; agent-facing tools take parameters only.
- **Never reverse engineer, patch or bypass ArtiosCAD/CAD-X licensing.** Use only what the current license allows: importing CF2/DXF, interactive Standards, and Outputs/Reports.
- The shipped app calls models only through an **OpenRouter API key** (any model OpenRouter serves), never subscription or OAuth credentials.
- Every new box style needs: allowance rules, geometry and manufacturing checks, and a comparison against an ArtiosCAD reference when one exists.
- Keep machine-specific details (hostnames, absolute paths, SSH settings) and private business notes out of committed files. Put them in `.local/`, which git ignores.
- Large generated output (exports, renders, batches) goes outside the repo. Use a configurable output directory, never the repo tree.
