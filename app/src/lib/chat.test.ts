import assert from "node:assert/strict";
import { test } from "node:test";

import { cleanPathInput, formatModelPrice, formatModelSummary, formatToolLabel, parsePathList } from "./chat.ts";

test("formatToolLabel handles running (empty input) states without trailing dots or empty quotes", () => {
  assert.equal(formatToolLabel("search_files", undefined, undefined), "search_files");
  assert.equal(formatToolLabel("read_file", undefined, undefined), "read_file");
  assert.equal(formatToolLabel("web_search", undefined, undefined), "web_search");
  assert.equal(formatToolLabel("web_fetch", undefined, undefined), "web_fetch");
  assert.equal(formatToolLabel("view_design", undefined, undefined), "view_design");
  assert.equal(formatToolLabel("get_geometry", undefined, undefined), "get_geometry");
  assert.equal(formatToolLabel("create_box", {}, undefined), "create_box");
});

test("formatToolLabel summarizes completed tool inputs and results", () => {
  assert.equal(
    formatToolLabel(
      "create_box",
      { style: "reg-glio-nx", length: "14 1/8", width: 8, depth: 6, units: "in", board: "32 C KR" },
      undefined,
    ),
    "create_box · reg-glio-nx · 14 1/8 × 8 × 6 in · 32 C KR",
  );
  assert.equal(
    formatToolLabel("view_design", { path: "C:\\Users\\Example\\JOB-0123.dxf", region: [0, 0, 4, 8] }, undefined),
    "view_design · JOB-0123.dxf · zoom 0, 0, 4, 8",
  );
  assert.equal(
    formatToolLabel("get_geometry", { line_type: "crease", region: [1, 2, 3, 4] }, undefined),
    "get_geometry · crease · zoom 1, 2, 3, 4",
  );
  assert.equal(
    formatToolLabel("search_files", { query: "JOB-0123" }, { total_matches: 3 }),
    'search_files · "JOB-0123" · 3 found',
  );
  assert.equal(
    formatToolLabel("read_file", { path: "/ref/2026_Design_Log.xlsx", page: 2 }, { page: 2, pages: 4 }),
    "read_file · 2026_Design_Log.xlsx · p. 2",
  );
  assert.equal(
    formatToolLabel("web_search", { query: "FEFCO 0201" }, { results: [{ title: "a" }] }),
    'web_search · "FEFCO 0201" · 1 result',
  );
  assert.equal(
    formatToolLabel("web_search", { query: "FEFCO 0201" }, { results: [{ title: "a" }, { title: "b" }] }),
    'web_search · "FEFCO 0201" · 2 results',
  );
  assert.equal(
    formatToolLabel("web_fetch", null, { url: "https://fefco.org/code" }),
    "web_fetch · https://fefco.org/code",
  );
});

test("cleanPathInput and parsePathList handle Windows quotes and CRLF", () => {
  assert.equal(cleanPathInput('  "C:\\Users\\Example\\OneDrive - Example"  '), "C:\\Users\\Example\\OneDrive - Example");
  assert.equal(cleanPathInput("\\\\server\\Sample_Files\\Example"), "\\\\server\\Sample_Files\\Example");
  assert.deepEqual(
    parsePathList('"C:\\OneDrive"\r\n\r\n  D:\\Specs  \n'),
    ["C:\\OneDrive", "D:\\Specs"],
  );
});

test("formatModelPrice handles dynamic, free, missing, integer, and fractional prices", () => {
  assert.equal(formatModelPrice(-1000000, -1000000), "Dynamic pricing");
  assert.equal(formatModelPrice(-1, 5), "Dynamic pricing");
  assert.equal(formatModelPrice(0, 0), "Free");
  assert.equal(formatModelPrice(null, 5), null);
  assert.equal(formatModelPrice(5, null), null);
  assert.equal(formatModelPrice(4, 20), "$4/$20 per M tokens in/out");
  assert.equal(formatModelPrice(0.3, 2.5), "$0.30/$2.50 per M tokens in/out");
  assert.equal(formatModelPrice(0.1824, 11.0055), "$0.1824/$11.0055 per M tokens in/out");
});

test("formatModelSummary formats live, non-reasoning, dynamic, unlisted, and offline states", () => {
  assert.equal(
    formatModelSummary(
      {
        id: "openrouter/auto",
        label: "Auto Router",
        context: 2000000,
        input_per_mtok: -1000000,
        output_per_mtok: -1000000,
        reasoning: true,
      },
      true,
    ),
    "Auto Router · Dynamic pricing",
  );
  assert.equal(
    formatModelSummary(
      {
        id: "amazon/nova-pro-v1",
        label: "Amazon: Nova Pro 1.0",
        context: 300000,
        input_per_mtok: 0.8,
        output_per_mtok: 3.2,
        reasoning: false,
      },
      true,
    ),
    "Amazon: Nova Pro 1.0 · $0.80/$3.20 per M tokens in/out · no reasoning effort",
  );
  assert.equal(
    formatModelSummary(undefined, true),
    "Not in OpenRouter's list of models with tools and image input.",
  );
  assert.equal(
    formatModelSummary(undefined, false, "Claude Opus 5.5"),
    "Claude Opus 5.5 · Any OpenRouter model with tool calling and image input.",
  );
});

test("set_artwork tool rows say what changed and when an import waits for the designer", () => {
  assert.equal(
    formatToolLabel("set_artwork", { items: [{ kind: "sketch" }, { id: "a1" }], remove: ["a2"] }, {}),
    "set_artwork · sketch, a1 · remove 1",
  );
  assert.equal(
    formatToolLabel("set_artwork", { items: [{ kind: "import", path: "x.svg" }] }, { import: { id: "import1" } }),
    "set_artwork · import · waiting for your approval",
  );
});
