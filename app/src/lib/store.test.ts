import assert from "node:assert/strict";
import { test } from "node:test";

import type { Design } from "./protocol.ts";
import { initialState, MAX_RETAINED_IMAGES, reducer } from "./store.ts";

function fakeDesign(id: string, overall: "pass" | "warn" | "fail" = "warn"): Design {
  return {
    id,
    name: `REG-GLIO-NX ${id}`,
    params: { style: "reg-glio-nx", length: 14, width: 8, depth: 6, units: "in", board: "32 B KR" },
    cf2: "$BOF\r\nMAIN,test\r\nUI\r\nL,2,1,0,0,0,1,0,0,0\r\nEND\r\n$EOF\r\n",
    checks: [],
    overall,
    summary: {
      style: "reg-glio-nx",
      standard: "REG-GLIO-NX",
      units: "in",
      inside_in: { length: 14, width: 8, depth: 6 },
      board: "32 B KR",
      board_verified: true,
      board_source: "artioscad",
      cal_in: 0.125,
      il_in: 0.0625,
      options: {},
      calibrated: false,
      panels_in: [8, 14.125, 8.125, 14.125],
      glue_lug_in: 1.25,
      body_height_in: 6.3125,
      bottom_flaps_in: [4, 4],
      top_flaps_in: [4, 4],
      slot_in: 0.375,
      blank_in: { width: 45.625, height: 14.3125 },
    },
  };
}

test("form and job previews survive switching sessions and keep session.design in sync", () => {
  let state = initialState();
  const s1 = state.activeId;
  const jobDesign = fakeDesign("job-1", "warn");

  state = reducer(state, {
    type: "preview",
    source: {
      kind: "design",
      design: jobDesign,
      origin: "job",
      stem: "RL26-0771",
      notes: ["32 C KR CAL was 5/32 in, now 3/16 in"],
    },
  });
  assert.equal(state.sessions[0].design?.id, "job-1");

  // Start a new session: preview clears.
  state = reducer(state, { type: "new_session" });
  const s2 = state.activeId;
  assert.notEqual(s2, s1);
  assert.equal(state.preview, null);

  // Switch back to session 1: job design, origin, stem, and notes are restored intact.
  state = reducer(state, { type: "select_session", id: s1 });
  assert.equal(state.preview?.kind, "design");
  assert.equal(state.preview.design.id, "job-1");
  assert.equal(state.preview.origin, "job");
  assert.equal(state.preview.stem, "RL26-0771");
  assert.deepEqual(state.preview.notes, ["32 C KR CAL was 5/32 in, now 3/16 in"]);
});

test("background session design events do not hijack the active session preview", () => {
  let state = initialState();
  const s1 = state.activeId;
  state = reducer(state, { type: "new_session" });
  const s2 = state.activeId;

  const d1 = fakeDesign("agent-s1", "pass");
  state = reducer(state, { type: "engine", event: { type: "design", session_id: s1, design: d1 } });
  assert.equal(state.activeId, s2);
  assert.equal(state.preview, null);

  state = reducer(state, { type: "select_session", id: s1 });
  assert.equal(state.preview?.kind, "design");
  assert.equal(state.preview.design.id, "agent-s1");
  assert.equal(state.preview.origin, "agent");
});

test("chat.tool_result attaches images, handles start-less tool results, and avoids empty-id collisions", () => {
  let state = initialState();
  const sid = state.activeId;

  // Normal client tool: start -> result with image
  state = reducer(state, {
    type: "engine",
    event: { type: "chat.tool_start", session_id: sid, tool_use_id: "tu_1", name: "view_design" },
  });
  state = reducer(state, {
    type: "engine",
    event: {
      type: "chat.tool_result",
      session_id: sid,
      tool_use_id: "tu_1",
      name: "view_design",
      input: { region: [0, 0, 5, 5] },
      result: { view: { units: "in" } },
      is_error: false,
      image: "data:image/png;base64,AAA=",
    },
  });

  // Tool result arriving without a prior start event
  state = reducer(state, {
    type: "engine",
    event: {
      type: "chat.tool_result",
      session_id: sid,
      tool_use_id: "call_web_1",
      name: "web_search",
      input: { query: "FEFCO 0201" },
      result: { results: [{ title: "FEFCO", url: "https://example.com" }] },
      is_error: false,
    },
  });

  // Two tool results with empty tool_use_id must not overwrite each other or collide
  state = reducer(state, {
    type: "engine",
    event: {
      type: "chat.tool_result",
      session_id: sid,
      tool_use_id: "",
      name: "web_fetch",
      input: null,
      result: { url: "https://a.example" },
      is_error: false,
    },
  });
  state = reducer(state, {
    type: "engine",
    event: {
      type: "chat.tool_result",
      session_id: sid,
      tool_use_id: "",
      name: "web_fetch",
      input: null,
      result: { url: "https://b.example" },
      is_error: true,
    },
  });

  const items = state.sessions[0].items;
  assert.equal(items.length, 4);
  assert.equal(items[0].kind, "tool");
  assert.equal(items[0].image, "data:image/png;base64,AAA=");
  assert.equal(items[1].kind, "tool");
  assert.equal(items[1].id, "call_web_1");
  assert.equal(items[2].kind, "tool");
  assert.equal(items[3].kind, "tool");
  assert.notEqual(items[2].id, items[3].id);
  assert.ok(items[2].id.length > 0 && items[3].id.length > 0);
  assert.equal(items[3].status, "error");
});

test("long tool sessions cap retained preview images to bound memory", () => {
  let state = initialState();
  const sid = state.activeId;
  const total = MAX_RETAINED_IMAGES + 5;

  for (let i = 0; i < total; i++) {
    state = reducer(state, {
      type: "engine",
      event: {
        type: "chat.tool_result",
        session_id: sid,
        tool_use_id: `tu_${i}`,
        name: "view_design",
        input: {},
        result: { view: { units: "in" } },
        is_error: false,
        image: `data:image/png;base64,IMG_${i}`,
      },
    });
  }

  const tools = state.sessions[0].items.filter((it) => it.kind === "tool");
  assert.equal(tools.length, total);
  const withImage = tools.filter((it) => Boolean(it.image));
  const cleared = tools.filter((it) => it.imageCleared);
  assert.equal(withImage.length, MAX_RETAINED_IMAGES);
  assert.equal(cleared.length, 5);
  assert.equal(tools[0].image, undefined);
  assert.equal(tools[0].imageCleared, true);
  assert.equal(tools[total - 1].image, `data:image/png;base64,IMG_${total - 1}`);
  assert.equal(tools[total - 1].imageCleared, undefined);
});

function snap(id: string, seq: number, revision = 1): Design {
  return { ...fakeDesign(id), design_id: `d-${id}`, revision, seq };
}

test("a reply lands on the session that asked, even after a switch", () => {
  let state = initialState();
  const a = state.activeId;
  state = reducer(state, { type: "new_session" });
  const b = state.activeId;
  state = reducer(state, { type: "preview", sessionId: a, source: { kind: "design", design: snap("a", 1), origin: "form" } });
  assert.equal(state.preview, null); // B is on screen and shows nothing
  assert.equal(state.sessions.find((s) => s.id === b)?.design, null);
  assert.equal(state.sessions.find((s) => s.id === a)?.design?.design_id, "d-a");
});

test("an older design never replaces a newer one", () => {
  let state = initialState();
  const s = state.activeId;
  state = reducer(state, { type: "engine", event: { type: "design", session_id: s, design: snap("new", 2) } });
  state = reducer(state, { type: "engine", event: { type: "design", session_id: s, design: snap("old", 1) } });
  assert.equal(state.sessions[0].design?.design_id, "d-new");
  state = reducer(state, { type: "preview", sessionId: s, source: { kind: "design", design: snap("old", 1), origin: "form" } });
  assert.equal(state.preview?.kind === "design" && state.preview.design.design_id, "d-new");
});

test("the event for a design already shown keeps the job's name and notes", () => {
  let state = initialState();
  const s = state.activeId;
  const d = snap("job", 3);
  state = reducer(state, { type: "preview", sessionId: s, source: { kind: "design", design: d, origin: "job", stem: "RL26-0001", notes: ["board changed"] } });
  state = reducer(state, { type: "engine", event: { type: "design", session_id: s, design: d } });
  assert.ok(state.preview?.kind === "design" && state.preview.origin === "job" && state.preview.stem === "RL26-0001");
});

test("after an engine restart, shown designs save from their parameters", () => {
  let state = initialState();
  const s = state.activeId;
  state = reducer(state, { type: "engine", event: { type: "design", session_id: s, design: snap("x", 1) } });
  state = reducer(state, { type: "engine", event: { type: "engine.exit", code: 1 } });
  assert.equal(state.sessions[0].design?.design_id, undefined);
  assert.ok(state.preview?.kind === "design" && state.preview.design.design_id === undefined);
});

test("the designer's own replacement doesn't take over a raw file from its event", () => {
  let state = initialState();
  const s = state.activeId;
  state = reducer(state, { type: "preview", sessionId: s, source: { kind: "file", name: "sample.cf2", text: "x" } });
  state = reducer(state, { type: "engine", event: { type: "design", session_id: s, design: snap("form", 2), origin: "form" } });
  assert.equal(state.preview?.kind, "file");
  assert.equal(state.sessions[0].design?.design_id, "d-form");
  state = reducer(state, { type: "engine", event: { type: "design", session_id: s, design: snap("chat", 3), origin: "chat" } });
  assert.equal(state.preview?.kind, "design"); // the chat's new design is shown
});
