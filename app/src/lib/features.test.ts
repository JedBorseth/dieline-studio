import assert from "node:assert/strict";
import { test } from "node:test";

import {
  featurePaths,
  isActionable,
  keepFeatureLinks,
  parseFeatureHref,
  regionToMm,
  replaceLoses,
  sceneToPrimitive,
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
