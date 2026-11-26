# Agent instructions

Read [docs/CONTEXT.md](docs/CONTEXT.md) and [docs/PLAN.md](docs/PLAN.md) before starting work.

## Rules

- **Agents never hand-write structural coordinates.** Panels, scores, slots, flaps and allowances come only from the deterministic engine; agent-facing tools take parameters and placements. The one exception is cut-out artwork (a smiley, a logo, lettering): an agent may sketch its outline in the artwork's own local frame, which the engine places on a panel, checks and labels "agent-drawn". See [docs/DESIGN-SYSTEM.md](docs/DESIGN-SYSTEM.md).
- **Never reverse engineer, patch or bypass ArtiosCAD/CAD-X licensing.** Use only what the current license allows: importing CF2/DXF, interactive Standards, and Outputs/Reports.
- The shipped app calls models only through an **OpenRouter API key** (any model OpenRouter serves), never subscription or OAuth credentials.
- Every new box style needs: allowance rules, geometry and manufacturing checks, and a comparison against an ArtiosCAD reference when one exists.
- Keep machine-specific details (hostnames, absolute paths, SSH settings) and private business notes out of committed files. Put them in `.local/`, which git ignores.
- Large generated output (exports, renders, batches) goes outside the repo. Use a configurable output directory, never the repo tree.
- **Reviews always use two reviewers: Sol 6.2 (Codex `gpt-6.2-sol`) and Gemini Neon (Antigravity `gemini-4-neon`)** (delegated, one review each), even when the user doesn't ask for them by name. Pass both this rule and the review brief to any agent that plans or runs work.
