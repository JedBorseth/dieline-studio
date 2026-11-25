import assert from "node:assert/strict";
import { test } from "node:test";

import { convertLengthText, fmtExactIn, fmtIn, parseLength, snapInchNoise } from "./units.ts";

test("formats inches as fractions", () => {
  assert.equal(fmtIn(14.3125), "14 5/16");
  assert.equal(fmtIn(0.03125), "1/32");
  assert.equal(fmtIn(-0.5), "-1/2");
  assert.equal(fmtIn(6), "6");
  assert.equal(fmtIn(1.23456), "1.2346");
});

test("parses fractions and decimals", () => {
  assert.equal(parseLength("34 7/16"), 34.4375);
  assert.equal(parseLength("8-1/2"), 8.5);
  assert.equal(parseLength("14+3/16"), 14.1875);
  assert.equal(parseLength("10:03"), 10.1875);
  assert.equal(parseLength("-0:08"), -0.5);
  assert.ok(Number.isNaN(parseLength("10:16")));
  assert.equal(parseLength('6"'), 6);
  assert.equal(parseLength("-1/2"), -0.5);
  assert.equal(parseLength(".5"), 0.5);
  assert.equal(parseLength("1e-7"), 1e-7);
  assert.equal(parseLength("-1 1/2"), -1.5);
  assert.ok(Number.isNaN(parseLength("--1/2")));
  assert.ok(Number.isNaN(parseLength("12 1/0")));
  assert.ok(Number.isNaN(parseLength("abc")));
});

test("unit switching preserves 1/32 inch fractions and metric round-trips", () => {
  const mm32 = convertLengthText("14 1/32", "mm");
  assert.ok(Math.abs(Number(mm32) - 14.03125 * 25.4) < 1e-4);
  assert.equal(convertLengthText(mm32, "in"), "14 1/32");

  const mm16 = convertLengthText("14+3/16", "mm");
  assert.equal(mm16, "360.3625");
  assert.equal(convertLengthText(mm16, "in"), "14 3/16");

  assert.equal(convertLengthText("not a number", "mm"), "not a number");
});

test("snapInchNoise cleans CF2 float noise without distorting metric blanks", () => {
  assert.equal(snapInchNoise(14.031251), 14.03125);
  assert.equal(fmtIn(snapInchNoise(14.031251)), "14 1/32");
  // 305 x 205 x 150 mm blank width (~41.7824803 in) must not snap to a 1/1024 grid (41.7822).
  assert.equal(fmtIn(snapInchNoise(41.7824803)), "41.7825");
});

test("fmtExactIn preserves non-1/32 decimal precision", () => {
  assert.equal(fmtExactIn(0.5), "1/2");
  assert.equal(fmtExactIn(-0.25), "-1/4");
  assert.equal(fmtExactIn(0.123456), "0.123456");
});
