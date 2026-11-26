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
      /** PNG data URL of what the model was shown. */
      image?: string;
      /** Set when an older preview image was dropped to cap memory in long sessions. */
      imageCleared?: boolean;
    }
  | { kind: "notice"; id: string; tone: "error" | "info"; text: string };

export interface Session {
  id: string;
  title: string;
  items: ChatItem[];
  busy: boolean;
  design: Design | null;
  preview: PreviewSource | null;
}

export type PreviewSource =
  | {
      kind: "design";
      design: Design;
      origin: "agent" | "form" | "job";
      /** File name of the job it was opened from, reused as the export name. */
      stem?: string | null;
      /** Differences found when reopening a job (board values, geometry). */
      notes?: string[];
    }
  | {
      kind: "file";
      name: string;
      text: string;
      /** A job opened as exported: every part's CF2 from its record, shown one at a time. */
      parts?: { label: string; quantity: number; name: string; text: string }[];
      /** The record it came from, to regenerate it with current rules. */
      job?: {
        record: unknown;
        stem: string | null;
        exportedAt?: string | null;
        /** Whether the files on disk are still that set (complete, pending, incomplete, unverified). */
        setStatus?: string;
        setNotes?: string[];
      };
    };

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
  /** A note in a session's chat (e.g. a layout card that couldn't be chosen). */
  | { type: "notice"; sessionId: string; tone: "error" | "info"; text: string }
  /** Show a source in a session's preview. sessionId: the session that asked (its reply may land after a switch). */
  | { type: "preview"; source: PreviewSource; sessionId?: string }
  | { type: "engine_ready"; ready: boolean };

const uid = () => crypto.randomUUID();

export function newSession(): Session {
  return { id: uid(), title: "New design", items: [], busy: false, design: null, preview: null };
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

/** Tool calls still "running" when a turn ends never got a result. */
function markUnfinishedTools(s: Session): Session {
  return {
    ...s,
    items: s.items.map((it) => (it.kind === "tool" && it.status === "running" ? { ...it, status: "error" } : it)),
  };
}

/** Keep the most recent N tool preview images per session so long sessions stay lean. */
export const MAX_RETAINED_IMAGES = 24;

function pruneOldImages(items: ChatItem[], maxImages = MAX_RETAINED_IMAGES): ChatItem[] {
  let kept = 0;
  let needsPrune = false;
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "tool" && it.image) {
      kept++;
      if (kept > maxImages) {
        needsPrune = true;
        break;
      }
    }
  }
  if (!needsPrune) return items;
  let seen = 0;
  const out = items.slice();
  for (let i = out.length - 1; i >= 0; i--) {
    const it = out[i];
    if (it.kind === "tool" && it.image) {
      seen++;
      if (seen > maxImages) {
        const { image: _dropped, ...rest } = it;
        out[i] = { ...rest, imageCleared: true };
      }
    }
  }
  return out;
}

/** Is `incoming` older than the design a session holds? Commits are numbered per session (seq). */
export function isStale(held: Design | null, incoming: Design): boolean {
  return held?.seq !== undefined && incoming.seq !== undefined && incoming.seq < held.seq;
}

function sameSnapshot(a: Design, b: Design): boolean {
  return a.design_id !== undefined && a.design_id === b.design_id && a.revision === b.revision;
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
        // Show the selected session's own preview, never one left over from another session.
        preview: s?.preview ?? (s?.design ? { kind: "design", design: s.design, origin: "agent" } : null),
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
    case "notice":
      return updateSession(state, action.sessionId, (s) => notice(s, action.tone, action.text));
    case "preview": {
      // Goes to the session that asked, never to whichever is on screen when the reply lands.
      const target = action.sessionId ?? state.activeId;
      const session = state.sessions.find((s) => s.id === target);
      if (!session) return state;
      const source = action.source;
      if (source.kind === "design" && isStale(session.design, source.design)) return state; // a newer one is shown
      const next = updateSession(state, target, (s) => ({
        ...s,
        preview: source,
        design: source.kind === "design" ? source.design : s.design,
      }));
      return target === state.activeId ? { ...next, preview: source } : next;
    }
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
    case "engine.exit": {
      // A restarted engine holds no designs: what the preview shows saves from its parameters.
      const forget = (d: Design | null): Design | null =>
        d && d.design_id !== undefined ? { ...d, design_id: undefined, revision: undefined, seq: undefined } : d;
      const sessions = state.sessions.map((s) => {
        const base = s.busy
          ? { ...notice(markUnfinishedTools(s), "error", "The engine stopped during this turn."), busy: false }
          : s;
        const preview = base.preview?.kind === "design" ? { ...base.preview, design: forget(base.preview.design)! } : base.preview;
        return { ...base, design: forget(base.design), preview };
      });
      const active = sessions.find((s) => s.id === state.activeId);
      return { ...state, engineReady: false, sessions, preview: active?.preview ?? state.preview };
    }
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
      return updateSession(state, e.session_id, (s) => {
        const done = {
          status: (e.is_error ? "error" : "done") as "error" | "done",
          input: e.input,
          result: e.result,
          image: e.image,
          imageCleared: undefined as boolean | undefined,
        };
        const hasMatch = Boolean(e.tool_use_id) && s.items.some((it) => it.kind === "tool" && it.id === e.tool_use_id);
        const items: ChatItem[] = hasMatch
          ? s.items.map((it) =>
              it.kind === "tool" && it.id === e.tool_use_id ? { ...it, name: e.name || it.name, ...done } : it,
            )
          : [
              ...s.items,
              // Guard against tool results arriving without a start event (or with an empty id).
              { kind: "tool", id: e.tool_use_id || uid(), name: e.name, ...done },
            ];
        return { ...s, items: e.image ? pruneOldImages(items) : items };
      });
    case "chat.error":
      return updateSession(state, e.session_id, (s) => notice(s, "error", e.message));
    case "chat.turn_end":
      return updateSession(state, e.session_id, (s) => {
        const ended = { ...markUnfinishedTools(s), busy: false };
        if (e.stop_reason === "cancelled")
          return notice(ended, "info", "Stopped. This turn was not kept in the conversation.");
        if (e.stop_reason === "error") return notice(ended, "info", "This turn was not kept in the conversation.");
        return ended;
      });
    case "design": {
      const session = e.session_id ? state.sessions.find((s) => s.id === e.session_id) : undefined;
      if (session && isStale(session.design, e.design)) return state; // arrived after a newer one
      // The event for a design this session already shows (a form or job reply landed first)
      // keeps that preview's origin, job name and notes.
      // The designer's own replacement (form, job) is shown by its reply, in its request order;
      // its event only updates the session's design underneath a raw file being looked at.
      if (session && e.origin === "form" && session.preview?.kind === "file") {
        return updateSession(state, session.id, (s) => ({ ...s, design: e.design }));
      }
      const shown = session?.preview?.kind === "design" ? session.preview : null;
      const same = shown && sameSnapshot(shown.design, e.design);
      const preview: PreviewSource = same ? { ...shown, design: e.design } : { kind: "design", design: e.design, origin: "agent" };
      const withDesign = e.session_id
        ? updateSession(state, e.session_id, (s) => ({ ...s, design: e.design, preview }))
        : state;
      // Only replace the preview if the design belongs to the session on screen.
      if (e.session_id && e.session_id !== state.activeId) return withDesign;
      return { ...withDesign, preview };
    }
    case "export.pending":
      return state; // the export bar asks the designer (PreviewPane listens for it)
  }
}
