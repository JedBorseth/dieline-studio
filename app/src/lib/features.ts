// Features in the preview: the engine's tagged scene, feature links from the chat
// (`feature:<part_id>/<feature_id>`) and which checks need the designer's attention.
// docs/DESIGN-SYSTEM.md, "UI".

import { type Primitive, toSvgPath } from "./cf2.ts";
import type { CheckResult, Design, SceneSegment } from "./protocol.ts";

const MM_PER_IN = 25.4;

/** A box in the preview's millimetres (the CF2 doc's frame), y up. */
export interface MmBox {
  xmin: number;
  ymin: number;
  xmax: number;
  ymax: number;
}

/** "feature:p1/f2" → the part and feature it points at. */
export function parseFeatureHref(href: string | undefined | null): { partId: string; featureId: string } | null {
  const m = /^feature:(p\d+)\/(f\d+)$/.exec((href ?? "").trim());
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
  const features = parts.reduce((n, p) => n + (p.params.features?.length ?? 0), 0);
  const others = parts.length - 1;
  if (!features && !others) return null;
  const lost = [
    features ? `${features} feature${features > 1 ? "s" : ""}` : "",
    others ? `${others} other part${others > 1 ? "s" : ""}` : "",
  ].filter(Boolean);
  return `Replace the current design? Its features, overrides and extra parts will be lost (${lost.join(", ")}).`;
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
