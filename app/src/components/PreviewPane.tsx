import { useMemo, useRef, useState } from "react";

import { type Cf2Doc, type LineKind, parseCf2 } from "../lib/cf2";
import { EngineError, request } from "../lib/engine";
import type { BoxParams, EngineInfo } from "../lib/protocol";
import type { PreviewSource } from "../lib/store";
import { DielineCanvas, KIND_LABELS } from "./DielineCanvas";

interface Props {
  width: number;
  source: PreviewSource | null;
  info: EngineInfo | null;
  onPreview: (source: PreviewSource) => void;
}

const KINDS: LineKind[] = ["cut", "crease", "perforation", "score", "other"];

export function PreviewPane({ width, source, info, onPreview }: Props) {
  const [visible, setVisible] = useState<Record<LineKind, boolean>>({
    cut: true,
    crease: true,
    perforation: true,
    score: true,
    other: true,
  });
  const [fitToken, setFitToken] = useState(0);
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  const [zoom, setZoom] = useState(1);
  const [status, setStatus] = useState<{ tone: "info" | "error"; text: string } | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const parsed = useMemo((): { doc: Cf2Doc | null; error: string | null } => {
    if (!source) return { doc: null, error: null };
    try {
      return { doc: parseCf2(source.kind === "design" ? source.design.cf2 : source.text), error: null };
    } catch (err) {
      return { doc: null, error: err instanceof Error ? err.message : String(err) };
    }
  }, [source]);
  const doc = parsed.doc;

  const design = source?.kind === "design" ? source.design : null;
  const title = source ? (source.kind === "design" ? source.design.name : source.name) : "Preview";
  const present = new Set(doc?.primitives.map((p) => p.type));

  const openFile = async (file: File) => {
    const text = await file.text();
    onPreview({ kind: "file", name: file.name, text });
    setStatus(null);
  };

  const exportCf2 = async () => {
    if (!design) return;
    try {
      const res = await request("box.export", { params: design.params });
      setStatus({ tone: "info", text: `Saved ${res.path}` });
    } catch (err) {
      setStatus({ tone: "error", text: err instanceof EngineError ? err.message : String(err) });
    }
  };

  return (
    <section className="preview" style={{ width }}>
      <header className="pane-header">
        <h2 className="pane-title" title={title}>
          {title}
        </h2>
        {source && (
          <span className="badge subtle">
            {source.kind === "file" ? "file" : source.origin === "agent" ? "from chat" : "from form"}
          </span>
        )}
        <div className="spacer" />
        <button className="btn btn-ghost" onClick={() => setFormOpen((o) => !o)} aria-expanded={formOpen}>
          Parameters
        </button>
        <button className="btn btn-ghost" onClick={() => fileRef.current?.click()}>
          Open CF2…
        </button>
        <button className="btn btn-ghost" onClick={() => void exportCf2()} disabled={!design}>
          Export CF2
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".cf2,.CF2,text/plain"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void openFile(f);
            e.target.value = "";
          }}
        />
      </header>

      {formOpen && info && (
        <QuickBox
          info={info}
          initial={design?.params}
          onCreated={(d) => onPreview({ kind: "design", design: d, origin: "form" })}
          onError={(text) => setStatus({ tone: "error", text })}
        />
      )}

      <div className="canvas-wrap">
        {doc ? (
          <DielineCanvas doc={doc} visible={visible} fitToken={fitToken} onCursor={setCursor} onZoom={setZoom} />
        ) : (
          <div className="canvas-empty">
            {parsed.error ? (
              <span className="error-text">Couldn't read this CF2: {parsed.error}</span>
            ) : (
              <>
                <p>No dieline yet.</p>
                <p className="muted">Ask in the chat, use Parameters, or open a CF2 file.</p>
              </>
            )}
          </div>
        )}
        {doc && (
          <div className="legend">
            {KINDS.filter((k) => present.has(k)).map((k) => (
              <label key={k} className="legend-item">
                <input
                  type="checkbox"
                  checked={visible[k]}
                  onChange={(e) => setVisible({ ...visible, [k]: e.target.checked })}
                />
                <span className={`swatch ln-${k}`} />
                {KIND_LABELS[k]}
              </label>
            ))}
            <button className="btn btn-ghost small" onClick={() => setFitToken((n) => n + 1)}>
              Fit
            </button>
          </div>
        )}
      </div>

      {design && <Checks design={design} />}

      <footer className="statusbar">
        {doc && (
          <>
            <span>
              Blank {(doc.bounds.xmax - doc.bounds.xmin).toFixed(1)} × {(doc.bounds.ymax - doc.bounds.ymin).toFixed(1)} mm
            </span>
            <span>{doc.primitives.length} segments</span>
            <span>{Math.round(zoom * 100) / 100} px/mm</span>
            {cursor && (
              <span>
                x {cursor.x.toFixed(1)} · y {cursor.y.toFixed(1)}
              </span>
            )}
            {doc.warnings.length > 0 && <span className="warn-text">{doc.warnings.length} parse warnings</span>}
            {present.has("other") && (
              <span className="warn-text" title={JSON.stringify(doc.codes)}>
                unmapped line codes present
              </span>
            )}
          </>
        )}
        {status && <span className={status.tone === "error" ? "error-text" : "muted"}>{status.text}</span>}
      </footer>
    </section>
  );
}

function Checks({ design }: { design: NonNullable<Extract<PreviewSource, { kind: "design" }>["design"]> }) {
  const failing = design.checks.filter((c) => c.status !== "pass");
  return (
    <details className="checks" open={design.overall === "fail"}>
      <summary>
        <span className={`badge ${design.overall}`}>{design.overall}</span>
        Checks · {design.checks.length - failing.length}/{design.checks.length} pass
      </summary>
      <ul>
        {design.checks.map((c) => (
          <li key={c.id} className={c.status}>
            <span className={`badge ${c.status}`}>{c.status}</span> {c.message}
          </li>
        ))}
      </ul>
    </details>
  );
}

function QuickBox(props: {
  info: EngineInfo;
  initial?: BoxParams;
  onCreated: (d: Awaited<ReturnType<typeof request<"box.create">>>) => void;
  onError: (msg: string) => void;
}) {
  const { info } = props;
  const [p, setP] = useState<BoxParams>(
    props.initial ?? {
      style: info.styles[0]?.id ?? "",
      length: 300,
      width: 200,
      depth: 150,
      units: "mm",
      board: info.boards.find((b) => b.id === "C-32ECT")?.id ?? info.boards[0]?.id ?? "",
    },
  );
  const [busy, setBusy] = useState(false);
  const num = (k: "length" | "width" | "depth") => (e: React.ChangeEvent<HTMLInputElement>) =>
    setP({ ...p, [k]: Number(e.target.value) });

  return (
    <form
      className="quickbox"
      onSubmit={(e) => {
        e.preventDefault();
        setBusy(true);
        request("box.create", p)
          .then(props.onCreated)
          .catch((err: unknown) => props.onError(err instanceof Error ? err.message : String(err)))
          .finally(() => setBusy(false));
      }}
    >
      <label>
        Style
        <select value={p.style} onChange={(e) => setP({ ...p, style: e.target.value })}>
          {info.styles.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        L
        <input type="number" min={1} step="any" value={p.length} onChange={num("length")} />
      </label>
      <label>
        W
        <input type="number" min={1} step="any" value={p.width} onChange={num("width")} />
      </label>
      <label>
        D
        <input type="number" min={1} step="any" value={p.depth} onChange={num("depth")} />
      </label>
      <label>
        Units
        <select value={p.units} onChange={(e) => setP({ ...p, units: e.target.value as "mm" | "in" })}>
          <option value="mm">mm</option>
          <option value="in">in</option>
        </select>
      </label>
      <label>
        Board
        <select value={p.board} onChange={(e) => setP({ ...p, board: e.target.value })}>
          {info.boards.map((b) => (
            <option key={b.id} value={b.id}>
              {b.name} ({b.caliper_mm} mm)
            </option>
          ))}
        </select>
      </label>
      <button className="btn btn-primary" type="submit" disabled={busy}>
        Generate
      </button>
    </form>
  );
}
