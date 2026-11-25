# Shop acceptance tests

Tests that need the ArtiosCAD workstation, the sample table or a designer. They settle what the engine can't learn from files. Run them on copies; save nothing over shop designs. Record results (values and photos) in `.local/` and update the engine and tests to match.

## 1. Rounding (one ArtiosCAD rebuild each)

Build each standard from ServerLib in ArtiosCAD. Then, from Design > Rebuild Design, read the listed variables with full precision.

| Standard | Inputs | Read | Why |
|---|---|---|---|
| REG-GLIO-NX | 14 × 8 × 6 1/32, 32 B KR, regular flaps | TD, BD, CD | CD raw is 6 11/32. Rounding mode 2 sends it to 6 5/16 or 6 3/8. |
| REG_GLI_EXT | 14 × 9 × 6, 32 B KR, top Gap/Lap with lap 2 15/16, then 3, then 3 1/16 | TF1, TD1, CD | Mode 3 flap rounding against the strict `TF1 > 2/3 W` depth branch |
| HSC_GL | 14 × 8 1/16 × 6, 32 B KR | DP, FL; which edge the flaps are on | Modes 2 and 3, and the standard's final vertical mirror |

Then generate the same inputs in Dieline Studio and compare. Its rounding check names the variables that should differ.

## 2. CF2 import (milestone 3)

1. Export a 14 × 8 × 6, 32 B KR `reg-glio-nx` as CF2.
2. In ArtiosCAD, File > Open the CF2 with the shop's default CFF2 import tuning.
3. Check the following:
   - **Scale:** the blank is 45 5/8 × 14 5/16 in.
   - **Line types:** cuts are cut, creases are crease, and every rule is 2 pt.
   - **Outline:** it's closed. Design > Design Checks and Preflight show nothing new.
   - **3D:** the blank folds in 3D (convert to 3D, fold the creases), panels close, and the inside is 14 × 8 × 6.
4. Ask the designer to export the same design from ArtiosCAD with **CFF2 - Save**. Keep it as the reference CF2: it confirms the `UI` keyword, the header and how arcs are written.
5. Arcs: export any arc-bearing design as CFF2 and DXF. This confirms the arc direction flag before any style with curves.

## 3. Sample table (AOLCUT)

1. Export the same design as DXF.
2. Load it on the AOLCUT with the usual profile and cut it in 32 B KR.
3. Check:
   - inches are read as inches (blank 45 5/8 × 14 5/16)
   - colour 1 is assigned to the knife and colour 2 to the creasing wheel
   - the sample folds square

## 4. Board values

The engine has CAL and IL from ArtiosCAD for 32 B KR only. Copy the records for every board code in `engine/src/dieline_engine/boards.py` from DataCenter Admin's board list (CAL, IL, OG, CRRV, with units), most used first:

32 C KR, 48 BC KR, 51 BC KR, 44 C KR, 23 B KR, 61 BC KR, 40 C KR, 71 BC KR, 32 E KR, 42 BC KR.

## 5. Designer decisions

- **Pointage for double wall:** the standards draw 2-pt rules. The shop's Preflight allows 2 pt only up to 3/16" caliper. Should BC exports use 3 pt?
- **Asymmetric RSCs:** with different top and bottom flap styles, REG-GLIO-NX draws panel 4's lower glue-side flap wall from the top flap depth, which gives a slanted edge. Is that intended, or a standard bug? The engine draws it square.
- **Machines:** does the slotter feed with the blank height along the feed? Which presses are actually used for which jobs?
