// Features in the preview: the engine's tagged scene, feature links from the chat
// (`feature:<part_id>/<feature_id>`) and which checks need the designer's attention.
// docs/DESIGN-SYSTEM.md, "UI".

import { type Primitive, toSvgPath } from "./cf2.ts";
import { type BoxParams, type CheckResult, type Design, isPartitionParams, type SceneSegment } from "./protocol.ts";

const MM_PER_IN = 25.4;

/** A box in the preview's millimetres (the CF2 doc's frame), y up. */
export interface MmBox {
  xmin: number;
  ymin: number;
  xmax: number;
  ymax: number;
}

/** "feature:p1/f2" (or an artwork's "feature:p1/a1") → the part and feature it points at. */
export function parseFeatureHref(href: string | undefined | null): { partId: string; featureId: string } | null {
  const m = /^feature:(p\d+)\/([fa]\d+)$/.exec((href ?? "").trim());
  return m ? { partId: m[1], featureId: m[2] } : null;
}

/** react-markdown drops unknown URL schemes; keep feature links, and leave the rest to the default rules. */
export function keepFeatureLinks(url: string, fallback: (url: string) => string): string {
  return parseFeatureHref(url) ? url : fallback(url);
}

function scale(units: string | undefined): number {
  return units === "mm" ? 1 : MM_PER_IN;
}

/** A scene segment as a preview primitive (millimetres, like the parsed CF2). */
export function sceneToPrimitive(s: SceneSegment, units?: string): Primitive {
  const k = scale(units);
  if (!s.arc) {
    return { kind: "line", code: 0, type: "other", x1: s.start[0] * k, y1: s.start[1] * k, x2: s.end[0] * k, y2: s.end[1] * k };
  }
  const end = s.full_circle ? s.start : (s.end as [number, number]);
  return {
    kind: "arc",
    code: 0,
    type: "other",
    sx: s.start[0] * k,
    sy: s.start[1] * k,
    ex: end[0] * k,
    ey: end[1] * k,
    cx: (s.center as [number, number])[0] * k,
    cy: (s.center as [number, number])[1] * k,
    cw: Boolean(s.clockwise),
  };
}

/** SVG paths of one feature's segments (or every stale ghost when featureId is null and stale is set). */
export function featurePaths(
  scene: SceneSegment[] | undefined,
  pick: { featureId?: string | null; stale?: boolean },
  units?: string,
): string[] {
  return (scene ?? [])
    .filter((s) =>
      pick.stale ? Boolean(s.stale) : pick.featureId ? s.feature_id === pick.featureId && !s.stale : false,
    )
    .map((s) => toSvgPath(sceneToPrimitive(s, units)));
}

/** A region the engine gave (design units, flat) in the preview's millimetres. */
export function regionToMm(region: readonly number[] | undefined, units?: string): MmBox | null {
  if (!region || region.length !== 4 || !region.every(Number.isFinite)) return null;
  const k = scale(units);
  return { xmin: region[0] * k, ymin: region[1] * k, xmax: region[2] * k, ymax: region[3] * k };
}

/** Checks the designer should act on (geometry, features, folding, partitions, artwork, fit), kept
 *  apart from calibration, board and machine notices. */
const ACTIONABLE = new Set(["geometry", "feature", "fold", "partition", "artwork", "fit"]);
export function isActionable(c: CheckResult): boolean {
  return (
    c.category !== undefined &&
    ACTIONABLE.has(c.category) &&
    (c.status === "warn" || c.status === "fail" || c.status === "stale" || c.status === "unavailable")
  );
}

/** Generate in the form replaces the whole design: ask first when that loses more than one plain
 *  standard part (other parts, or any part's features). */
export function replaceLoses(design: Design | null | undefined): string | null {
  if (!design) return null;
  const parts = design.parts?.length ? design.parts : [design];
  const features = parts.reduce(
    (n, p) =>
      n + (isPartitionParams(p.params) ? 0 : (p.params.features?.length ?? 0) + (p.params.artwork?.length ?? 0)),
    0,
  );
  const others = parts.length - 1;
  // A partition set (or any group) isn't one plain standard part either.
  const set = parts.some((p) => isPartitionParams(p.params)) || (design.groups?.length ?? 0) > 0;
  if (!features && !others && !set) return null;
  const lost = [
    features ? `${features} feature${features > 1 ? "s" : ""}` : "",
    others ? `${others} other part${others > 1 ? "s" : ""}` : "",
    set ? "its partition set" : "",
  ].filter(Boolean);
  return `Replace the current design? Its features, overrides and extra parts will be lost (${lost.join(", ")}).`;
}

/** The form's Replace starts a clean standard part: only the plain standard parameters, never the
 *  design's features, artwork or acknowledgements (nor anything else the form doesn't edit). */
export function plainStandard(params: BoxParams, boardIds: readonly string[] = []): BoxParams {
  const normalized = params.board.trim().toUpperCase().replace(/\s+/g, " ");
  const out: BoxParams = {
    style: params.style,
    length: params.length,
    width: params.width,
    depth: params.depth,
    units: params.units,
    board: boardIds.find((b) => b === normalized) ?? params.board,
  };
  if (params.options) out.options = { ...params.options };
  return out;
}

// A feature link clicked in the chat asks the preview to show that feature.
const EVENT = "dieline:focus-feature";
export interface FocusFeature {
  partId: string;
  featureId: string;
}

export function focusFeature(target: FocusFeature): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent<FocusFeature>(EVENT, { detail: target }));
}

export function onFocusFeature(cb: (target: FocusFeature) => void): () => void {
  if (typeof window === "undefined") return () => {};
  const listener = (e: Event) => cb((e as CustomEvent<FocusFeature>).detail);
  window.addEventListener(EVENT, listener);
  return () => window.removeEventListener(EVENT, listener);
}

// ------------------------------------------------------------------ artwork

/** Agent-drawn artwork, drawn apart in the preview: its paths keep their line style, and a dashed
 *  frame round each sketch is labelled "Agent-drawn sketch". */
export interface SketchOverlay {
  paths: { d: string; type: "cut" | "crease" | "perforation" | "score" | "annotation" }[];
  frames: { featureId: string; box: MmBox }[];
}

export function agentSketches(scene: SceneSegment[] | undefined, units?: string): SketchOverlay {
  const out: SketchOverlay = { paths: [], frames: [] };
  const boxes = new Map<string, MmBox>();
  for (const s of scene ?? []) {
    if (s.provenance !== "agent sketch" || s.stale || !s.feature_id) continue;
    const prim = sceneToPrimitive(s, units);
    out.paths.push({ d: toSvgPath(prim), type: s.type });
    const k = scale(units);
    const pts: [number, number][] = [s.start, s.end];
    if (s.arc && s.center) {
      const [cx, cy] = s.center;
      const r = Math.hypot(s.start[0] - cx, s.start[1] - cy);
      const a = Math.atan2(s.start[1] - cy, s.start[0] - cx);
      const b = Math.atan2(s.end[1] - cy, s.end[0] - cx);
      const turn = 2 * Math.PI;
      const along = (angle: number) => (((s.clockwise ? a - angle : angle - a) % turn) + turn) % turn;
      const sweep = s.full_circle ? turn : along(b);
      const extrema: [number, number][] = [[cx + r, cy], [cx, cy + r], [cx - r, cy], [cx, cy - r]];
      extrema.forEach((point, i) => {
        if (along(i * Math.PI / 2) <= sweep) pts.push(point);
      });
    }
    const b = boxes.get(s.feature_id) ?? { xmin: Infinity, ymin: Infinity, xmax: -Infinity, ymax: -Infinity };
    for (const [x, y] of pts) {
      b.xmin = Math.min(b.xmin, x * k);
      b.ymin = Math.min(b.ymin, y * k);
      b.xmax = Math.max(b.xmax, x * k);
      b.ymax = Math.max(b.ymax, y * k);
    }
    boxes.set(s.feature_id, b);
  }
  for (const [featureId, box] of boxes) out.frames.push({ featureId, box });
  return out;
}

/** Whether any part of the design carries agent-drawn artwork (the header's info pill). */
export function hasAgentDrawn(design: Design | null | undefined): boolean {
  if (!design) return false;
  const parts = design.parts?.length ? design.parts : [design];
  return parts.some((p) => (p.features ?? []).some((f) => f.agent_drawn && f.status === "ok"));
}

/** Suspicious-artwork flags waiting for the designer's acknowledgement. */
export function pendingFlags(checks: CheckResult[]): CheckResult[] {
  return checks.filter((c) => Boolean(c.ack) && !c.acknowledged && c.status === "fail");
}
