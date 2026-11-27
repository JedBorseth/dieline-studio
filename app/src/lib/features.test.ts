import assert from "node:assert/strict";
import { test } from "node:test";

import {
  agentSketches,
  featurePaths,
  hasAgentDrawn,
  isActionable,
  pendingFlags,
  plainStandard,
  keepFeatureLinks,
  parseFeatureHref,
  regionToMm,
  replaceLoses,
  sceneToPrimitive,
  segmentExtent,
} from "./features.ts";
import type { Design, SceneSegment } from "./protocol.ts";

test("feature links name a part and a feature", () => {
  assert.deepEqual(parseFeatureHref("feature:p2/f13"), { partId: "p2", featureId: "f13" });
  assert.equal(parseFeatureHref("feature:p2"), null);
  assert.equal(parseFeatureHref("https://example.com"), null);
  assert.equal(parseFeatureHref(undefined), null);
  // react-markdown would drop the unknown scheme; feature links are kept, others go to its own rules.
  assert.equal(keepFeatureLinks("feature:p1/f1", () => ""), "feature:p1/f1");
  assert.equal(keepFeatureLinks("javascript:alert(1)", () => ""), "");
});

const scene: SceneSegment[] = [
  { type: "cut", op: "outline", kind: "outline", provenance: "structure", arc: false, start: [0, 0], end: [1, 0] },
  {
    type: "cut", op: "f1.1", kind: "hole", provenance: "feature", feature_id: "f1", face_id: "P1",
    arc: true, start: [2, 1], end: [2, 1], center: [1.5, 1], clockwise: false, full_circle: true,
  },
  {
    type: "annotation", op: null, kind: null, provenance: "feature", feature_id: "f2", face_id: "P2", stale: true,
    arc: false, start: [0, 2], end: [1, 2],
  },
];

test("a feature's scene segments become highlight paths in millimetres", () => {
  const paths = featurePaths(scene, { featureId: "f1" }, "in");
  assert.equal(paths.length, 1);
  assert.match(paths[0], /^M50\.8 -25\.4A12\.7\d* 12\.7\d* .*A/); // inches to mm, y negated, a full circle in two arcs
  assert.equal(featurePaths(scene, { featureId: "f2" }, "in").length, 0); // a stale ghost isn't the feature
  assert.equal(featurePaths(scene, { stale: true }, "in").length, 1);
  const p = sceneToPrimitive(scene[0], "mm");
  assert.deepEqual(p.kind === "line" && [p.x2, p.y2], [1, 0]);
});

test("regions zoom in millimetres", () => {
  assert.deepEqual(regionToMm([1, 2, 4, 4], "in"), { xmin: 25.4, ymin: 50.8, xmax: 101.6, ymax: 101.6 });
  assert.equal(regionToMm(undefined), null);
  assert.equal(regionToMm([1, 2, 3]), null);
});

test("actionable checks are kept apart from notices", () => {
  assert.equal(isActionable({ id: "a", status: "warn", message: "", category: "feature" }), true);
  assert.equal(isActionable({ id: "b", status: "stale", message: "", category: "feature" }), true);
  assert.equal(isActionable({ id: "c", status: "warn", message: "", category: "calibration" }), false);
  assert.equal(isActionable({ id: "d", status: "pass", message: "", category: "geometry" }), false);
  assert.equal(isActionable({ id: "e", status: "info", message: "", category: "feature" }), false);
  assert.equal(isActionable({ id: "f", status: "warn", message: "" }), false);
});

test("Generate asks before replacing features or extra parts", () => {
  const base = {
    params: { style: "reg-glio-nx", length: 14, width: 10, depth: 12, units: "in", board: "32 B KR" },
  } as unknown as Design;
  assert.equal(replaceLoses(null), null);
  assert.equal(replaceLoses(base), null);
  const withFeatures = { ...base, params: { ...base.params, features: [{ id: "f1", kind: "handhole" }] } } as Design;
  assert.match(replaceLoses(withFeatures) ?? "", /^Replace the current design\? Its features, overrides and extra parts will be lost \(1 feature\)/);
  const twoParts = { ...base, parts: [{ ...base, part_id: "p1" }, { ...base, part_id: "p2" }] } as unknown as Design;
  assert.match(replaceLoses(twoParts) ?? "", /1 other part/);
});

test("artwork links name a part and an artwork", () => {
  assert.deepEqual(parseFeatureHref("feature:p1/a3"), { partId: "p1", featureId: "a3" });
  assert.equal(parseFeatureHref("feature:p1/b3"), null);
});

const artScene: SceneSegment[] = [
  { type: "cut", op: "outline", kind: "outline", provenance: "structure", arc: false, start: [0, 0], end: [9, 0] },
  {
    type: "cut", op: "a1.1", kind: "artwork", provenance: "agent sketch", feature_id: "a1", face_id: "P2",
    arc: true, start: [3, 2], end: [3, 2], center: [2, 2], clockwise: false, full_circle: true,
  },
  {
    type: "crease", op: "a1.2", kind: "artwork_crease", provenance: "agent sketch", feature_id: "a1", face_id: "P2",
    arc: false, start: [1, 4], end: [3, 4],
  },
  {
    type: "cut", op: "a2.1", kind: "artwork", provenance: "library artwork", feature_id: "a2", face_id: "P1",
    arc: false, start: [6, 1], end: [7, 1],
  },
  {
    type: "annotation", op: null, kind: null, provenance: "agent sketch", feature_id: "a3", stale: true,
    arc: false, start: [0, 5], end: [1, 5],
  },
];

test("agent-drawn artwork gets its own overlay: line styles kept, one framed box per sketch", () => {
  const o = agentSketches(artScene, "in");
  assert.deepEqual(o.paths.map((p) => p.type), ["cut", "crease"]);
  assert.equal(o.frames.length, 1);
  const f = o.frames[0];
  assert.equal(f.featureId, "a1");
  // The circle (centre 2, 2, radius 1) and the crease up to y = 4, in millimetres.
  assert.deepEqual(
    [f.box.xmin, f.box.ymin, f.box.xmax, f.box.ymax].map((v) => Math.round(v * 100) / 100),
    [25.4, 25.4, 76.2, 101.6],
  );
  assert.deepEqual(agentSketches(undefined).frames, []);
});

test("the header pill and the flags waiting for the designer", () => {
  const base = { checks: [], features: [] } as unknown as Design;
  assert.equal(hasAgentDrawn(base), false);
  const drawn = {
    ...base,
    parts: [{ ...base, features: [{ id: "a1", kind: "artwork", status: "ok", agent_drawn: true }] }],
  } as unknown as Design;
  assert.equal(hasAgentDrawn(drawn), true);
  const flags = pendingFlags([
    { id: "artwork_a1_flag_slot", status: "fail", message: "", ack: "a1:slot:x" },
    { id: "artwork_a1_flag_span", status: "info", message: "", ack: "a1:span:x", acknowledged: true },
    { id: "material", status: "fail", message: "" },
  ]);
  assert.deepEqual(flags.map((c) => c.id), ["artwork_a1_flag_slot"]);
});

test("the form's Replace starts a clean standard part: no features, artwork or acknowledgements", () => {
  // Review finding 4: the form used to spread the design's params, so Replace kept them.
  const initial = {
    style: "reg-glio-nx",
    length: 14,
    width: 10,
    depth: 12,
    units: "in" as const,
    board: " 32  b kr",
    options: { top_flaps: "standard" },
    features: [{ id: "f1", kind: "handhole" }],
    artwork: [{ id: "a1", kind: "shape" }],
    acknowledged: ["a1:parallel:abc"],
    overrides: [{ id: "o1" }],
  } as unknown as Parameters<typeof plainStandard>[0];
  const clean = plainStandard(initial, ["32 B KR", "32 C KR"]);
  assert.deepEqual(clean, {
    style: "reg-glio-nx",
    length: 14,
    width: 10,
    depth: 12,
    units: "in",
    board: "32 B KR",
    options: { top_flaps: "standard" },
  });
  assert.ok(!("features" in clean) && !("artwork" in clean) && !("acknowledged" in clean));
  // the options are a copy, so editing the form never edits the design on screen
  clean.options!.top_flaps = "gap_lap";
  assert.equal(initial.options!.top_flaps, "standard");
});


test("a short sketch arc frames only its swept bounds", () => {
  const x = Math.sqrt(100 ** 2 - 1);
  const arc = {
    ...artScene[1],
    center: [0, 0],
    start: [x, -1],
    end: [x, 1],
    clockwise: false,
    full_circle: false,
  } satisfies SceneSegment;
  assert.deepEqual(agentSketches([arc], "mm").frames[0].box, {
    xmin: x, ymin: -1, xmax: Math.hypot(x, 1), ymax: 1,
  });
});

test("a shallow arc of a large radius is framed by what it sweeps, not its whole circle", () => {
  // Radius 80 in, the arc a gentle bow from (0, 0) to (4, 0), bulging down to y = -0.025.
  const r = 80;
  const cy = Math.sqrt(r * r - 4);
  const bow: SceneSegment = {
    type: "cut", op: "a1.1", kind: "artwork", provenance: "agent sketch", feature_id: "a1", face_id: "P2",
    arc: true, start: [0, 0], end: [4, 0], center: [2, cy], clockwise: false,
  };
  const f = agentSketches([bow], "in").frames[0].box;
  assert.ok(f.xmax - f.xmin < 4 * 25.4 + 1e-6 && f.ymax - f.ymin < 0.03 * 25.4);
  assert.ok(Math.abs(f.ymin - (cy - r) * 25.4) < 1e-6);
  // Each direction sweeps its own extremes: counter-clockwise from (1, 0) to (0, 1) about the origin
  // passes nothing beyond its ends; clockwise between them passes 270, 180 deg.
  const q = (clockwise: boolean): SceneSegment => ({
    ...bow, start: [1, 0], end: [0, 1], center: [0, 0], clockwise,
  });
  const ys = (pts: [number, number][]) => pts.map((p) => Math.round(p[1] * 1e9) / 1e9);
  const xs = (pts: [number, number][]) => pts.map((p) => Math.round(p[0] * 1e9) / 1e9);
  assert.equal(Math.min(...ys(segmentExtent(q(false)))), 0);
  assert.equal(Math.min(...ys(segmentExtent(q(true)))), -1);
  assert.equal(Math.min(...xs(segmentExtent(q(true)))), -1);
});
