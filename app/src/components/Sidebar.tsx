import type { Session } from "../lib/store";

interface Props {
  sessions: Session[];
  activeId: string;
  onNew: () => void;
  onSelect: (id: string) => void;
  onSettings: () => void;
  engineReady: boolean;
  engineVersion?: string;
}

export function Sidebar({ sessions, activeId, onNew, onSelect, onSettings, engineReady, engineVersion }: Props) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark" aria-hidden>
          ◇
        </span>
        Dieline Studio
      </div>
      <button className="btn btn-primary new-session" onClick={onNew}>
        + New design
      </button>
      <nav className="session-list" aria-label="Designs">
        {sessions.map((s) => (
          <button
            key={s.id}
            className={`session ${s.id === activeId ? "active" : ""}`}
            onClick={() => onSelect(s.id)}
            title={s.title}
          >
            <span className={`dot ${s.busy ? "busy" : s.design ? s.design.overall : ""}`} />
            <span className="session-title">{s.title}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar-footer">
        <span className={`engine-status ${engineReady ? "ok" : ""}`}>
          {engineReady ? `Engine ${engineVersion ?? ""}` : "Engine offline"}
        </span>
        <button className="btn btn-ghost" onClick={onSettings} aria-label="Settings">
          ⚙ Settings
        </button>
      </div>
    </aside>
  );
}
