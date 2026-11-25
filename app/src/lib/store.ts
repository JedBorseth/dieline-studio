// App state: chat sessions (left + centre) and the preview (right).

import type { Design, EngineEvent } from "./protocol";

export type ChatItem =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string; text: string; thinking: string }
  | {
      kind: "tool";
      id: string;
      name: string;
      status: "running" | "done" | "error";
      input?: Record<string, unknown> | null;
      result?: unknown;
    }
  | { kind: "notice"; id: string; tone: "error" | "info"; text: string };

export interface Session {
  id: string;
  title: string;
  items: ChatItem[];
  busy: boolean;
  design: Design | null;
}

export type PreviewSource =
  | { kind: "design"; design: Design; origin: "agent" | "form" }
  | { kind: "file"; name: string; text: string };

export interface State {
  sessions: Session[];
  activeId: string;
  preview: PreviewSource | null;
  engineReady: boolean;
}

export type Action =
  | { type: "engine"; event: EngineEvent }
  | { type: "new_session" }
  | { type: "select_session"; id: string }
  | { type: "user_message"; sessionId: string; text: string }
  | { type: "send_failed"; sessionId: string; message: string }
  | { type: "preview"; source: PreviewSource }
  | { type: "engine_ready"; ready: boolean };

const uid = () => crypto.randomUUID();

export function newSession(): Session {
  return { id: uid(), title: "New design", items: [], busy: false, design: null };
}

export function initialState(): State {
  const s = newSession();
  return { sessions: [s], activeId: s.id, preview: null, engineReady: false };
}

function updateSession(state: State, id: string, fn: (s: Session) => Session): State {
  return { ...state, sessions: state.sessions.map((s) => (s.id === id ? fn(s) : s)) };
}

function appendAssistant(s: Session, field: "text" | "thinking", chunk: string): Session {
  const last = s.items[s.items.length - 1];
  if (last && last.kind === "assistant" && !(field === "thinking" && last.text)) {
    const items = s.items.slice(0, -1);
    items.push({ ...last, [field]: last[field] + chunk });
    return { ...s, items };
  }
  const item: ChatItem = { kind: "assistant", id: uid(), text: "", thinking: "", [field]: chunk };
  return { ...s, items: [...s.items, item] };
}

function notice(s: Session, tone: "error" | "info", text: string): Session {
  return { ...s, items: [...s.items, { kind: "notice", id: uid(), tone, text }] };
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "new_session": {
      const s = newSession();
      return { ...state, sessions: [s, ...state.sessions], activeId: s.id, preview: null };
    }
    case "select_session": {
      const s = state.sessions.find((x) => x.id === action.id);
      return {
        ...state,
        activeId: action.id,
        preview: s?.design ? { kind: "design", design: s.design, origin: "agent" } : state.preview,
      };
    }
    case "user_message":
      return updateSession(state, action.sessionId, (s) => ({
        ...s,
        busy: true,
        title: s.items.length === 0 ? action.text.slice(0, 48) : s.title,
        items: [...s.items, { kind: "user", id: uid(), text: action.text }],
      }));
    case "send_failed":
      return updateSession(state, action.sessionId, (s) => ({ ...notice(s, "error", action.message), busy: false }));
    case "preview":
      return { ...state, preview: action.source };
    case "engine_ready":
      return { ...state, engineReady: action.ready };
    case "engine":
      return applyEvent(state, action.event);
  }
}

function applyEvent(state: State, e: EngineEvent): State {
  switch (e.type) {
    case "engine.ready":
      return { ...state, engineReady: true };
    case "engine.exit":
      return {
        ...state,
        engineReady: false,
        sessions: state.sessions.map((s) =>
          s.busy ? { ...notice(s, "error", "The engine stopped during this turn."), busy: false } : s,
        ),
      };
    case "chat.text":
      return updateSession(state, e.session_id, (s) => appendAssistant(s, "text", e.text));
    case "chat.thinking":
      return updateSession(state, e.session_id, (s) => appendAssistant(s, "thinking", e.text));
    case "chat.tool_start":
      return updateSession(state, e.session_id, (s) => ({
        ...s,
        items: [...s.items, { kind: "tool", id: e.tool_use_id, name: e.name, status: "running" }],
      }));
    case "chat.tool_result":
      return updateSession(state, e.session_id, (s) => ({
        ...s,
        items: s.items.map((it) =>
          it.kind === "tool" && it.id === e.tool_use_id
            ? { ...it, status: e.is_error ? "error" : "done", input: e.input, result: e.result }
            : it,
        ),
      }));
    case "chat.error":
      return updateSession(state, e.session_id, (s) => notice(s, "error", e.message));
    case "chat.turn_end":
      return updateSession(state, e.session_id, (s) => {
        const ended = { ...s, busy: false };
        if (e.stop_reason === "cancelled")
          return notice(ended, "info", "Stopped. This turn was not kept in the conversation.");
        if (e.stop_reason === "error") return notice(ended, "info", "This turn was not kept in the conversation.");
        if (e.stop_reason === "max_steps") return notice(ended, "info", "Stopped after too many tool steps.");
        return ended;
      });
    case "design": {
      const withDesign = e.session_id
        ? updateSession(state, e.session_id, (s) => ({ ...s, design: e.design }))
        : state;
      // Only replace the preview if the design belongs to the session on screen.
      if (e.session_id && e.session_id !== state.activeId) return withDesign;
      return { ...withDesign, preview: { kind: "design", design: e.design, origin: "agent" } };
    }
  }
}
