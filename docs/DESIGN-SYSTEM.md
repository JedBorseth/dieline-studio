# Design system: features, partitions, the panel builder and artwork

Status: spec, revision 5 (2026-11-25), after four rounds of Sol 6.2 and Gemini Neon review. Phases 1 (foundations), 2 (features on standards), 3 (partitions) and 4 (artwork) are built; see [Phase 2 as built](#phase-2-as-built), [Partitions as built](#partitions-as-built) and [Artwork as built](#artwork-as-built) for the readings taken where this spec left a choice. For the reasons behind the engine see [CONTEXT.md](CONTEXT.md), for the code see [ARCHITECTURE.md](ARCHITECTURE.md).

Today the app draws four R&L standards and nothing else. Designers also need handholes, vents, extra scores, partitions, pads, inserts, trays, wraps and one-off changes to a standard. About half of this year's R&L designs were drawn by hand rather than from a standard, so this is most of the work.

Every default number in this spec is **provisional** unless it says it came from a shop file. Provisional values are flagged in the checks until a shop reference or the die maker confirms them.

## Decisions (2026-11-25)

| Question | Decision |
|---|---|
| Scope | Features on any part, new parametric styles (slotted partitions first), and a panel builder for one-offs |
| Features | Handholes, hand flaps (hinge edge chosen per flap, angled flaps too), vent holes, perforations, tear strips, extra scores |
| Partitions | Slotted interlocking sets. Input as box inside size + cells, cell sizes, piece sizes, or product count + item size |
| Partition allowances | Industry defaults, flagged "not calibrated" until compared with real R&L sets |
| Rule breaks | Draw and warn. Refuse only geometry that can't exist or can't be made |
| Handhole default | File_Box_1's: 1 3/8 × 3 5/8, full-radius (11/16) ends, long side horizontal |
| Handhole offset | Measured to the hole's edge or its centre, said explicitly every time. The agent asks when the request doesn't say |
| Builder first targets | Die-cut trays and mailers, pads and inserts, wraps and folders, changes to a standard |
| Builder output | One-off. Not offered as a new style. The job record reopens it exactly as exported |
| Form | Chat only for now. The plain form keeps the standards |
| Free-form cut-outs | Shape library + agent sketch in the artwork's own frame + designer-supplied SVG/DXF, all placed and checked like a handhole |
| Free-form structure | Left out. Revisit only after the fold model is proven, as a flagged one-off "draft" mode |

## The rule this keeps

Agents never write **structural** coordinates: panels, scores, slots, flaps and allowances come from the engine. Agents choose parameters and placements ("centred on the end panels, 2" from the top score to the hole's edge"). The single exception is cut-out artwork, sketched in its own local frame and placed by the engine (see [Artwork](#artwork)). AGENTS.md carries the rule.

Measured offsets are legitimate design dimensions. The engine enforces three things:

- **Relative placement only.** Every distance is relative to a named reference on a named face. There are no raw positions on the blank.
- **No vertex lists for structure.** Point and polyline input exists only in artwork.
- **A bounded builder.** The builder's grammar is a fixed vocabulary with bounded counts, so it can't trace an arbitrary outline (see [Builder limits](#builder-limits)).

The system prompt adds a behavioural rule, which the engine can't detect: don't copy numbers from `get_geometry` into placements to rebuild structure.

## Model

### Pipeline

Each part is built in fixed stages. Every stage rebuilds the face map, the outline, the fold data and the summary values, so later stages and the checks always see the current shape:

```
source → (dieline, face map, fold graph)
       → overrides → (dieline', face map', fold graph')
       → features → artwork → dieline''
       → meta recomputed from dieline'' → checks
```

A summary value such as flap depth, blank size or machine fit is never carried over from an earlier stage. A mutation that fails leaves the previous revision in place and on screen.

### Design and parts

A **design** is a list of **parts**. Each part is one die-cut piece:

- an RSC is one part
- a partition set is a long-piece part and a short-piece part, each with a quantity per set
- a carton plus inserts is several parts

```
Design
  id, revision
  parts: [Part]
  groups: [Group]
  links: [FitLink]
Part
  id (stable, engine-made), label, piece_no (shop number, optional)
  units: in | mm
  board, flute: x | y      # flute direction in the flat blank's frame
  quantity: 1              # per design; a group member's quantity lives in the group
  source: Standard(style, L, W, D, options)    # options include HSC use: tray | lid
        | Partition(...) | Builder(structure)
  overrides: [Override]
  features: [Feature]      # each with a stable id
  artwork: [Artwork]       # each with a stable id
Group
  id (stable), kind: partition_set | telescoping | stack | assembly
  members: [{part_id | group_id, role, quantity_per_group}]   # a stack can hold partition sets and pads
FitLink
  id, container: part or group id, contained: part or group id
```

**Groups**

- A group owns its members' quantities: a partition set is "1 set = 5 long + 11 short".
- **Replacing a set** (`replace_set`) keeps the group id, so links to the group stay valid and are checked again.
  - New members are matched to old ones by role (`long`, `short`, …). A matched member keeps its part id, so direct links to it stay valid.
  - An old member with no match is removed, and its direct links become stale. A new member with no match gets a new id.
- **Replacing a part** (`replace_part(id)`) keeps the part id and its place in its group, so its links stay valid and are checked again.
- Removing a member removes it from its group. A group left empty is removed.

**Fit links**

- Links always run from a container to the contained: a lid **contains** its base, and a carton contains its partition set. The check always compares the container's assembled inside with the contained's assembled outside, so there's no need for a separate `fits_over`.
- A link can point at a group, so a two-piece partition set is checked as one assembled set.
- A link whose part or group is removed becomes **stale** (see [Checks](#checks)).

A part's **flute direction** is part of the part, stored as an axis of the flat blank (`x` or `y`). Designers think of flutes in the assembled box, so:

- **Standards and partitions** accept `vertical` or `horizontal` (in the assembled box) and convert it. On both, vertical means `y`: the body height on a standard, and the piece height on a partition. That's also the default.
- **Builder parts** take the flat axis directly.

The flute direction feeds:

- the machine check, which knows each machine's flute and feed rules
- the fold warnings
- the partition, handhole and tear-strip strength warnings

### Material model

The engine needs to know which board stays attached and which line ends are deliberate. A flat list of lines can't say either, so each part carries a **material arrangement**:

- **Retained regions** of board, and **discard regions** (holes, slot gaps, scrap).
- **Faces** inside the retained regions, with the hinges between them, the slits, and the seams where parts are glued or locked.
- **Typed operations:** every line belongs to one operation (outline, hinge, slot, handhole, perf rail, bridge, …). Operations declare their deliberate open ends:
  - a hand flap's cut meeting its hinge
  - a relief nick
  - a tear-strip pull tab
  - a bridge gap
  - a single slit

  A declared open end still has to keep the board connected.
- **Annotation** never counts towards the blank's size or machine fit.

#### Contact and disposition rules

Every place where two lines meet, cross or come close falls into exactly one class. The class decides what happens:

| Class | Examples | Design | Export |
|---|---|---|---|
| **Permitted junction**: a contact some operation declares | a standard's hinge crease ending at a slot or on the outline; a slit or hand-flap cut ending on its hinge; a notch joining the outline; an extra score ending on the outline; a perforation crossing a crease at a tie | accepted | allowed |
| **Near miss**: no contact, but closer than a clearance limit | a vent 1/8" from a score; two handholes 3/16" apart | accepted, **warn** | allowed |
| **Invalid intersection**: any undeclared touch, overlap or crossing | two vents touching after rounding; a handhole crossing a score; a cut lying on a crease (coincident rules); duplicate rules | the mutation is **refused**, and the previous revision stays | — |
| **Unresolved island**: board meant to stay with no connection to the blank | the middle of a cut-out O | accepted, **fail** | blocked until bridged or the drop-out is accepted |

Arcs are stored with:

- an explicit sweep sign and sweep angle
- a `full_circle` flag

This prevents silent degeneration:

- A **partial arc** has distinct endpoints and a sweep strictly between 0 and one full turn.
- A **full circle** has `full_circle` set, identical endpoints and a sweep of exactly one signed turn.
- The flag, the sweep and the endpoints must agree within tolerance, or the arc is refused. So a short arc whose endpoints coincide after rounding can't turn into a circle, and contradictory arc data can't be stored.
- An arc whose start and end radii differ by more than 0.0001" is refused.
- Zero-length segments and zero-radius arcs are refused.

Validation runs on the composed result:

- every retained region attached, or reported as an unresolved island
- loops closed except at declared open ends
- line/line, line/arc and arc/arc crossings and tangencies classified as above

It runs twice:

- on the design
- on each output file as **reconstructed from that file's own rounded or expanded geometry**

Tolerances are in physical units. If a target's reconstructed geometry has an invalid intersection or an island the design doesn't have, only that target is blocked. This replaces today's `check_closed_outline` and `check_crossings` in phase 1. Today's checks fail every hand flap and ignore arcs.

### Rule types and export capability

Today a segment carries only a line type. Phase 2 adds a **rule spec** per operation:

| Rule | Parameters |
|---|---|
| cut | pointage (steel-rule points) |
| crease | pointage; side (normal or reverse) |
| perforation | cut and tie lengths; phase at the start; crossing policy |
| cut-score / partial cut | depth, side |
| annotation | never manufactured |

**Perforations:**

- A perforation is one continuous path with stationing, so its cut/tie pattern doesn't restart at each panel.
- The default crossing policy centres a tie on every crease it crosses, so the crease rule stays continuous. A cut landing on a crease is flagged.

**Export capability:**

- Each export target (ArtiosCAD CF2, sample-table DXF) has a **capability table** that marks each rule-and-parameter combination as **verified**, **unverified** or **unsupported**. Verified means proved by importing or cutting a reference file on the receiving equipment, mirrored geometry included.
- A target exports only verified combinations. If a design needs an unverified or unsupported one, that target is blocked, with the reason. The design stays visible, and other targets that can carry it can still be exported.
- An export is checked as a whole before anything is written. If any requested file can't be made, nothing is written. The export bar shows each format's readiness, so the designer can untick the blocked one.
- Today only cut = 1 and crease = 2 in CF2 are verified, as is cut and crease in the sample-table DXF.
- Pointage is per operation. The CF2 writer today uses one pointage for the whole file, so a design with mixed pointage is blocked for CF2 until a mixed-pointage import is verified.
- On request, the sample-table DXF can expand a perforation into its dashed cut pieces:
  - It's a separate representation and must be verified in its own right.
  - Its result goes through the topology and gap checks.
  - On thick board (CAL ≥ 3/16) it warns when ties are shorter than max(3/16, CAL), because the blade overcuts the bottom liner.
- CF2 writes a full circle as two half arcs. This is part of the phase 2 import fixture.
- The sample-table DXF lists entities in cutting order: creases, then interior cuts and artwork, then the outer perimeter. The board then stays held down until the last cut.

### Faces and orientation

Every source returns a **face map**.

**Identity and roles**

- **Face ids** are stable topology names that don't change when dimensions change: `P1`..`P4`, `glue_lug`, `top_flap_P2`, `bottom_flap_P3`, `base`, `wall_back`, `piece`.
- **Roles** come from the style's options, never from comparing sizes. A square box (L = W) still knows which panels are ends. Swapping E-S-E-S to S-E-S-E changes what P1 *is*, and `list_faces` reports it.

**Viewing and frames**

- **Viewing side:** placements are given **as seen from the outside of the folded box**, the way designers describe them. The flat drawing is inside-up with the glue lug on the left, so its left and right are mirrored. The engine does that conversion, and `list_faces` says so.
- **Face frame:** each face has a local frame, with `+y` up on the folded box and `+x` to the right as seen from outside. On a flap, "up" means toward its free edge.
- **HSC use:** an HSC part records `use: tray | lid` in its options. It sets which way is up on the folded box, and so what "top" means. It changes the face frames and face ids, never the drawing. The default is `tray`.
  - **Lid:** the flap row is `top_flap_P1..P4`, and the face frames follow the generic rule below.
  - **Tray:** the same row becomes `bottom_flap_P1..P4`. The drawing still has the flaps at flat `+y`, but on the folded box they point down. So the body panels' frame is `+x_face = +x_flat`, `+y_face = −y_flat`, which flips y instead of x. That is still a mirror, as an outside view of an inside-up drawing must be.
- **Drawing side:** each part records whether its drawing is `inside_up` or `outside_up`. Every standard is drawn inside up, glue lug on the left, as today.
- **Export transform:** export transforms are limited to rotation and translation. A mirror is never applied silently, because mirroring a flat drawing is the same as turning the sheet over, which changes the side the creases are pressed from.
  - HSC_GL's standard script ends with a vertical mirror. It's still unknown whether that leaves the ArtiosCAD drawing outside up, so the engine keeps drawing HSC_GL inside up with its flaps at the top until a real HSC_GL export is compared.
  - If a layout ever needs a mirror, it's recorded as a change of drawing side.

**Edges and centres**

- A face has a **boundary**: its exact piecewise outline. P2's top, for example, is a 3/16 cut, the crease, then another 3/16 cut, because of the slots.
- It also has **reference lines** for measuring:
  - `top`, `bottom`, `left`, `right`, `centre_x` and `centre_y`
  - named physical edges such as `top_score`, `free_edge`, `hinge` and `open_edge`
- A hinge's reference line is its full construction line, and its actual creased span is shorter. Both are available. A distance measured along an edge must fall within the edge's span, or it's refused.
- `centred` means the **assembled face centre**, so P1 and P3 match on the folded box. Another datum can be chosen with `datum: material | usable`. A centred feature that falls in a keep-out zone warns; it isn't moved.

#### Geometry contract

The face map is published with exact geometry, so phases 2–5 build on it rather than redefining it.

**Frames**

- Each face has a rigid map between the flat blank and the face frame.
  - On an inside-up drawing it includes a mirror, because the face frame is seen from outside. That's an x-flip, except on faces whose up runs opposite flat `+y` (the HSC tray's body panels), where it's a y-flip.
  - Every flat ↔ face conversion goes through this map.
- Body panels and walls: `+y` is up on the folded box, and `+x` is right as seen from outside.
- Flaps (and any face whose folded position is horizontal):
  - `+y` points toward the flap's free edge.
  - `+x` is right when the flap is viewed from outside with its free edge up.

  This is the flat-hinged frame. The folded orientation isn't used, so a flap's frame never depends on how it closes.
- **Standard flaps** (and lug extensions) always use the flat-hinged frame above, with generic `left` and `right`, whichever way they lie when closed. Opposite flaps point opposite ways when closed, so they can't share a box-fixed frame.
- **A builder base**, and any other face that stays horizontal and fixed in the folded box (not a flap):
  - It's measured with the **box-side names** `front_edge`, `back_edge`, `left_edge` and `right_edge`, where front, back, left and right are the box's sides as seen standing at its front.
  - Generic `left` and `right` references are refused on these faces, because they flip with viewing side.
  - `x` runs toward `right_edge` and `y` toward `back_edge`.
- **Datum, not corner:** each frame's origin sits on the flat score or open-edge line. On the folded box the physical outside corner is OG beyond a score, so a placement measured "from the corner" will need its own reference when it's added.

**Assembled centres**

`centre_x` on a body panel is measured from the panel's score with its **non-joint** neighbour. The joint side moves with the closure, and these formulas absorb that:

| Panel | Measured from | Assembled `centre_x` |
|---|---|---|
| P1 | the P1/P2 score | `(P1A + CAL) / 2` toward the free edge |
| P2, P3 | either of its scores | half the score-to-score width |
| P4 | the P3/P4 score | `(P4A + CAL) / 2` toward the glue lug |

- P1A and P4A are the standard's raw panel sizes (W or L).
- The formulas hold for both inside and outside joints. Outside takes IL off P1's flat width and adds IL to P4's, which moves the flat midpoints, but not the assembled centres.
- A flap's assembled `centre_x` is its parent panel's, so a vent centred on a flap lines up with a handhole centred on the body.
- Material centres differ. On P1, the flap's material centre is 3/32" off the body's material centre, because the flap is flush with the free edge and stops half a slot short of the score. That only matters with `datum: material`.

**Hinges**

- Each hinge is a directed axis along its score line. The child panel lies to the left of the direction of travel, as seen on the inside face.
- A positive fold angle rotates the child toward the inside.
- **Bend offsets** use the board's IL (inside loss) and OG (outside gain) per 90° fold, as ArtiosCAD does:
  - Folded, a panel's inside surface spans its score-to-score width minus IL at each bounding fold. Its outside surface spans that width plus OG at each fold.
  - So the inside corner of a fold lies IL inward from the score line along each panel, and the outside corner lies OG outward.
  - Example: on the RSC, P2 = P2A + CAL score to score, and with IL = CAL/2 its inside is P2A.
- **Transforms:** the fold model places each child from these corners, not as a pure rotation about the score line. Score-to-score sizes stay the flat dimensions, the same reference as the standards' formulas.
- **Assembled datum:** placements are measured on the face's **outside surface** in the folded box. Each face's origin is its bottom-left corner as seen from outside: the bottom score (or open edge) on body panels, and the hinge on flaps, at the face's left side. `x` runs right and `y` up from there. The rigid face map carries this origin and the axes to the flat blank. Assembled centres are still measured from the non-joint score, as in the table above.
- **Material datum:** with `δ = 0` for an inside joint and `δ = IL` for an outside one, and `A1 = P1A + CAL`, `A4 = P4A + CAL`, measured from the scores in the table above:
  - P1: `(A1 − 1/8 − δ) / 2`
  - P4: `(A4 + δ) / 2`
  - P1's flap: `(A1 − 1/8 − δ + 3/16) / 2`
  - P2 and P3: as assembled

**Reference lines**

- Each has a stable id (`P2.top_score`, `P1.free_edge`, `P2.centre_x`), a directed span (start → end in the face frame) and an inward side.
- They're distinct from the actual cut and crease operations along them. A hinge's reference line runs its full construction length, while its crease operation is shorter, between the slots.

**Extended lug faces:** REG_GLI_EXT's upper and lower lug extensions are faces of their own (`glue_lug_ext_top`, `glue_lug_ext_bottom`), creased to the lug. They're never part of `top_flaps` or `bottom_flaps`.

**Keep-out zones**

- Faces carry keep-out zones. The main one is the **glue landing zone**: the strip of P1 along its free edge where the glue lug lands when folded, as wide as the lug.
- With `closure: outside` the lug lands on P1's outside, so the zone covers outside creases and artwork as well as through-cuts.
- On REG_GLI_EXT, the extended lug also lands on P1's flaps.
- Full-overlap and gap/lap flaps carry an **overlap band** where another flap covers them.

#### Selectors

| Selector | Meaning |
|---|---|
| `P2`, `top_flap_P1`, … | One face by id |
| `end_panels`, `side_panels` | Body panels by role, from the panel order option |
| `body_panels` | P1–P4. Never includes `glue_lug` |
| `top_flaps`, `bottom_flaps` | All flaps on that end |
| `top_outer_flaps`, `top_inner_flaps`, `bottom_outer_flaps`, `bottom_inner_flaps` | From the closure order. On CSSC and full overlap, where "outer" isn't defined, they're an error listing the alternatives |
| `[a, b, …]` | A list, duplicates removed. `glue_lug` must be named explicitly to be included |

A selector that matches nothing is an error naming the faces that exist. So is a hinge name that doesn't exist on the style, such as a flap score on a tube.

A feature stores its **selector** as well as the faces it resolved to. When the panel order changes, a selector-bound feature follows its role: "end panels" stays on the ends. The tool result names the faces it now sits on.

### Placement

One schema for every feature and every piece of artwork:

```
on: <selector>
datum: assembled | material | usable              # what "centred" means; default assembled
x: "centred" | {from: <reference>, to: "near_edge"|"far_edge"|"centre", distance}
y: "centred" | {from: <reference>, to: "near_edge"|"far_edge"|"centre", distance}
pattern: {kind: "single"}
       | {kind: "row"|"column", count, pitch}
       | {kind: "grid", cols, rows, pitch_x, pitch_y}
       | {kind: "spread", axis: "x"|"y", count, inset}
rotation_deg: 0
mirror: "none" | "position_only" | "position_and_shape"
pairs: [[P1, P3], [P2, P4]]                        # only when the pairing isn't obvious
```

**References**

- `x` references are vertical lines (`left`, `right`, `centre_x`, a vertical score) and `y` references are horizontal lines. Using one on the wrong axis is refused, so each axis has one unique origin.
- On a sloped edge, the distance is measured along the edge's inward normal.
- From an edge, distance runs inward. From a centreline it's signed: `+` toward `+x` (right, seen from outside) or `+y` (up). `near_edge` is the feature's side facing the centreline. A distance of 0 needs `to: centre`.

**Distances**

- `to` is required whenever an axis isn't `centred`. There's no default, so the agent must ask when the request doesn't say.
- `near_edge` and `far_edge` are the feature's extreme points along the measuring direction, after rotation.
- `centre` is the feature's datum: the centre of its bounding box for library shapes, or the declared origin for sketches and imports. Rotation is about that datum.
- Distances accept fractions, and ArtiosCAD sixteenths on inch parts (`2 3/4`, `2:12`).

**Patterns**

- `x` and `y` place the whole pattern's bounding box. There is one count, in the pattern.
- `spread` owns its axis, so that axis's `x` or `y` must be omitted. It puts `count` copies between the insets, each inset measured from the face's usable boundary to the outer edge of the end copy. The gaps are equal.
- On a tapered face, `spread` uses the usable width along the cross-axis line through the pattern.
- With one copy, `spread` centres it.

**Mirroring**

- `position_only` is the default for opposite faces. It mirrors the placement about the box's centre plane, so "2" from the front corner" stays near the front corner on both ends. Shapes aren't flipped unless `position_and_shape` is asked for, so text and arrows still read correctly.
- Pairing is automatic for two opposite faces (P1↔P3, P2↔P4). Larger selections, such as all four body panels, pair by opposite faces. Anything else needs explicit `pairs`, or it's refused.

**Preview:** every resolved copy is shown with its anchor. A check that fails names the copy, for example "vent 3 of 4 on P3".

### Expressions

Builder dimensions, partition values and placement distances may be expressions.

- **Types:** each expression is either a **length** (in the part's units) or a **scalar**.
  - `2 * CAL` and `W / 2` are lengths. `L / W` is a scalar.
  - A bare number takes its type from where it's used: a **length in the part's units** when added to or subtracted from a length (`W/2 - 1/4`), a **scalar** when multiplying or dividing (`2 * CAL`). A literal can carry a unit (`2in`, `50mm`).
  - `L * W` (a length times a length) and any expression whose result is a scalar in a length field are refused.
- **Grammar:**
  - Numbers: decimals and mixed fractions (`1 1/2`), read as single numbers before any arithmetic is parsed.
  - ArtiosCAD sixteenths (`2:12`) are accepted on inch parts only, and take no unit (`2:12mm` is refused).
  - Operators: `+ - * /`, unary minus and parentheses.
  - Functions: `min`, `max`, `abs`, `round(x, quantum)`. The quantum is a length (`1/16in`, `1mm`), so rounding means the same thing in any units.
- **Names:**
  - Board values `CAL`, `IL`, `OG` and `CRRV` are converted to the part's units first, so `D - CAL` works in mm.
  - Dimensions `L`, `W` and `D` come from the part's `dims` field.
  - The part's own `params` may not reuse a reserved name.
- **Evaluation:**
  - Dependencies are ordered, and a cycle is an error naming the loop.
  - Results must be finite. Division by zero is an error.
  - A **dimension** (a panel size, depth, width or feature size) at or below the export precision is refused, naming the expression. Placement distances may be zero, and negative where a signed reference allows it.
  - Expressions are bounded in length, work and result magnitude, and are never run through `eval`.
- **Recording:** the job record stores each expression and its resolved value.

## Features

| Feature | Parameters (defaults) | Construction |
|---|---|---|
| Handhole | `obround` (1 3/8 × 3 5/8, full radius), `rect` + corner radius, `d_shape`, `circle`; size; placement | Closed cut |
| Hand flap | profile + `hinge: top|bottom|left|right` + `double_crease` + relief nicks; `angled` with `taper_deg` | Cut on the free sides, crease on a straight hinge |
| Vent hole | `circle` (diameter), `obround`, `rect`; usually a pattern | Closed cuts |
| Window / cut-out | rectangle, rounded rectangle, circle, ellipse | Closed cut |
| Perforation | route (below) + rule spec | One perforation path |
| Tear strip | route, width, start side, pull tab (`angled_tab`, `thumb_hole`, `edge_tab`), rails: perf or cut-score | Two parallel rails + the tab cut |
| Extra score | `horizontal` or `vertical`, one reference + distance, a route over faces; crease side | Crease path, ending on the outline it meets (a declared contact) |
| Edge notch | on a straight span of a named edge; position along the edge; half-circle, V, rounded rectangle | Replaces part of the straight span |

`hinge` is in the face frame and is applied after `rotation_deg`, so a rotated obround's hinge is still the side named.

### Hand flaps

A hinge must be straight, and the cut must meet it at a real angle (default at least 35°). A tangent join leaves a sliver the die can't make.

- **Default hinged profile:** the shop's handhole is the File_Box_1 obround. A hinged version of it uses a **secant hinge**: the hinge is a straight chord inset from the long side, which cuts through the end arcs at a real angle. The engine solves the inset from the minimum angle. The flap is slightly shorter than the hole.
- **D-shape:** a straight hinge with rounded far corners. It suits any side.
- **Rectangle:** any side.
- **Circle:** hinged by a secant chord, like the obround.
- **Double crease** (File_Box_1 has one):
  - The second crease sits 2 × CAL **outside** the opening, away from the hole.
  - The cut ends on the inner crease.
  - Relief nicks, when asked for, run outward from the inner crease's ends to the outer one.
  - The carry web is measured from the outer crease.
- **Angled flaps:** the flap always narrows away from the hinge, so it can swing into the opening. A negative taper (a dovetail) is refused.
- **Bottom hinge:** allowed, for access or vent flaps. It returns an info note that a top hinge is usual for carrying.
- **Unsupported combinations** of profile, hinge and rotation are input errors listing what works.

### Routes across faces

Perforations, tear strips and extra scores that cross faces follow a **route**, an ordered, directed list of spans:

- the start face, the start edge and a datum
- the faces crossed, which must be adjacent and joined by hinges
- the end
- what happens at slots: stop, or jump the slot, with the stationing continuing over the gap
- what happens at the glue joint: cross it on the lug, or stop short

Tear strips:

- A tear strip around all four body panels continues across the glue lug at the same height by default.
- At the joint, the check looks at both plies:
  - the rails must line up within a tolerance
  - glue must not land on the tab
  - the tab must be reachable once the box is assembled

  A strip that stops at the joint warns that the lug will block it.
- Strip width under 1/2" warns that it may snap.
- A pull tab within 1/2" of a vertical score or in the glue landing zone warns.
- A plain perforation strip on double-wall board (EB, BC) warns that it may not tear cleanly. A strip running parallel to the flutes warns that the tear may wander.

### Notches

All notches on one edge are applied in one pass:

- each must lie on the edge's straight span, clear of corner fillets and of each other
- they're placed by distance along the edge
- the span is split once

A notch on a crease, a curve or a corner is refused with the reason. A notch is a boundary operation, so it touches the outline by design; the containment rule for holes doesn't apply.

### Feature checks

| Check | Default (provisional) | Level |
|---|---|---|
| Clearance from scores, slots and edges | 1/4", per process profile | warn |
| Handhole carry web (to the score above) | `max(1", 6 × CAL)` | warn |
| Handhole side web (to the scores either side) | `max(1", 4 × CAL)` | warn |
| Combined ligament: the uncut board on the load path once neighbouring vents, perfs and holes are subtracted | as carry web | warn |
| Handhole on HSC_GL with no score above it (`use: tray`, open edge up) | — | info |
| Carry web with flutes parallel to the handhole's long side | — | warn |
| In the glue landing zone; message names the intrusion ("enters the 1 1/4" glue zone by 9/16") | — | warn |
| On an overlap band without a matching cut-out on the covering flap | — | warn |
| Closer than the clearance to another feature (near miss) | — | warn |
| Touching, overlapping or crossing another feature or a score without a declared contact | — | refuse |
| Outside its face | — | refuse |

Machine eligibility: a part with any die-cut feature can't run as a plain slotted job. The machine check reports sheet-size fit separately from what the machine can do (slotting only, rotary die or flatbed die, allowed rotations, and its flute and feed axis). It lists only machines that can make the part.

## Partitions

New style family `partition-slotted`: interlocking pieces, half-slotted so they cross. A partition set is a **group** with its own id. Its pieces are updated together.

### One crossing model

All four input modes are converted into the same **grid** before anything is drawn:

- the two axes, labelled `long` and `short`
- the installed clear cells along each axis
- the walls (pieces) between the cells, with each piece's board and which way it faces
- the crossings: where each long piece meets each short piece

The arithmetic, for one axis with box inside span `B`, wall thicknesses `t` and total drop-in clearance `C`:

```
installed clear cells:  sum(cells) + sum(t) = B
piece length:           B − C
slot centre, box frame: cell_0 + t_1/2, then + cell_1 + t_1/2 + t_2/2, … (from the first box wall)
slot centre, on piece:  box-frame position − C/2   (measured from the piece's near end;
                        reversed for a piece that faces the other way)
```

- The end legs of each piece are C/2 shorter than their end cells, but the installed cells are true size against the carton walls.
- The engine reports the installed cells, each piece's length and end legs, the tip gaps and the box inside size the set needs.
- A count describes cells along an axis. `cells_long = 4` means four cells along the long axis, which takes three short pieces.

### Input modes (`input_mode`)

1. **`box_cells`:** the box inside `L × W × D` plus `cells_long × cells_short`. The cells are equal.
2. **`cell_sizes`:** lists of installed clear cell sizes per axis, unequal allowed, plus the height. Solves for `B`.
3. **`pieces`:** each piece type's length, height and slot positions, measured from a named piece end. These are converted to a grid. Positions that don't make one consistent grid are refused, naming the mismatch.
4. **`product`:**
   - inputs: count, item size (`diameter` or `length × width`, both orientations tried), item height, cell clearance (default 1/16 per side), and the maximum box size
   - perimeter: `none`, `air_cells(width)` (an empty ring of cells, which adds walls) or `liner(thickness)` (space left for separate liner pads)
   - it returns **layout candidates**, each with an id, the cell size, the set's outside size and an estimated sheet usage
   - nothing is drawn until one is chosen (see [Agent tools](#agent-tools))

### Rules (industry defaults, flagged "not calibrated")

| Value | Default | Notes |
|---|---|---|
| Drop-in clearance | 1/8 total per axis | split between the ends |
| Piece height | inside depth | option: shorter |
| Slot width | mating CAL ≤ 1/8: CAL + 1/32; mating CAL > 1/8: CAL + 1/16 | uses the **mating** piece's caliper, so EB and mixed boards are covered |
| Slot depth | H/2 + 1/32 on each piece | total overcut 1/16 |
| Slot bottom | round where the slot is at least twice the minimum bend radius wide; square otherwise | a narrow round slot can't be bent from steel rule |
| Slot mouth | 1/8 × 45° lead-in, reduced automatically to fit | option: square |
| Slot direction | long pieces from the top, short from the bottom | option to swap |
| Boards | the set's board | `board_long` and `board_short` options |
| Flute | vertical in the assembled set | option: horizontal |

Every partition design carries a "not calibrated" warning until a real R&L set has been compared.

### Partition checks

Interlock is checked from the actual slot shapes at each crossing, with both pieces standing on a common floor:

- **Overlap:** the overlap uses the **usable** depth, the part of each slot as wide as the mating board. A round bottom loses depth. No overlap: refused. More than 1/8" of vertical play: warn.
- **Slot cuts through:** a slot that reaches the far edge splits the piece. Refused.
- **Residual web:** measured from the deepest cut. Under 1/2": warn. Under 1" on a vertical-flute piece: warn that it may snap along a flute.
- **Fit:** slot width below the mating caliper is refused. Above that plus 1/16 warns that it's loose.
- **Mouths:** lead-ins that overlap are refused. A rim between neighbouring mouths narrower than max(1/4", 2 × CAL) warns. So does a mouth within that distance of a piece end.
- **Small cells:** cells under 3 × CAL warn.
- **Reciprocity:** crossing counts and positions must agree between the long and short pieces, or the set is refused.
- **Height:** taller than the box's inside depth warns that the lid won't close.
- **Orientation:** a piece with an asymmetric slot plan gets an info note, since packers can insert it backwards. An orientation notch is suggested.
- **`1 × n` cells:** one piece type, with no slots. The tool needs `confirm_degenerate: true`.
- **`1 × 1`:** no dividers. Refused, with guidance.
- **Product mode:** a count with no exact layout (e.g. 7) lists layouts with spare cells. Sheet usage is labelled an estimate.

### Parts and quantities

- Each distinct piece is a part in the group. By default a set has long and short pieces.
- Pieces on one axis with different slot plans (unequal cells, mixed boards) become separate parts, each with its own quantity.

## Fit between parts

A **fit link** (see [Design and parts](#design-and-parts)) joins a container to what it contains:

- an insert, pad or partition set in a carton
- a carton in a carton
- a base under a telescoping lid (the lid is the container)

How the check works:

- It compares the contained part's or group's **assembled outside envelope** with the container's **assembled inside envelope**.
  - For a standard, outside is inside + 2 × CAL, or the style's own outside formula.
  - For a builder part, it's the folded envelope.
  - For a group, it's the assembled set.
- Slip fit: under 1/16" clearance per axis warns that it's tight, and over 1/2" warns that it's loose.
- A **stack** group (tiers of partitions with layer pads) checks that the tiers plus the pad thicknesses fit inside the container's depth.
- Two sets linked into one container without a stack group warn.
- Resizing either end re-runs the check and names the links that fail.

What isn't checked:

- Loading past inside joints and flaps. It's listed as unchecked.
- On telescoping pairs, whether features on the lid and base walls line up. Also listed as unchecked.

## Panel builder

A structure language for one-offs. The agent describes panels and how they join, and the engine does the arithmetic, the allowances and the drawing.

### Conventions

- Parts are drawn inside face up, like the slotted standards.
- `flute: x | y` is required.
- **Edges** are named relative to the panel:
  - On the root panel: `front`, `back`, `left`, `right`, the box's sides as seen standing at its front, the same names its placements use.
  - On an attached panel: `hinge` (toward its parent), `free` (opposite), and `left` and `right` as seen from outside with the free edge up.
- **Folds:**
  - Each hinge has an oriented axis along the parent's edge.
  - The fold sign is relative to the parent: `crease` folds the child toward the inside, `reverse_crease` toward the outside.
  - Angles are magnitudes, `0 < angle ≤ 90` per hinge. Anything past 90 is a rollover (below).
- **Thickness:** each face has an inside and an outside surface. Each hinge has its bend offset, set by the allowance policy.

### Structure

The agent sends JSON. This 12 × 8 × 3 inside rollover tray is the reference example, validated by a test:

```json
{
  "units": "in", "flute": "y",
  "dims": {"L": 12, "W": 8, "D": 3},
  "root": {"name": "base", "w": "L", "h": "W", "dims": "inside"},
  "attach": [
    {"to": "base", "edge": "back", "name": "wall_back", "kind": "panel", "depth": "D", "fold": "crease", "angle": 90},
    {"to": "wall_back", "edge": "free", "name": "roll_back", "kind": "rollover", "depth": "D", "traps": ["dust_left_back", "dust_right_back"]},
    {"to": "base", "edge": "left", "name": "wall_left", "kind": "panel", "depth": "D", "fold": "crease", "angle": 90},
    {"to": "base", "edge": "right", "name": "wall_right", "kind": "panel", "depth": "D", "fold": "crease", "angle": 90},
    {"to": "wall_left", "edge": "left", "name": "dust_left_back", "kind": "dust_flap", "length": "W/2 - 1/4"},
    {"to": "wall_right", "edge": "right", "name": "dust_right_back", "kind": "dust_flap", "length": "W/2 - 1/4"},
    {"to": "roll_back", "edge": "free", "name": "locks_back", "kind": "lock_tab",
     "tabs": {"pair": {"inset": 1, "length": 2}}, "mates": {"face": "base", "near": "back"}}
  ],
  "sequence": ["wall_left", "dust_left_back", "wall_right", "dust_right_back", "wall_back", "roll_back", "locks_back"]
}
```

What the structure can say:

- **The root panel:** a rectangle defined by expressions. `dims: inside | outside | score` says what its numbers mean.
- **Attachments**, to a named parent edge, with:
  - a span: `full`, `centred(length)`, or `from(reference, offset, length)`
  - the depth
  - the fold and its angle
  - a `kind`
- **Shared edges:** several attachments may share one edge if their spans don't overlap. Overlapping spans are refused, naming both.
- **`kind: panel`** (default): a plain panel. Its free end can be `square`, `tapered(angles)`, `rounded(radius)` or a tuck profile.
- **`kind: rollover`:** a first-class double hinge. Two creases with a bridge face between them, folding a total of 180°, and an inner wall:
  - The bridge width comes from the layer stack it wraps. The generic rule is CAL per layer of board it bends around: itself, plus each trapped dust flap listed in `traps`.
  - The inner wall's height is reduced so it rests on the base, not past it.
  - The inner wall steps in at both ends to clear the side walls, by the side wall plus the trapped layers.
  - On heavy board (CAL ≥ 3/16, or any double wall), a rollover whose hinges run parallel to the flutes warns that the outer liner may crack.
- **`kind: dust_flap`:**
  - It's a real attached panel, owned by one wall. Two walls can't both own one corner: a second claim is refused, naming the owner.
  - Its length is explicit until a calibrated default exists.
  - It has a clearance taper (default 10°), a hinge inset (IL) and a setback from the rollover top (default `min(1/4", max(CAL, D/8))`; 0 when the mating wall has no rollover).
- **`kind: lock_tab`:**
  - One or more tabs on a free edge, with `pair(inset, length)` for the usual symmetric pair.
  - Each one `mates` with a receiving face and the edge it lands near. The engine creates the slot (see [Fold model](#fold-model)).
- **Corners** between neighbouring attachments: `slot(width)`, `relief(V | radius)`, `mitre` or `none`. Wrap spines default to a 3/8 inter-flap slot.
- **Wrap chains:** a run of parallel hinges, such as a folder or a 5-panel wrap, is one chain of panels with their widths. Each panel's allowance counts the layers it wraps, so outer panels grow by one thickness per layer inside them.
- **`sequence`:** the folding order. It's required for parts with locks.
- **Roadmap kinds** (refused, with "not yet supported", until a reference is reproduced):
  - `glue_flap`: width, taper, mate, inside or outside seam
  - `wing_lock` and `tuck_flap`: a wing or tuck that catches in a cavity or slit instead of a base slot

### Allowance policy

One function turns each requested dimension into score-to-score values, once. It records the result and where it came from: which rule and which board values.

- **Supported patterns:** each one (pad, wrap, rollover tray, …) defines:
  - its measurement endpoints, for example between the inner surfaces of opposite walls
  - whether the inputs are inside, outside or score dimensions
  - the conversion: bend offsets from IL and OG, plus the layer stack
- **Generic rules** start every pattern and are labelled provisional:
  - 90° fold: the inside-loss terms from the board (IL), not a flat + CAL per fold
  - rollover: the layer-stack bridge above
  - wrap chains: one CAL per wrapped layer

  Each pattern is calibrated against a shop reference before its provisional label is dropped.
- **One deduction:** a dimension never gets two. The rollover in the example takes `depth: D` and the policy does the rest; nothing is subtracted by hand.
- **Independent check:** after solving, the fold model measures the assembled dimension it produced and compares it with the request.
- **Layers inside:** across a rollover tray, the inside span between opposite walls loses each side's outer wall, trapped dust flaps and inner rollover wall. This comes from the stack, not a fixed rule.

### Fold model

The builder keeps two graphs:

- **Hinge tree:** the panels and their hinges, which gives each panel's 3D transform. A panel with two parents, or a cycle, is refused.
- **Constraint graph:** the glue seams, lock contacts and resting contacts.

After every operation that removes board (slots, notches), hinge connectivity is rebuilt. A hinge whose crease has been cut away entirely no longer joins its panels, and a panel left with no connection is refused.

**Lock tabs and slots:**

- The engine projects the tab's inner and outer surfaces onto the receiving face along the insertion path. That gives the landing band.
- The slot covers the landing band plus clearance on each side (default 1/32). For a single-thickness tab the slot is CAL + 2 × clearance wide.
- A slot that overlaps the crease zone splits the crease and cuts into the receiving face.
- The corner radius is at most half the slot width and at least the minimum bend radius. If it can't be both, the corners are square.
- A slot is never placed from an agent-given position.

**Assembly:** panels fold in `sequence` order.

- Each lock declares its receiving face, its insertion direction and where it sits in the sequence.
- The check sweeps each panel's actual profile, with its thickness, through its fold and insertion motion, against everything already folded. So a late fold that hits an inserted tab is caught.
- The result is one of four:
  - fits through the motion
  - interferes, with where
  - final pose only, when the motion couldn't be checked
  - needs board flex the model doesn't simulate

  The last two are warnings, never "fits".
- Contact exceptions are local: the band along each hinge, the tab inside its own slot, and glue seams. Anything else touching is interference.

**Proving it:** the oriented frames and hinge graph are built in phase 1, for the standards. The builder doesn't offer mailers until two references reproduce: one scored wrap, and one rollover tray with locks (`DC_TRAY_2_ROLLOVERS`, then `MEATBOX_WL_DF_1SL` once `wing_lock` exists).

### Builder limits

The vocabulary is fixed: panels, attachments, kinds, corners, chains and profiles. Each count is bounded, by default to 40 panels and 8 profile parameters per panel.

Anything outside that is refused with a list of what's supported. The grammar grows only when a shop reference shows it's needed.

### Changes to a standard

Overrides name faces, never lines. Each override declares what it reads and what it writes: faces, edges, hinges and glue areas.

| Override | Effect |
|---|---|
| `flap_depth(face, value)` | Moves that flap's tip; its slots follow |
| `flap_profile(face, rounded(r) | tapered(a))` | Changes the tip. The straight middle stays notchable |
| `remove_flap(face)` | The hinge becomes a cut, the slot walls are trimmed back to the body score, and the face is removed |
| `cut_down(face, cut_by | remaining_height, mode: straight | u_cut(shoulder))` | Straight removes the panel's top flap, and the exposed spans of the neighbouring vertical scores become cuts. U-cut is a display cut-out that keeps the shoulders and scores. Exactly one of `cut_by` and `remaining_height` |

**Compatibility:**

- `flap_depth` and `flap_profile` combine on one flap, depth first.
- `remove_flap` conflicts with any other override on that flap.
- Two overrides of the same kind on one face conflict.
- A conflict refuses the second override, naming the first.

**Glue lug:**

- Cutting down P4 trims the lug to P4's new height and re-applies its tapers. On REG_GLI_EXT it removes the upper lug extension. A lug left under 2" warns.
- Cutting down P1, or removing P1's top flap on REG_GLI_EXT, warns that the lug has nothing to land on.

**Flap checks:**

- A flap longer than the opposite inside span minus CAL warns that it won't fold flat.
- A flap under 1" warns.
- On a `regular` style, flaps that now overlap warn.

**Dependent features:** every feature reference is re-resolved after each override. That includes references on faces the override didn't name. Cutting down P2 changes P1's and P3's edges.

- A feature whose reference is gone, or whose meaning changed, is **stale**.
- A stale feature is drawn ghosted and **blocks export** (fail, naming the feature) until it's rebound to an existing edge or removed. It never silently disappears.

**Check outcomes:**

- `not applicable`, with the reason, for example flap closure after `remove_flap`.
- `unavailable`, when the data the check needs is missing.
- `stale`.

These are separate from pass. The machine, geometry and calibration checks always run on the modified shape.

**Labelling:** the design name and job record list the overrides, and the calibration note says "modified from REG-GLIO-NX".

**Resizing:** `update_part` follows the same rules. A resize, an order swap or a style change re-resolves every feature through its selector. Features that no longer fit become stale, and the resize itself succeeds. A resize can carry feature changes in the same call.

## Artwork

Cut-outs that aren't structure: a smiley, a logo, lettering, a decorative window. Artwork is always **decorative**:

- it stays on one face
- it stays clear of every hinge, slot, mate and edge
- it can't change the outline, be a mate or be referenced by a selector

### Sources

1. **Shape library:**
   - circle, ellipse, rounded rectangle, polygon with corner radius, star (points, inner ratio), heart, smiley, arrow
   - **stencil text:** outline letters with stencil breaks already drawn as cut geometry. This is the default for cut lettering.
   - **stroke text:** single-line letters, allowed only as crease (embossed) or annotation. Cutting it would make hairline slits, so `cut` is refused with that reason.
2. **Agent sketch:** for anything else.
   - The agent writes the outline in a **0–100 canvas**, using shapes (`circle`, `ellipse`, `rect` with radius, `polygon`) and SVG-style paths (`M L Q C A Z`).
   - Arcs use SVG's endpoint form. If an arc's radius is too small to reach its endpoint, SVG enlarges it. The engine reports that correction, and refuses the arc if the correction is more than 1%.
   - The sketch is scaled uniformly to the requested width or height.
   - The part is labelled "agent-drawn artwork" in the checks, the preview and the job record.
3. **Imported artwork:** an SVG or DXF the designer supplies from the reference folders.
   - Files are confined to the reference folders, as with `read_file`.
   - SVG is read with a safe XML parser. It supports `path`, `rect`, `circle`, `ellipse`, `polygon`, `polyline` and local `use` references, with transforms resolved.
     - What's imported is the **cut contours**, not the filled appearance. Fill rules (nonzero vs even-odd), strokes, clipping and hidden paths can make a file look like one opening but cut as two.
     - So the import shows the retained and discarded result, and waits for approval before committing.
   - DXF supports `LINE`, `ARC`, `CIRCLE`, `LWPOLYLINE` and `POLYLINE` (with bulges), `SPLINE`, `ELLIPSE` and `INSERT` (mirrored and scaled blocks resolved). Explicit units are respected, and out-of-plane entities are refused.
   - Unsupported content (live text, images, unknown entities) is listed. The import stops until the designer chooses to go ahead without it.
   - Scaling is uniform by default (`width`, `height` or `fit_box`). A unitless file needs its size given.
   - File size, entity count and segment count are limited.

**Curves:**

- Béziers, splines and ellipses are fitted with **arcs** (biarcs) within the tolerance (default 0.002" at final size), so dies and the sample table get smooth, bendable curves rather than tiny chords.
- Straight-line fallback is used only where arcs can't fit, and it's reported.
- The source curves are kept for the tooling checks, so the results don't depend on how the curve was fitted.

**Rules and open paths:**

- Each closed loop's rule is `cut` (default), `crease`, `perforation` or `partial cut`. Every rule other than cut goes through the export capability table.
- Open paths are never closed silently. A loop declared closed may snap endpoints within 0.005" at final size. Otherwise an open cut path is refused. Open paths are fine as crease or annotation.

### Suspicious artwork

Artwork is untrusted. These cases are flagged:

- a long straight path parallel to a nearby hinge or edge (it looks like a fold or slot)
- slot-like rectangles near an edge or a mate
- a path spanning more than 80% of a face
- any crease or perforation artwork longer than 50% of the face width

A flag blocks export until the **designer** acknowledges it in the UI. The agent can't acknowledge it.

### Artwork checks

Each check produces one result, with a count, the worst value and up to five sample locations, so a 500-segment logo can't flood the panel.

**Islands and bridges**

- Retained and discarded regions are worked out across all artwork loops and features together.
- A plain hole, such as a smiley's eyes, is expected to drop out. Board that is meant to stay but has no connection to the retained board, such as the middle of an O, is an island.
- The fixes offered:
  - **Bridges:**
    - Each is a continuous corridor of retained board from the island, across the opening, to retained board. It is not a gap that ties the island to scrap.
    - At least two per island, each at least max(3/16", 1.5 × CAL) wide, placed across the flutes where possible.
    - The cuts are redrawn around them. The corridor widths and connectivity are checked again.
  - **Stencil breaks**, which work the same way.
  - **Partial cut:** turning the outer loop into a partial cut.
  - **Drop-out:** accepting the drop-out.

  The last two change what gets made, so they're recorded as explicit choices.
- An unhandled island is `fail`: drawn, but export is blocked until it's bridged or the drop-out is accepted (see [Contact and disposition rules](#contact-and-disposition-rules)).
- Tests cover two and three levels of nesting.

**Tooling limits**

Defaults are for 2-pt rule. All are provisional until confirmed with the die maker, and each one warns:

- **Bend radius:** curves tighter than 1/8" (3/16" for 3-pt rule on double-wall), measured on the rule's centreline, from the source curvature. Straight-to-straight corners are mitred knives, so they're not flagged.
- **Acute angles:** interior angles under 35° pinch the dieboard.
- **Short straights:** straight segments under 1/8", between true corners. Curve fitting doesn't count.
- **Thin webs:** cuts closer than max(1/8", 2 × CAL).

**Placement**

- Too close to the face edge (default 1/4"), or in a keep-out zone: warn.
- Outside its face: refused.

## Checks

`CheckResult` becomes:

```
{id, status: pass|info|warn|fail|not_applicable|unavailable|stale,
 category: geometry|feature|fold|partition|artwork|fit|machine|calibration|export,
 message, part_id?, feature_id?, region?}
```

A **refused mutation** is not a check. It's a tool error that says what to change, and the previous revision stays as it was. Checks describe a committed revision:

| Status | Meaning | Export |
|---|---|---|
| `fail` | The design is drawn but has something that must be resolved: a stale feature or link, an unresolved island, unacknowledged suspicious artwork | blocked |
| `stale` | A reference that no longer resolves (shown as its own status, counted as `fail`) | blocked |
| `warn` | Drawn and exportable. Shown in the checks panel, the job record and the agent's tool result | allowed |
| `unavailable` | The check couldn't run, because data is missing. Shown as "couldn't check" | allowed, counted as `warn` |
| `info` | A note, such as "agent-drawn artwork present" | allowed |
| `not_applicable` | The check doesn't apply, with the reason | allowed, ignored |
| `pass` | — | allowed |

`overall()` is `fail` if any check is `fail` or `stale`; otherwise `warn` if any is `warn` or `unavailable`; otherwise `pass`. `info` and `not_applicable` never change it. Target-specific blocks (an unverified rule, an invalid reconstruction) are reported per export target, not in `overall()`.

- **Process profiles:** manufacturing checks use named profiles (process, tooling, board). Each limit is marked measured, provisional or unavailable.
- **Later checks**, added as the shop supplies the data:
  - spacing between cut, crease, perf and glue zones
  - cut-to-score ligaments
  - small scrap, ejection and hold-down
  - trim and gripper margins per machine

## Agent tools

All parameter-only.

**Revisions**

Every design instance has an id and a revision number.

- **Binding:** when a chat turn starts, the engine binds the turn to the current design's id and revision. With no design yet, it binds to "none".
- **Order:** tool calls in a turn run one at a time, in order.
- **Advancing:** each mutating call is **atomic**. It applies completely or not at all. On success, the turn's binding moves to the new revision. The agent never passes revisions; the engine tracks them for the turn.
- **External edits:** anything outside the chat that changes the design gives it a new revision or a new id. That includes the form, an opened job and the UI's own edits.
  - The design id and revision are compared **at commit**, under the same lock as the write (compare-and-swap). An edit made while a call is regenerating therefore isn't overwritten; the call is refused with "the design changed outside the chat".
  - A successful `view_design` rebinds the turn to the snapshot it returned. The agent must call it before trying again.
  - `list_faces` never rebinds: the face map doesn't show the changed design, so after an outside edit it's refused with the same "call view_design" error.
- **No exceptions:** this includes `new_design` and `action: replace_design`, so the agent can't discard the designer's form edit unseen.
- **Retries:** each mutating call carries the tool call's id. A repeated call with the same id returns the first result and isn't applied twice, so a retried `add` can't double the parts.
- **Exports:** `export_files` is bound to an exact snapshot (design id + revision). The file list, validation and writing all use that snapshot.
  - The UI's export bar shows the file list for the snapshot on screen, and its Save passes that design id and revision. Opening another design, even one at the same revision number, invalidates the approval.
  - A mismatch refuses the export and refreshes the list.

**Same pattern everywhere**

- Every mutating tool takes `dry_run: true`. That returns the resolved placements, the checks and a picture without changing anything.
- Lists of features, overrides and artwork use **batch upsert by id** (`items: [...]`, where an item without an id is new), plus `remove: [ids]`.

| Tool | Purpose |
|---|---|
| `new_design()` | Start an empty design |
| `create_box(..., features?, overrides?, action: replace_design | add)` | A standard part |
| `create_partition(input_mode, ..., action: add | replace_set(set_id) | replace_design, contained_in?, confirm_degenerate?)` | A partition group. `product` mode returns layout candidates and draws nothing |
| `choose_layout(candidate_id, action, contained_in?)` | Commits a product-mode layout (below) |
| `build_part(structure, action: replace_design | add | replace_part(id))` | The panel builder |
| `update_part(part, ..., features?)` / `remove_part(part)` | Quantity, board, dimensions, options, use; can carry feature changes |
| `set_features(part, items, remove)` | Features |
| `set_overrides(part, items, remove)` | Overrides |
| `set_artwork(part, items, remove)` | Artwork. Changing only a placement doesn't resend the sketch |
| `list_faces(part)` | Face ids, roles, frames, reference lines, keep-out zones, applicable selectors, viewing side |
| `view_design(part?, region?)` / `get_geometry(part?, ...)` | `part` is required when the design has several parts; the error lists them |
| `export_files(..., formats, dxf_expand_perfs?)` | Exports the current revision (below) |

**Choosing a layout**

- A candidate is tied to its inputs, the parent part's revision and the board and rule versions. If any of those changes, the candidate expires.
- Candidates show in the chat as cards. The designer's click on a card is the normal way to choose.
- The agent may call `choose_layout` only after the designer names a candidate in a message. The tool records that message's id.

**Keeping turns within budget:** mutating tools return a compact typed summary (affected parts, faces, warnings) and one picture of the affected part, not the whole design.

**System prompt additions**

- **Placement rules:** outside view, always say edge or centre, and ask when the request doesn't say.
- **Check levels:** refuse, warn and info.
- **No structure in artwork:** structure is never sketched.
- **Read-back:** after a change, the agent restates it in plain words before moving on:
  - Each feature: its face names, its dimensions, and whether distances run to the edge or the centre.
  - Partitions: the layout, installed cell sizes, piece sizes and quantities, flute direction, and "not calibrated".
  - Builder parts: the panels and folds in order, the inside dimensions the fold model measured, and the lock results.
  - Agent-drawn artwork: "agent-drawn sketch, verify before cutting", its size and placement, and the island options when there are islands.

The UI, the RPC and the agent all go through the same mutation and revision pipeline.

## UI

Chat only for now.

- **Part tabs:** each part is a tab showing its quantity, board and a worst-status dot, so a warning on another tab is visible. Design-level checks (fit, groups) show above the tabs.
- **Tagged preview:** the engine sends a preview scene per part, next to the CF2.
  - Segments are tagged with operation, provenance (structure, feature, library artwork, imported artwork, agent sketch), `feature_id` and `face_id`.
  - The CF2 stays clean for ArtiosCAD.
  - Stale features are drawn ghosted.
- **Features list:** collapsible. Hovering a feature highlights it, and clicking zooms to it. The agent can mention a feature as a chip link (`[handhole 1](feature:<part_id>/<feature_id>)`, scoped to its part) that does the same.
- **Agent-drawn artwork:** drawn in an accent colour inside a dashed frame labelled "Agent-drawn sketch", keeping its line style (solid cut, dashed crease, dotted perf). The header shows an info pill.
- **Checks panel:**
  - Actionable warnings (geometry, feature, fold, partition, artwork, fit) are kept apart from calibration and board notices. When actionable ones exist, the panel opens or shows a banner.
  - Clicking a warning switches to its part, highlights the feature and zooms to the region.
  - Suspicious-artwork acknowledgement happens here.
- **Export bar:** shows the exact file list and each format's readiness before saving, including any format blocked by an unverified rule.
- **Form:** keeps the standards. If the current design is anything other than one unmodified standard part, Generate asks first: "Replace the current design? Its features, overrides and extra parts will be lost."
- **Agent picture (`render.py`):**
  - It uses the face map instead of guessing the body from creases. Today a vertical hand-flap hinge would corrupt the labels.
  - It labels the faces (GL, P4 … P1).
  - It adds callouts for feature offsets (marked edge or centre) and for partition cells and slots, plus the part name and quantity.

## Exports and job records

### Files

| Design | CF2 / DXF | Record |
|---|---|---|
| One part | `<stem>.cf2`, `<stem>.dxf` (as today) | `<stem>.dieline.json` |
| Several parts | `<stem>-<part slug>.cf2`, `<stem>-<part slug>.dxf` | `<stem>.dieline.json` |

- **The record:** `<stem>.dieline.json` is always written. It's the manifest: every part with its quantity, units, files and hashes, plus the exporter profile.
- **Ownership:** each export belongs to a design id, a revision and a destination plus stem. The record shows which job owns which files.
- **Part slugs:** made from stable part ids and shop piece numbers. They're checked for collisions after Windows name normalisation and truncation.

### Replacing

**Reservations and ownership**

- Every output path, new or old (including old paths that the new set won't reuse), is normalised: case-folded, with Windows reserved names and length rules applied.
- The export **reserves every one of those paths** before checking ownership.
  - The reservation is the root-level lock file of each destination root, held for the whole check and publish (roots locked in path order). That reserves every path in the root at once, so a multi-part `A-b.cf2` and another job's one-part `A-b` can't race. A per-path registry would let exports to one folder overlap; they take a second or so, so it isn't worth its stale-entry handling.
  - A lock file records its owner, workstation, process and time.
  - Every root's lock lives in the hidden `.<folder>.dieline-pending/` beside it, whatever role the root plays, so one physical folder has one lock. Roots are locked once each, in one order (by resolved, case-folded path).
  - A stale lock is broken only by naming it: the designer's confirmation carries that lock's id. Breakers take turns through a guard file; a lock still being written is judged by its file's age.
- With the reservations held:
  - **Owned by another job:** a path whose manifest belongs to another job blocks the export, naming that job.
  - **Unowned:** an existing file that no manifest owns blocks unless the designer confirms replacing it.
  - **Changed:** an old file whose hash differs from the previous manifest blocks the replacement if the new set needs its path, or if it would otherwise stay behind as a stale file in the set. The export names the file.
  - **Version 1 files:** v1 records only have a geometry digest for the CF2, so a v1 file without a hash is treated as changed unless the designer confirms it.
- **Stale locks:** a lock whose process isn't running on this workstation, or one from another workstation older than 10 minutes, can be broken only by the designer, after a prompt.

**Publishing the main destination** (CF2, DXF and the manifest):

1. Stage every new file in a hidden `.dieline-staging/<transaction>/` folder in the destination root, on the same volume so moves are atomic renames. Validate the staged files.
2. Write the **transaction journal** `<stem>.dieline.pending.json` and flush it to disk. It records:
   - the transaction id
   - each planned move: source, destination and hash
   - the old files, with their hashes and archive paths
3. Copy the old manifest to `superseded/<stem> <time>/`. The live old manifest stays in place.
4. Move the old part files to `superseded/`, then move the staged files into place. Each move is recorded in the journal before and after it happens.
5. Replace the manifest atomically (`os.replace`). The new manifest carries the transaction id. **This is the commit point.**
6. Delete the journal and the empty staging folder.

**Readers:**

- Anything in the app that reads a set (`job.open`, the next export) reads the manifest, then checks for a journal with a different transaction id, then verifies the files' hashes.
- A journal or a hash mismatch means the set is **incomplete**. The app says so instead of showing it as current.

**Recovery** runs on the next export to that stem, or at app start, when a journal is found:

- **The live manifest has the journal's transaction id**, so the transaction committed. Clean up only: delete the journal and any staging leftovers.
- **The live manifest is the old one**, so the transaction didn't commit. Roll back:
  - Each move is located by hash, at either its staged or its installed path, so a move that finished just before its journal update is still found.
  - New files installed at their destinations are removed. That includes paths that had no old file.
  - Archived old files are moved back.

Replay is idempotent, so a crash during recovery is recovered the same way: the journal is deleted before the staging folder, so a replay never sees a half-removed staging. The designer is told what happened.

**Finished first:** recovery first asks whether the transaction committed (the live manifest carries its id, or for a sample table, says that set is published); then only the journal and staging folder go. Otherwise a sample-table transaction whose every file has moved (judged by the files) is rolled forward, and anything else is undone.

**Nothing in a journal or manifest is trusted.** Every move must be one this transaction could make: from its own staging folder to this job's file names in this folder, or from this job's files in a locked root to its archive folder, each with a hash. A file that is neither where the move started nor where it ended (with that hash) is reported, not guessed at. No folder on the way to a file recovery touches may be a link or junction, and a main journal's waiting sample set is removed only when that table is locked. Either way the journal is kept and that name is blocked until the designer has checked the folder. Staging folders no journal names are swept only while every journal there can be read.

**The sample-table folder** is a second destination whose reader ignores manifests. It's published only after the main destination commits, as its own transaction with the same journal rules:

- Files wait in a hidden pending folder **beside** the sample-table folder, never inside it (so a drive root can't be the sample-table folder). They're staged there before the main destination commits, so a committed pending set always has its files. Replaced sample-table files are archived there too, not in a `superseded/` inside the table's folder.
- **Ownership at a shared table:** the pending folder keeps an index of which job's manifest claims each file, so a job from another export folder can't take it. Claims are written before the main destination commits, so there's no moment when a committed set's names belong to nobody; a claim counts only while the claiming manifest lists the file. An index that can't be read (or has an entry this app doesn't write) blocks exports to that table. The index is consulted whatever role another export gives that folder (its own export folder included), and a claim whose job record can't be read right now still stands.
- **A waiting set names its owner:** each staged sample-table set holds a small `.owner.json` (its job record and main transaction). Cleanup after an undone export, dropping a superseded waiting set, and a pickup all check it, so no job's waiting files can be removed or moved in for another.
- **A record can't make a folder a sample table:** a sample-table folder (and the files a record says are there) is used only if that table's index records the job. A version 1 record's DXF counts only in today's sample-table folder.
- **What's at the table:** while a set waits, the manifest records the old files still at the table (with their hashes); a newer export replaces those.
- The manifest records the pending set (its transaction, waiting files and the old files it replaces) and is updated when the files move in. A newer export of the job drops a set still waiting.
- **One file, no old set:** a single rename, so the reader sees it complete or not at all.
- **Several files, or a replacement:** these need a **coordinated pickup**. The export bar asks the designer to confirm the table won't pick up during the handoff, and nothing moves until they do. The main destination is already committed, so the export reports the sample-table set as pending until then.

### Reopening

There are two operations:

- **Open as exported:** shows exactly what was exported, from the snapshot in the job record. It never touches stored source paths.
- **Regenerate with current rules:** rebuilds from the inputs with today's board values and rules, and shows per-part differences in shape, dimensions, operations, quantity and bindings.

If regenerating fails, the as-exported snapshot is still viewable.

**What the record stores**

- the inputs, with defaults resolved
- the board, rule, geometry and schema versions
- stable ids
- expressions with their resolved values
- normalised artwork geometry: agent sketches, imported files (with path and hash), and generated library and stencil paths. A moved or changed source file can't change the design.
- the per-part as-exported geometry

**Artwork sources:** an imported artwork's source path is confined to the reference folders and read only when the designer asks to refresh it or compare it with its source.

**Version 1 records** still open, as single-part designs with today's regenerate-only behaviour, and are labelled that way.

## Phases

1. **Foundations**
   - the material arrangement and the contact and disposition rules (design and reconstructed per-output), with arc invariants
   - face maps for the four standards per the geometry contract: roles, frames, assembled centres, reference lines, keep-out zones, overlap bands, extended-lug faces
   - oriented hinges with bend offsets, and the hinge graph
   - the multi-part design model with groups, fit links (the checks themselves come in phase 3), the preview scene, and one-part naming unchanged
   - `CheckResult` statuses and `overall()`
   - the unified mutation pipeline with turn binding, atomic calls, retry ids and snapshot-bound export; and `list_faces`
   - manifest exports: path ownership, stem locks, the transaction journal with roll-forward and roll-back, and the sample-table pickup
   - the selector test matrix: both panel orders, both joints, L = W, HSC_GL tray and lid, tube with no flaps, extended lug, mixed flap styles
   - CF2 full circles as two arcs, and DXF cutting order
2. **Features on standards** (built, 2026-11-26; the import and cut fixture is still to do)
   - handholes, hand flaps, vents, windows, extra scores, notches
   - perforations and tear strips, with rule specs and the capability table
   - **before** perf, reverse score or partial cut can export: an import and cut fixture covering obround and circular arcs, mirrored arcs, perf, reverse score and mixed pointage, checked in ArtiosCAD and on the sample table. Until then those targets are blocked.
   - feature checks, machine capability fields, and the agent tools
3. **Partitions** (built 2026-11-26)
   - the crossing model, all four input modes and `choose_layout`
   - the checks, the fit and stack checks plus the tools that set fit links (the links themselves exist from phase 1), and part tabs
4. **Artwork** (built 2026-11-26)
   - the library, stencil text, sketch and import (with biarc fitting)
   - islands and bridges, the aggregated checks, and the suspicious-artwork flags
5. **Panel builder**
   1. pads and inserts (no folds)
   2. wraps and folders (wrap chains with layer stacks), checked against a scored wrap reference
   3. trays: rollovers, dust flaps, lock tabs, the assembly sweep. Checked against `DC_TRAY_2_ROLLOVERS` before it's offered.
   4. overrides on standards
   5. `glue_flap`, then `wing_lock`, each against a reference (`MEATBOX_WL_DF_1SL`)
6. **Calibration:** compare partitions and each builder pattern with real R&L designs, and replace the provisional values.

Each phase gets the two-reviewer review (Sol 6.2 and Gemini Neon) before it's committed.

## Phase 2 as built

Readings taken where the spec left a choice, each the safer one. Change them here first if the shop decides otherwise.

**Placement**

- `datum: usable` is the face less its glue landing zone. A spread's insets run from that usable edge, so vents on P1 stay out of the glue zone.
- A pattern's `centre` is the pattern box's centre; a single copy's is the shape's own datum.
- Mirroring: the placement as given applies to the first face of each pair in the selector's order, and the second face gets it mirrored left for right. A multi-face selection that can't be paired completely (P1 and P2, or anything with the glue lug) is refused unless it says `mirror: none` or gives `pairs`.
- A face named by id (`P1`) records its role when the feature is set; if a later order swap changes that role, the feature is stale. A role selector (`end_panels`) follows the role.

**Features**

- Defaults carry their unit (`3 5/8 in`), so they mean the same on a millimetre part.
- The form's Generate (and its Replace) starts a clean standard part: style, size, units, board and options only. Features, artwork and acknowledgements never carry over from the design it replaces.
- Hand flaps turn only in 90° steps; the turn is applied to the profile's size and the hinge is then named in the face frame. Obround and circle flaps hinge on a long side only, with a secant hinge inset to the next 1/32 above `r(1 − cos 35°)` (1/8 on the File_Box_1 obround). Distances measure the opening, not the outer crease. A D-shape's default corners leave a straight start at the hinge, so a relief nick continues the cut in line.
- Routes (scores, perforations, tear strips) are straight, from the first face's edge to the last face's, across faces joined by hinges, or over a slot of up to 1/2 in with `slots: jump`. Starting or stopping inside a face isn't offered yet.
- Tear strips: rails are perforations (the File_Box_1 rule) or cut-scores. `glue_joint: cross` (the default) carries a P1-to-P4 strip over the glue lug, and a P4-to-P1 one too (the lug goes first; the reference line is still the route's first listed face). Edge tabs need the strip to start at a cut edge; each tab cut runs in from its own rail's start, so a start on the lug's slanted taper still meets both rails. A thumb hole's tab is checked at the hole's centre. The strip's pointage applies to its tab cuts too. Width 3/4 in and tab 1/2 in are provisional.
- Notches go on straight cut edges, at least 1/8 in from the ends of the straight cut and from the face's corners, and are part of the outline operation. A half-circle notch is half its width deep.
- Ellipse windows are four-centre ovals (true arcs, tangent at the joins); the summary gives the largest gap from the ellipse.
- A feature placed wholly inside another's opening leaves an island: drawn, export blocked, as the contact table says for islands.

**Checks**

- The combined ligament is the board between a handhole (or hand flap) and the nearest other cut, perforation or score above it, against the carry-web limit.
- An overlap band's matching cut-out is any feature on the opposite flap.
- An extra score in the glue zone warns only when the joint is outside (the lug lands on P1's outside); cuts and perforations there always warn.
- A tear strip's pull tab is in the glue zone only when it's inside the landing both ways (a flap's landing can be short). A strip that wraps P1 and P4 without the lug warns at the joint whichever end it starts from.
- Edge notches count in the near-miss check against holes, vents, windows, hand flaps and other notches on their face: applying them keeps only 1/8 in between notches on one edge, and notches on two edges can meet. A notch pattern's pitch must be above zero.
- The slotter's die-cutting isn't confirmed, so a part with a die-cut feature isn't listed for it. An extra score alone doesn't count as die-cut.

**Exports**

- In the expanded sample-table perforation, a tie is centred on every crease the path crosses and on a crease it ends on; a cut piece trimmed shorter than half a tie is dropped. The expansion must pass the topology rules (a cut on a crease is invalid), and the target stays blocked as unverified.
- The dashed cuts keep the perforation's contacts with named cuts: a tear strip's tab cuts end on the rails' cuts, and the rails' cuts may end on the tabs (never on a crease). A curved perforation (artwork) becomes dashed arcs, stationed on along the operation; a run with no recorded station continues from the one before it.
- A feature can carry its own pointage; the CF2 writer then writes it per operation, and mixed pointage blocks CF2.

## Partitions as built

Where this spec left a choice open, phase 3 took the safest reading:

- **Slots belong to the outline.** A slot is a boundary operation, like an edge notch: the piece's outline stays one closed cut that runs round each slot, so its mouth joins the outline by construction. A slot drawn as its own cut ending on an unbroken edge would be a declared contact, but its slug would stay as a separate piece of board (a test shows it).
- **Round bottoms are compensated.** The slot is drawn deeper by what its round bottom loses, so its *usable* depth (as wide as the mating board) is still H/2 + 1/32. The interlock check uses usable depths.
- **Shallow slots are square.** A slot gets a round bottom only when it's at least twice the minimum bend radius wide *and* deep enough for the full radius (drawn depth ≥ half its width); otherwise the radius would run its walls back past the piece's edge. Such a slot is square-bottomed, with an info check (`partition_slot_bottom`).
- **Unequal heights** (pieces mode): slot depths come from the shorter piece's height h. The bottom-slotted piece gets h/2 + 1/32, the top-slotted one H − h/2 + 1/32, so they still overlap by 1/16.
- **Lead-ins** shrink so neighbouring mouths and the piece ends keep a rim of max(1/4", 2 × CAL), and to at most half the slot's straight wall. If even a square mouth can't keep the rim, it warns.
- **Facing.** Pieces are drawn from their start end, nearest the box's first wall. Pieces mode says which end its slot positions are measured from (`from: start | end`).
- **Parts per slot plan.** Pieces on one axis differ only when their boards do (`boards_long` / `boards_short` lists one per piece). The first plan keeps the role `long` (or `short`), the others are `long_2`, `long_3`, … so `replace_set` keeps matching the main pieces.
- **A part's parameters** are the whole set's grid plus its role, so any part rebuilds alone with today's rules. `update_part` can only relabel a piece; the set changes through `create_partition` with `replace_set`.
- **Plain strips (1 × n)** come from `box_cells` or `cell_sizes` with `confirm_degenerate`. Pieces mode refuses them: strips have no slot positions to place them by.
- **Height.** When the box's inside depth isn't known, the height check is `unavailable`.
- **Product mode.** Piece height defaults to the item height. A layout is listed when it has fewer spare cells than one row; `1 × n` layouts come last and need `confirm_degenerate`. Up to six are offered, by spare cells, then estimated sheet usage. "No slots" (`degenerate`) is decided by the set's own grid: an air-cell ring turns a `1 × n` item layout into a slotted grid, which the card shows (`grid_cells`). A `boards_long` / `boards_short` list only rules out the grids it doesn't fit (bad board codes are refused). The maximum box's longer side bounds the set's long axis, whichever way round it's named.
- **Choosing a layout into a box** takes that box's inside depth for the height check, even when the candidates were offered for another box.
- **Candidates** are tied to the whole design's id and revision (stricter than the container part's revision), the board values of every board in them, and `partition-rules-1`. Their ids (`c1`, `c2`, …) are never reused in a chat. Each designer message gets an id (`m<turn>`), shown to the model as `[message m3]`. `choose_layout` needs a message sent after the cards that names the candidate's id as a word. A card click commits as the designer's own change (`layout.choose`), and the model is told on its next turn.
- **Default action.** `create_partition` adds the set when `contained_in` is given and otherwise replaces the design. `replace_set` takes the box size from the set's existing fit link when no `contained_in` is given.
- **Fit.** A standard's outside is inside + 2 × CAL. A set's outside is its long and short piece lengths and its height. A stack is its widest tier, with the tiers' heights plus pads × pad CAL. Only "taller than the inside depth" warns upwards. A set that doesn't go in warns rather than fails (draw and warn). A container must be a box part. A group as a container, or a link to one piece of a set, is `unavailable`.
- **Stacks** keep their pads in the group (`extra: {pads, pad_board}`); pads become parts with the panel builder. A stack's height is reported once, by `stack_<id>` (no separate `fit_<link>_depth`). A stack with pads but no known pad board, or one that contains itself, makes its fit `unavailable`; a job file with a group cycle is refused when opened.
- **Exports.** A partition piece is exported with its set, from the design the engine holds: the RPC refuses one piece's parameters alone (they'd skip the set's crossing, interlock and fit checks), and a held design with a piece in no partition set fails `partition_<part>_set`, which blocks export.
- **Check results** take their category from the id's prefix (`partition_` → partition, `fit_` and `stack_` → fit), so the checks panel files them with the actionable checks.

## Artwork as built

Where this spec left a choice open, phase 4 took the safest reading:

**Sources**

- **Library:** `circle` (diameter), `ellipse` (fitted with biarcs, not the four-centre oval the window uses), `rounded_rect`, `polygon` (sides, across its corners), `star` (points, inner ratio), `heart` (two lobes and two straight sides), `smiley` (the eyes and mouth are holes; its face outline is `none` or `crease`, never cut, which would make the eyes islands) and `arrow` (pointing right; turn it with `rotation_deg`).
- **Lettering** is the app's own straight-line skeleton font: capitals 6 units tall, stroke 1 unit. Stencil text cuts **every** junction back to a break, so no counter is ever closed. A break is max(0.6 units, the minimum bridge, max(3/16", 1.5 × CAL)); letter spacing is at least the thin-web limit. Text too small to keep its breaks is refused with the height it needs. Lowercase is drawn as capitals.
- **Sketch:** "SVG-style" is read literally: the canvas is SVG coordinates (x right, y **down**), so an agent's SVG habits draw the right way up. Only M L Q C A Z (absolute or relative); H V S T are refused in a sketch but read in imported SVG. The datum is the centre of what's drawn, and the width or height given is of what's drawn, not of the canvas.
- **Arc radius correction:** refused above 1% in a sketch; reported, not refused, in an imported SVG (a file from a drawing program is taken as written).
- **Imports** are approved by the **designer** on the chat card, never by the agent; an import that can't be placed (outside its face, across a score, an open cut path) is refused to the agent at once, so no card is shown that would fail when clicked; with unsupported content the button says it imports without it. A waiting import expires when the design changes. The part stores the parsed geometry in the file's own units with its name, path and SHA-256, what wasn't read and the notes, so the file is never read again.
- **SVG:** px and unitless files need a size (px counts as unitless). Hidden elements aren't imported and are listed, so the import waits; `<style>` sheets aren't read (noted); clipping and masks are listed and ignored; stroke-only shapes are cut on their centre line (noted); a filled path whose inner outline runs the same way as its outer one under `nonzero` is noted (it looks solid but cuts as a ring and a middle).
- **DXF:** colour 1 means cut and 2 crease by default, as the shop's files carry them. Layers that are off aren't imported. A SPLINE given only by fit points and an arrayed INSERT are unsupported. Anything off the drawing plane (any z, any extrusion not along Z) refuses the whole import. Separate entities join into a path only where exactly two ends coincide (within a ten-millionth of the drawing). A binary DXF is refused.
- **Limits:** 5 MB per file, 20,000 DXF entities (blocks expanded), 20,000 SVG elements walked (`<use>` expanded, so a small file of nested uses is refused, not run), 5,000 SVG shapes, 5,000 segments and 600 outlines after fitting. A limit reached inside a DXF block refuses the import; it's never listed and skipped. A filled path with over 200 closed outlines isn't compared pairwise for the fill-rule note; it gets a note saying the fill rule wasn't checked.
- **SVG size and paths:** when width, height and viewBox disagree in shape, the drawing is scaled as SVG fits it (`preserveAspectRatio`: the smaller scale for meet, the default; the larger for slice), with a note; `none` (a stretched drawing) is refused. Arc flags are read by SVG's grammar, one character each, so compact flags (`0 0150 50`, `1020 0`) read as written. Non-finite sizes, viewBoxes and points are refused.
- **Fitting:** a fitted arc flatter than a 10,000" radius (or bowing less than a ten-millionth of an inch) is drawn as a line, so no arc centre lands far outside the drawing; the deviation check still decides whether the fit is within tolerance. A curve that comes back to where it started partway (a figure-8) is split and fitted, not dropped; only a piece that never leaves its point is empty.
- **DXF numbers:** a value that isn't a finite number (or a whole number where one is needed: colours, flags, degrees) refuses the import. An ellipse with no size (a zero axis or a ratio outside 0–1) is listed as unreadable.

**Placement and rules**

- Artwork is one copy on one face: a selector must resolve to one face, patterns and mirroring aren't offered (add another item). Partition pieces refuse artwork, as they refuse features.
- Each outline's rule comes from `loop_rules` (by its number in the read-back), then the item's `rule`, then the source's own (a DXF crease, the smiley's outline), then cut (stroke text: crease). Cut, perforation and partial cut need a closed outline.
- `closed: true` snaps a path's ends within 0.005" at final size; a gap at an arc closes with a short line, so the arc keeps its radius.

**Islands**

- Worked out per face from every closed through-cut loop: artwork cut outlines and the features' holes (handholes, vents, windows), nested even-odd with the outline as depth 0. A loop at an even depth of 2 or more bounds an island. Other cuts (hand-flap cuts, scores) are obstacles for bridges, not loops.
- Islands are numbered per artwork (`i1`, `i2`, ...), and `islands` sets a fix for all of them or per id. An island whose loop or hole is a feature's, or another artwork's, can only be dropped out.
- **Partial cut** turns the hole's outer loop (the one between the board and the hole round the island) into a partial cut: the ring stays, scored, and the island becomes a plain hole that drops out. No board is left unheld.
- **Bridges** are straight corridors exactly the bridge width (`bridge_width` may only widen it). They're tried across the flutes first (vertical flutes: along the flat x), then along them, then diagonally, from points inside the island's board; each side must leave the island and then cross the hole's outer loop at 30° or more, with nothing else in the corridor. The second corridor prefers the opposite direction. The two outlines become the pieces of the hole between the corridors, and the whole design is validated again. **Stencil** is the same construction, reported as stencil breaks.
- Every island is worked out, however many: each pass fixes every island whose loop and hole no other fix in that pass has redrawn (a partial cut ends the pass, since it changes the nesting inside it). The passes are bounded (8 + 2 per loop on the face); if the bound is ever reached, every island still unresolved fails. Nothing is dropped silently.
- If artwork breaks the design together with other artwork and is set aside (stale), the islands are worked out again for exactly what's left, so the records always match what's drawn.
- The topology's `material` check leaves to the artwork check only the exact islands the artwork recorded as unhandled (which the artwork check fails) or as an accepted drop-out: the island's board must be bounded by that recorded loop. A split, an island the artwork says it bridged, stencilled or cut round, and any island nothing recorded (a feature's hole inside another feature's, inside artwork or not) stay a `material` fail and block export.

**Checks**

- Bend radius: arcs drawn as such and the source curvature of fitted curves (fitted arcs aren't measured); one place per outline.
- Acute corners: the angle between the two segments at a true corner (the tangent turns more than 1°), on either side.
- Short straights: source lines and bridge sides with a corner at both ends.
- Thin webs: between different artwork outlines (not a stencil letter's own pieces, which are bridges, and not across a bridge) and from artwork to the features' cuts on its face.
- Locations are given as the artwork id and face coordinates seen from outside.

**Suspicious artwork**

- Long straight: 1" or longer, within 3° of parallel and within 1" of any edge or crease of its face. Consecutive collinear pieces (within 1/2° and 0.001" of the first piece's line) count as one straight, so a straight drawn in pieces is still flagged; a many-sided polygon or a zigzag isn't.
- Slot-like: a closed cut no more than 1/2" wide and at least 3:1, mostly straight, within 1" of the face's edge, measured by its narrowest enclosing rectangle at any angle (a rotated slot is flagged). Standards have no mates yet.
- Span: over 80% of the face's width or height.
- Rule: a crease or perforation whose **path length** is over half what it could fold across. A single straight is measured against the face's chord in its own direction (a crease up a shallow panel needs less than one along it, and the limit changes smoothly as it turns); anything else (a curve, a closed outline) against the face's shorter side, so a large creased circle is flagged too.
- One flag per artwork and kind. Its acknowledgement key is the artwork id, the kind and a fingerprint of everything that decides what's cut: every segment (rule, ends, centre, direction, full circle), each outline's rule spec (pointage), the face and its frame on the blank (size, origin, axes, outline). So moving, resizing, reversing an arc or resizing the box (even when the artwork's flat coordinates stay put) asks again. Acknowledgements are stored on the part (`acknowledged`). The store refuses any chat change that adds one (dry runs too, and through any tool); the RPC needs the design id and revision on screen and accepts only keys the checks offer.

**UI and picture**

- The preview redraws agent sketches over the dieline in an accent colour with their own line style, in a dashed frame labelled "Agent-drawn sketch a1" (bounded by each arc's ends and the extremes it actually sweeps, never its whole circle); the header shows an "Agent-drawn artwork" pill; the features list shows artwork with an "agent-drawn" badge. Flag rows in the checks panel carry an Acknowledge button.
- The agent's picture shades the board artwork removes, frames agent-drawn sketches with "verify before cutting", and circles islands nothing holds.

## Open questions

- R&L's real partition allowances: slot width and depth, clearance and height. Needs CF2/DXF exports of two or three real sets. Measured so far (2026-11-26), from one sample-table DXF of a 12-cell (4 × 3) set: pieces about 10" and 7" long and 2 3/4" tall, drawn two-up on shared cut lines; slots 3/16" wide at an equal pitch, square bottomed, no lead-ins. On both piece types the slots run about two-thirds of the height and end in a barbed lock, so it's a locking variant, not a plain half slot. Its board and box sizes aren't known, so nothing was calibrated from it; a test builds a comparable set and checks the pitch and that the width is within 1/32 of 3/16. The other sample DXF named for this is an RSC, not a partition.
- The die maker's minimum bend radius per rule, minimum segment and web, ejection limits, and how they handle narrow round-bottom slots. The same for artwork: bridge width, stencil breaks, and the suspicious-artwork thresholds.
- CF2 codes for perforation (with its cut/tie), reverse crease and partial cut, plus mixed pointage, and AOLCUT's DXF colours for them. The shop's File_Box_1 uses a dedicated 3/16 × 3/16 perf rule.
- How R&L draws hand flaps and angled hand flaps: relief nicks, double crease, angles.
- The handhole carry-web and side-web thresholds the shop uses.
- How the sample table's operator picks up files, so replacing a set can be made safe there.
- Whether ArtiosCAD's HSC_GL drawing (its script ends with a vertical mirror) is outside up, and which side the shop's dies are drawn from.
- Whether partitions are ever nested with the carton on one sheet. It's in the shop's files, but not in scope now.
- Trim and gripper margins, and the flute and feed rules, per machine.
