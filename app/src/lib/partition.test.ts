import assert from "node:assert/strict";
import { test } from "node:test";

import { formatToolLabel } from "./chat.ts";
import { isPartitionParams, isPartitionSummary } from "./protocol.ts";
import { initialState, reducer } from "./store.ts";

test("partition tool labels", () => {
  assert.equal(
    formatToolLabel("create_partition", { input_mode: "box_cells", cells_long: 4, cells_short: 3 }, undefined),
    "create_partition · box_cells · 4 × 3 cells",
  );
  assert.equal(
    formatToolLabel("create_partition", { input_mode: "product", count: 7 }, { candidates: [{}, {}, {}] }),
    "create_partition · product · 7 items · 3 layouts to choose from",
  );
  assert.equal(
    formatToolLabel("choose_layout", { candidate_id: "c2", message_id: "m3" }, undefined),
    "choose_layout · c2 (named in m3)",
  );
  assert.equal(formatToolLabel("link_fit", { container: "carton", contained: "set_1a2b3c" }, undefined),
    "link_fit · set_1a2b3c in carton");
  assert.equal(formatToolLabel("create_stack", { tiers: ["a", "b"], pads: 3 }, undefined),
    "create_stack · 2 tiers · 3 pads");
});

test("partition params and summaries are told apart from boxes", () => {
  assert.equal(isPartitionParams({ style: "partition-slotted", units: "in", board: "32 B KR", role: "long", set: {} }), true);
  assert.equal(
    isPartitionParams({ style: "reg-glio-nx", length: 1, width: 1, depth: 1, units: "in", board: "32 B KR" }),
    false,
  );
  assert.equal(isPartitionParams(undefined), false);
  assert.equal(isPartitionSummary({ style: "partition-slotted" }), true);
  assert.equal(isPartitionSummary({ style: "reg-glio-nx" }), false);
});

test("a notice lands in its own session without ending a turn", () => {
  let state = initialState();
  const id = state.activeId;
  state = reducer(state, { type: "user_message", sessionId: id, text: "7 jars" });
  state = reducer(state, { type: "notice", sessionId: id, tone: "error", text: "layout c1 has expired" });
  const s = state.sessions.find((x) => x.id === id)!;
  assert.equal(s.busy, true);
  assert.deepEqual(s.items.at(-1), { ...s.items.at(-1), kind: "notice", tone: "error", text: "layout c1 has expired" });
});
