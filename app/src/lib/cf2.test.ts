import assert from "node:assert/strict";
import { test } from "node:test";

import { arcSweep, parseCf2, toSvgPath } from "./cf2.ts";

const SAMPLE = [
  "$BOF",
  "V2",
  "ORDER",
  "test",
  "END",
  "MAIN,test",
  "UM",
  "LL,0,0",
  "UR,100,50",
  "SCALE,1,1",
  "L,2,1,0,0,0,100,0,0,0",
  "L,2,2,0,50,0,50,50,0,0",
  "L,2,9,0,0,50,100,50,0,0",
  "A,2,1,0,10,0,0,10,0,0,1,0,0",
  "C,sub,100,0,0,1,1",
  "END",
  "SUB,sub",
  "L,2,3,0,0,0,10,0,0,0",
  "END",
  "$EOF",
].join("\r\n");

test("parses lines, arcs, subroutine calls and unknown codes", () => {
  const doc = parseCf2(SAMPLE);
  assert.equal(doc.name, "test");
  assert.equal(doc.primitives.length, 5);
  assert.deepEqual(
    doc.primitives.map((p) => p.type),
    ["cut", "crease", "other", "cut", "perforation"],
  );
  assert.equal(doc.codes[9], 1);
  const sub = doc.primitives[4];
  assert.ok(sub.kind === "line" && sub.x1 === 100 && sub.x2 === 110);
  assert.deepEqual(doc.bounds, { xmin: 0, ymin: 0, xmax: 110, ymax: 50 });
});

test("inch files are converted to mm", () => {
  const doc = parseCf2("MAIN,x\nUI\nL,2,1,0,0,0,1,0,0,0\nEND");
  const p = doc.primitives[0];
  assert.ok(p.kind === "line" && Math.abs(p.x2 - 25.4) < 1e-9);
});

test("arc direction maps to the SVG sweep flag", () => {
  const ccw = { kind: "arc", code: 1, type: "cut", sx: 10, sy: 0, ex: 0, ey: 10, cx: 0, cy: 0, cw: false } as const;
  assert.ok(Math.abs(arcSweep(ccw) - Math.PI / 2) < 1e-9);
  // Model CCW quarter turn, drawn with y negated: small arc, sweep-flag 0.
  assert.equal(toSvgPath(ccw), "M10 0A10 10 0 0 0 0 -10");
  assert.equal(toSvgPath({ ...ccw, cw: true }), "M10 0A10 10 0 1 1 0 -10");
});

test("rejects text without a MAIN block", () => {
  assert.throws(() => parseCf2("hello"), /no MAIN block/);
});
