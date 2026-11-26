# Plan

## Principle

**Agents choose parameters; a deterministic engine draws geometry.** An agent never writes coordinates. It calls tools like `create_box(style, dims, board)`, and the engine produces the dieline. Checks verify every result.

## Architecture

1. **Style library:** box styles written as code (start with the RSC/FEFCO 0201 shipping box, then a tuck-end carton and a mailer), each with board-allowance rules.
2. **Board table:** caliper and flute for each board grade.
3. **Geometry model:** an internal representation of lines and arcs with line types. CF2 and DXF are just ways of writing it out.
4. **Checks**, easiest to hardest:
   - Geometry: closed outline, no duplicate or overlapping lines, valid line types.
   - Manufacturing: glue flap width, minimum panel sizes, flute direction, blank fits the sheet or press.
   - Fold simulation: fold along the creases in 3D; panels close, flaps don't collide, inside dimensions within tolerance.
   - Comparison with ArtiosCAD: the same style built in ArtiosCAD and exported, scorelines compared.
5. **Exporters:** CF2 (main), DXF (ezdxf, one layer per line type), a preview image.
6. **Agent interface:** an MCP server or tool API (`create_box`, `validate`, `preview`, `export_cf2`). An optional plain form drives the same engine without AI.

## Milestones

1. **RSC generator plus CF2 writer.** One style, with board allowances.
2. **Geometry and manufacturing checks.**
3. **Import test on the ArtiosCAD workstation.** Check that line types come through and 3D folding works. This is the cheap test of the whole approach.
4. **Calibrate against ArtiosCAD's RSC** using exported scoreline data.
5. Then add the agent tools, fold simulation, more styles, DXF export and previews.

**Progress (2026-11-25):** the Windows desktop app is scaffolded (see [ARCHITECTURE.md](ARCHITECTURE.md)). Milestone 1 is in place with placeholder allowances; milestone 2 has the geometry checks and first manufacturing checks. The agent tools and CF2 preview from milestone 5 came early so the whole pipeline can be exercised. Milestones 3 and 4 (ArtiosCAD import test and calibration) are next.

**Progress (2026-11-25, later):** the engine now replicates R&L's own ServerLib standards (REG-GLIO-NX, HSC_GL, Tube_Glued) in inches with their board codes. It also writes the sample table's DXF format. The formulas reproduce two real R&L blanks to the 1/10,000". Research and evidence: `.local/ARTIOS-WORKFLOW.md` (private). Next: the import test (milestone 3), confirming board values from the board library, then 5PW, die-cut trays and the wing-lock mailers.

**Next (2026-11-25):** custom designs: features (handholes, hand flaps, vents, perfs, tear strips, scores, notches), slotted partitions, a panel builder for one-offs, and cut-out artwork. Spec and phases: [DESIGN-SYSTEM.md](DESIGN-SYSTEM.md).

## Open questions

- ~~Which box styles does the company use most?~~ From this year's designs: REG-GLIO-NX (RSC) by far, then REG_GLI_EXT, HSC_GL, MEATBOX wing-lock, Tube_Glued, die-cut trays, 5PW.
- Confirm the CF2 units keyword and arc direction on import. (Cut = 1 and crease = 2 are confirmed by the shop's import tuning.)
- Copy the real board values (CAL, IL, OG, CRRV) for each board code from ArtiosCAD's board library.
- Find or rebuild the "export to YAML" ArtiosCAD Output, and decide on its fields.
- The die maker's and sample cutter's accepted formats (CF2 directly?).
- Ownership and licensing of the code (see the local notes).
