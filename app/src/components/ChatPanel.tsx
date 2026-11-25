import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";

import type { Settings } from "../lib/protocol";
import type { ChatItem, Session } from "../lib/store";

interface Props {
  session: Session;
  settings: Settings | null;
  engineError: string | null;
  onSend: (text: string) => void;
  onStop: () => void;
  onOpenSettings: () => void;
}

const SUGGESTIONS = [
  "RSC shipping box, inside 300 × 200 × 150 mm, C-flute 32 ECT",
  "12 × 10 × 8 in regular slotted box in B-flute",
  "What styles and boards can you make?",
];

export function ChatPanel({ session, settings, engineError, onSend, onStop, onOpenSettings }: Props) {
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [session.items]);

  const modelLabel = settings?.models.find((m) => m.id === settings.model)?.label ?? settings?.model;

  return (
    <section className="chat">
      <header className="pane-header">
        <h1 className="pane-title">{session.title}</h1>
      </header>

      <div className="messages" ref={listRef}>
        {engineError && (
          <div className="notice error">
            Can't reach the engine: {engineError}
            <br />
            <small>Desktop app: the sidecar failed to start. Browser dev: run the Vite dev server.</small>
          </div>
        )}
        {settings && !settings.has_api_key && (
          <div className="notice info">
            Chat needs an Anthropic API key.{" "}
            <button className="link" onClick={onOpenSettings}>
              Add one in Settings
            </button>
            . The parameter form in the preview pane works without one.
          </div>
        )}
        {session.items.length === 0 ? (
          <div className="empty">
            <p>Describe the box. The engine draws the dieline; Claude only picks the parameters.</p>
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s} className="chip" onClick={() => onSend(s)} disabled={session.busy}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          session.items.map((item) => <Item key={item.id} item={item} />)
        )}
        {session.busy && <div className="working">Working…</div>}
      </div>

      <Composer
        busy={session.busy}
        onSend={onSend}
        onStop={onStop}
        modelLabel={modelLabel ? `${modelLabel} · ${settings?.effort}` : "…"}
        onModelClick={onOpenSettings}
      />
    </section>
  );
}

function Item({ item }: { item: ChatItem }) {
  switch (item.kind) {
    case "user":
      return <div className="msg user">{item.text}</div>;
    case "assistant":
      return (
        <div className="msg assistant">
          {item.thinking && (
            <details className="thinking">
              <summary>Reasoning</summary>
              <div className="thinking-body">{item.thinking}</div>
            </details>
          )}
          {item.text && (
            <div className="markdown">
              <Markdown>{item.text}</Markdown>
            </div>
          )}
        </div>
      );
    case "tool":
      return <ToolCard item={item} />;
    case "notice":
      return <div className={`notice ${item.tone}`}>{item.text}</div>;
  }
}

function ToolCard({ item }: { item: Extract<ChatItem, { kind: "tool" }> }) {
  const input = item.input ?? {};
  let label: string = item.name;
  if (item.name === "create_box" && item.input) {
    const { style, length, width, depth, units, board } = input as Record<string, string | number>;
    label = `create_box · ${style} · ${length} × ${width} × ${depth} ${units} · ${board}`;
  } else if (item.name === "export_cf2") {
    const path = (item.result as { path?: string } | undefined)?.path;
    label = path ? `export_cf2 → ${path}` : "export_cf2";
  }
  const overall = (item.result as { overall?: string } | undefined)?.overall;
  const error = item.status === "error" ? (item.result as { error?: string } | undefined)?.error : undefined;
  return (
    <details className={`tool ${item.status}`}>
      <summary>
        <span className={`tool-status ${item.status}`}>
          {item.status === "running" ? "…" : item.status === "error" ? "✕" : "✓"}
        </span>
        <span className="tool-label">{label}</span>
        {overall && <span className={`badge ${overall}`}>{overall}</span>}
      </summary>
      {error && <div className="tool-error">{error}</div>}
      {item.result !== undefined && <pre className="tool-json">{JSON.stringify(item.result, null, 2)}</pre>}
    </details>
  );
}

function Composer(props: {
  busy: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  modelLabel: string;
  onModelClick: () => void;
}) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [text]);

  const submit = () => {
    const t = text.trim();
    if (!t || props.busy) return;
    props.onSend(t);
    setText("");
  };

  return (
    <div className="composer">
      <textarea
        ref={ref}
        value={text}
        placeholder="Describe a box, or ask for a change…"
        rows={1}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <div className="composer-bar">
        <button className="chip model-chip" onClick={props.onModelClick} title="Model and effort">
          {props.modelLabel}
        </button>
        <span className="hint">Enter to send · Shift+Enter for a new line</span>
        {props.busy ? (
          <button className="btn btn-danger" onClick={props.onStop}>
            Stop
          </button>
        ) : (
          <button className="btn btn-primary" onClick={submit} disabled={!text.trim()}>
            Send
          </button>
        )}
      </div>
    </div>
  );
}
