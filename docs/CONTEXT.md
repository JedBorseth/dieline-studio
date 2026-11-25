# Context and findings

Research summary, collected 2026-11-24 to 2026-11-25. Prices and terms change, so re-check before relying on them.

## ArtiosCAD setup

- The ArtiosCAD workstation runs **ArtiosCAD 26.03** (build 4021), licensed through Esko's cloud subscription service.
- The license is **ArtiosCAD Advanced, Dynamic**, a seat shared across a group of users.
- Estimated price from Esko's public store, yearly plan (the page prints prices without decimal points, so these are interpreted):

  | Edition | Named user / month | Dynamic / month |
  |---|---|---|
  | Connect | ~$34 | ~$45 |
  | Essentials | ~$291 | ~$378 |
  | Advanced | ~$601 | **~$781 (current)** |
  | Prime | ~$813 | ~$1,058 |

## ArtiosCAD automation API (CAD-X)

- ArtiosCAD's automation API is **CAD-X**, a COM/ActiveX control (`cad-x.ocx` in the ArtiosCAD `Program` folder, ProgID `ArtiosCAD-X.CadXCtrl.1`). It has ~130 methods: `OpenDesign`, `RunStandard`, `SetNumericVariable`, `OutputDesign`, `SaveAsESVG`, thumbnails, layers, board and more.
- Tested from PowerShell: version calls work (`ReturnCadVersion` → `26.03`). Every call that touches a design returns code `2` with the error **"Not licensed to use this method"**.
- So the software is installed; only the license entitlement is missing.
- Esko publishes **no price** for CAD-X. It's quote-only, either as an add-on or possibly bundled with Prime. If it comes with Prime, the upgrade is ~$277/month (~$3,300/year) per seat over Advanced Dynamic, but that's unconfirmed.
- Questions for Esko if we ever want it: add-on vs. Prime, a server license or extra seat for unattended runs (a dynamic seat could be taken by a script), a trial license, and whether it can be added to the current subscription.
- **Do not reverse engineer or bypass the CAD-X license check.** That's DMCA anti-circumvention plus Esko's EULA. The valuable logic lives in the ArtiosCAD engine, not the OCX, so a bypass wouldn't get us anything worth having.

## Why CF2/DXF instead

- Our current license already lets ArtiosCAD import DXF and CF2.
- Imported designs stay **vector and fully editable**: lines can be moved, retyped (cut, crease and so on), dimensioned and folded in 3D. It is not like importing a raster image.
- What we lose: an imported design is **not parametric**. It won't resize itself or recalculate board allowances. To change size, regenerate it from the app and import it again.
- **CF2 over DXF:** CF2 (Common Format 2) is the packaging industry's plain-text dieline format, and it stores line types directly. DXF only stores geometry, so line types have to be passed through layer or colour conventions plus ArtiosCAD import mapping.
- **CF2 line-type codes:** Kodak's documentation lists 1 = cut, 2 = crease, 3 = perforation, 4 = score, and warns the numbers can vary. **Confirm against a CF2 exported from our ArtiosCAD before relying on them.**

## Board thickness and depth adjustments

- Changing depth means moving scorelines. Each fold needs an allowance based on board thickness (caliper) and flute direction, so the box hits its target inside dimensions.
- Two ways to handle it:
  - **Route A:** keep a native ArtiosCAD Standard (parametric template), type the new size, and ArtiosCAD does the maths exactly. It's interactive, needs no CAD-X, and has zero formula risk.
  - **Route B:** our generator encodes the allowance rules for each style and board. It's fully automatic, but we own the correctness.
- **Plan:** use Route A to calibrate Route B. Build the same box in ArtiosCAD at several depths and boards, export the scoreline data, derive the exact allowances, and make our generator match them.

## Exporting from ArtiosCAD without CAD-X

- An ArtiosCAD **Output** (specifically a custom **Report**) can write design data to a text file (YAML, JSON or CSV) from a menu or toolbar button. It runs as normal licensed ArtiosCAD, so CAD-X isn't needed. Outputs live in Shared/User Defaults, not as loose files.
- An earlier agent may have made one of these. It hasn't been located or confirmed yet.
- Limits: read-only, triggered by hand (batch over selected designs is possible), and hand-built YAML is fragile. Full line geometry is better exported as CF2. Units and locale formatting leak into the values, and the Output may show a filename prompt.
- Use: the read side of a round-trip, and calibration data for the generator.

## Libraries and licenses

| Library | Use | License |
|---|---|---|
| ezdxf | DXF read/write | MIT |
| shapely | 2D geometry checks | BSD-3 (verify) |
| numpy | fold maths | BSD-3 (verify) |
| (none) | CF2: no established free library; we write our own (~100–200 lines) | n/a |

- Aspose.CAD (commercial) can convert CF2 but isn't needed.
- T3 Code and OpenCode are MIT too. Code from them may be modified and sold. Keep their copyright and license notices, don't use their names or branding, and check the licenses of their dependencies.

## AI usage and cost

- The shipped app **must use an Anthropic API key**. Pro/Max subscriptions can't power a product. The Agent SDK docs prohibit offering claude.ai login or subscription rate limits in third-party products, and the consumer terms bar automated access except through an API key, sharing accounts, and reselling.
- Building the app with Claude Code on a subscription is fine.
- Expected cost is low because the AI only chooses parameters. At roughly 20k input and 3k output tokens per box: Haiku 5.5 ~$0.004, Sonnet 5.5 ~$0.07, Opus 5.5 ~$0.14 per box. Re-measure once it's built.
- Options: the client supplies their own API key; a spend limit on the key; make the AI optional so a plain form can drive the same engine.
