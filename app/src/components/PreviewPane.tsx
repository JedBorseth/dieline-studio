import {
  ChevronRightIcon,
  CircleAlertIcon,
  CircleCheckIcon,
  CircleDashedIcon,
  CircleHelpIcon,
  CircleMinusIcon,
  CircleXIcon,
  CrosshairIcon,
  DownloadIcon,
  FolderOpenIcon,
  InfoIcon,
  RulerIcon,
  ScanIcon,
  SlidersHorizontalIcon,
  SquareDashedIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import { type Cf2Doc, type LineKind, parseCf2 } from "../lib/cf2";
import { EngineError, request } from "../lib/engine";
import { loadString, saveString } from "../lib/prefs";
import {
  type BoxParams,
  type CheckResult,
  type CheckStatus,
  type Design,
  DESIGN_CHANGED,
  EXPORT_EXISTS,
  type EngineInfo,
  type ExportFormat,
  type OptionValue,
  type StyleInfo,
} from "../lib/protocol";
import { convertLengthText, fmtExactIn, fmtIn, mmToIn, parseLength, snapInchNoise } from "../lib/units";
import type { PreviewSource } from "../lib/store";
import { cn } from "../lib/utils";
import { STATUS_BADGE } from "./ChatPanel";
import { DielineCanvas, KIND_LABELS } from "./DielineCanvas";
import { Tip } from "./Tip";
import { Alert, AlertDescription, AlertTitle } from "./ui/alert";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Checkbox } from "./ui/checkbox";
import { Collapsible, CollapsiblePanel, CollapsibleTrigger } from "./ui/collapsible";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { Select, SelectItem, SelectPopup, SelectTrigger, SelectValue } from "./ui/select";
import { Spinner } from "./ui/spinner";

interface Props {
  width: number;
  source: PreviewSource | null;
  info: EngineInfo | null;
  /** The chat session shown; a design made here becomes its current design too. */
  sessionId: string;
  /** The design the session on screen holds, even while the preview shows a raw file. */
  sessionDesign?: Design | null;
  /** Show a source in the preview of the session that asked (sessionId), even if another is now on screen. */
  onPreview: (source: PreviewSource, sessionId: string) => void;
}

interface Status {
  tone: "info" | "error";
  text: string;
  action?: { label: string; run: () => void };
}

const KINDS: LineKind[] = ["cut", "crease", "perforation", "score", "other"];

interface ShowContext {
  sid: string;
  base: Design | null;
  /** Its place in the session's request order, taken when the designer acted: anything
   *  they did after it (opening a file, another Generate) wins. */
  seq: number;
}

export function PreviewPane({ width, source, info, sessionId, sessionDesign, onPreview }: Props) {
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
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
  const [status, setStatus] = useState<Status | null>(null);
  const [notesDismissed, setNotesDismissed] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const exportingRef = useRef(false);
  // Each session orders its own requests (show, open, refresh): a reply only lands if nothing newer
  // started in that session. Status messages belong to the session on screen.
  const seqsRef = useRef(new Map<string, number>());
  const bump = (sid: string): number => {
    const n = (seqsRef.current.get(sid) ?? 0) + 1;
    seqsRef.current.set(sid, n);
    return n;
  };
  const latest = (sid: string, n: number): boolean => seqsRef.current.get(sid) === n;
  const here = (sid: string): boolean => sid === sessionIdRef.current;
  const fileRef = useRef<HTMLInputElement>(null);

  const design = source?.kind === "design" ? source.design : null;
  const designIdRef = useRef<string | null>(design?.id ?? null);
  designIdRef.current = design?.id ?? null;
  // A design with several parts shows one at a time, chosen by the tabs.
  type PartView = Design & { part_id?: string; label?: string; quantity?: number };
  const parts: PartView[] = design?.parts?.length ? design.parts : design ? [design] : [];
  // The selected part is kept by id, so removing another part doesn't move the selection.
  const [partId, setPartId] = useState<string | null>(null);
  const shown: PartView | null = parts.find((p) => p.part_id === partId) ?? parts[0] ?? null;
  const several = parts.length > 1;
  // A second part appearing while the export bar is open: that bar can't export it.
  useEffect(() => {
    if (several) setExportOpen(false);
  }, [several]);

  // A status message (and reopen-notes dismissal) belongs to the dieline it was about;
  // a message queued for the incoming dieline is shown with it.
  // A message queued for the next dieline shown in its own session (never another's).
  const pendingStatusRef = useRef<{ sid: string; status: Status | null } | null>(null);
  useEffect(() => {
    const pending = pendingStatusRef.current;
    setStatus(pending && pending.sid === sessionIdRef.current ? pending.status : null);
    pendingStatusRef.current = null;
    setNotesDismissed(false);
  }, [source]);

  const parsed = useMemo((): { doc: Cf2Doc | null; error: string | null } => {
    if (!source) return { doc: null, error: null };
    try {
      return { doc: parseCf2(source.kind === "design" ? (shown ?? source.design).cf2 : source.text), error: null };
    } catch (err) {
      return { doc: null, error: err instanceof Error ? err.message : String(err) };
    }
  }, [source, shown]);
  const doc = parsed.doc;

  const title = source ? (source.kind === "design" ? (shown ?? source.design).name : source.name) : "Preview";
  const present = new Set(doc?.primitives.map((p) => p.type));

  /** Show a design made outside the chat, and make it the chat's current design too. */
  /** Show a design made outside the chat. The chat's current design is switched
   *  first and the preview only after the engine accepts it, so a chat message sent
   *  meanwhile can never act on a design the preview no longer shows. */
  /** After the engine refused a stale request: show the session's design as it is now. */
  /** seq: the place in the session's request order of the action that hit the refusal (a Save),
   *  so anything the designer did after that action wins over this refresh. */
  const refresh = async (sid: string, text: string, seq: number = bump(sid)): Promise<void> => {
    if (!latest(sid, seq)) return;
    try {
      const current = await request("design.get", { session_id: sid });
      if (!latest(sid, seq)) return;
      if (here(sid)) pendingStatusRef.current = { sid, status: { tone: "error", text } };
      if (current) onPreview({ kind: "design", design: current, origin: "agent" }, sid);
      else if (here(sid)) setStatus({ tone: "error", text });
    } catch (err) {
      if (here(sid)) setStatus({ tone: "error", text: err instanceof Error ? err.message : String(err) });
    }
  };

  /** Where a request started: its session and the design that session held then (whatever
   *  the preview showed, a raw CF2 included). Captured when the designer acts. */
  const context = (): ShowContext => ({ sid: sessionId, base: sessionDesign ?? null, seq: bump(sessionId) });

  const showDesign = async (
    next: Extract<PreviewSource, { kind: "design" }>,
    ctx: ShowContext,
    confirmed = false,
  ): Promise<void> => {
    const baseParts = ctx.base?.parts?.length ?? (ctx.base ? 1 : 0);
    // Replacing a design with several parts loses the others: ask first, and only in the session it
    // started in. If the designer has moved to another chat meanwhile, the replacement is dropped.
    if (!latest(ctx.sid, ctx.seq)) return; // the designer did something newer in that session
    if (!confirmed && baseParts > 1 && next.origin === "form") {
      if (ctx.sid !== sessionIdRef.current) return;
      setStatus({
        tone: "error",
        text: `Replace the current design? Its ${baseParts - 1} other part${baseParts > 2 ? "s" : ""} will be lost.`,
        action: { label: "Replace", run: () => void showDesign(next, { ...ctx, seq: bump(ctx.sid) }, true) },
      });
      return;
    }
    const sid = ctx.sid; // the reply goes to this session even if another is on screen by then
    const seq = ctx.seq;
    // What the designer was looking at: the engine refuses the replacement if it changed meanwhile.
    const expected =
      ctx.base?.design_id !== undefined && ctx.base.revision !== undefined
        ? { design_id: ctx.base.design_id, revision: ctx.base.revision }
        : null;
    let committed: Design;
    try {
      // The engine holds the session's design; the form and a reopened job replace it there.
      committed = await request("design.replace", { session_id: sid, params: next.design.params, expected });
    } catch (err) {
      if (!latest(sid, seq)) return;
      if (err instanceof EngineError && err.code === DESIGN_CHANGED) {
        await refresh(sid, "The chat changed the design while you were editing. Here it is now; Generate again to replace it.");
        return;
      }
      // Nothing was replaced: keep showing the design the engine holds.
      if (here(sid)) setStatus({ tone: "error", text: `Not replaced: ${err instanceof Error ? err.message : String(err)}` });
      return;
    }
    if (!latest(sid, seq)) return; // a newer design was chosen meanwhile, in that session
    if (here(sid)) pendingStatusRef.current = null;
    onPreview({ ...next, design: committed }, sid);
  };

  const openFile = async (file: File) => {
    const ctx = context();
    const seq = ctx.seq;
    setStatus(null);
    try {
      const text = await file.text();
      if (!latest(ctx.sid, seq)) return;
      if (!file.name.toLowerCase().endsWith(".json")) {
        onPreview({ kind: "file", name: file.name, text }, ctx.sid);
        return;
      }
      // A job record written next to an export: regenerate it and list what changed since.
      const res = await request("job.open", { record: JSON.parse(text) as unknown });
      if (!latest(ctx.sid, seq)) return;
      await showDesign(
        {
          kind: "design",
          design: res.design,
          origin: "job",
          stem: res.stem,
          notes: res.changes.length ? res.changes : undefined,
        },
        ctx,
      );
    } catch (err) {
      if (!latest(ctx.sid, seq) || !here(ctx.sid)) return;
      setStatus({ tone: "error", text: `Couldn't open ${file.name}: ${err instanceof Error ? err.message : String(err)}` });
    }
  };

  const exportFiles = async (filename: string, formats: ExportFormat[], overwrite = false): Promise<void> => {
    const target = design;
    if (!target || exportingRef.current) return;
    if (target.design_id === undefined && target.design_checks?.length) {
      // The engine restarted and no longer holds this design, so its own checks (fit links, groups)
      // can't be run again: saving from the parameters alone would skip them.
      setStatus({
        tone: "error",
        text: "The engine restarted and this design's own checks can't be run again. Generate it again before saving.",
      });
      return;
    }
    const sid = sessionId; // the session this export is for
    const seq = bump(sid); // its place in that session's request order
    exportingRef.current = true;
    setExporting(true);
    try {
      const which =
        target.design_id !== undefined && target.revision !== undefined
          ? { session_id: sid, design_id: target.design_id, revision: target.revision }
          : { params: target.params };
      const res = await request("box.export", {
        ...which,
        filename: filename.trim() || undefined,
        formats,
        overwrite,
      });
      if (!here(sid)) return;
      const moved = res.superseded.length ? ` (earlier files moved to superseded)` : "";
      setStatus({ tone: "info", text: `Saved ${[...res.paths, res.job_path].join(", ")}${moved}` });
      setExportOpen(false);
    } catch (err) {
      if (err instanceof EngineError && err.code === DESIGN_CHANGED) {
        // Even if the new design's event got here first: say so, and show it.
        if (here(sid)) setExportOpen(false);
        await refresh(sid, err.message, seq);
        return;
      }
      if (!here(sid)) return;
      if (err instanceof EngineError && err.code === EXPORT_EXISTS) {
        setStatus({
          tone: "error",
          text: err.message,
          action: { label: "Replace", run: () => void exportFiles(filename, formats, true) },
        });
        return;
      }
      setStatus({ tone: "error", text: err instanceof EngineError ? err.message : String(err) });
    } finally {
      exportingRef.current = false;
      setExporting(false);
    }
  };

  return (
    <section className="@container flex min-w-0 shrink-0 flex-col bg-card" style={{ width }}>
      <header className="flex h-12 shrink-0 items-center gap-1 border-b pr-2 pl-4">
        <h2 className="min-w-0 truncate font-medium text-sm" title={title}>
          {title}
        </h2>
        {source && (
          <Badge variant="outline" className="ml-1.5 font-normal text-muted-foreground">
            {source.kind === "file"
              ? "file"
              : source.origin === "agent"
                ? "from chat"
                : source.origin === "job"
                  ? "from job"
                  : "from form"}
          </Badge>
        )}
        <div className="min-w-2 flex-1" />
        <ToolbarButton
          label="Parameters"
          icon={<SlidersHorizontalIcon />}
          onClick={() =>
            setFormOpen((o) => {
              if (!o) setExportOpen(false);
              return !o;
            })
          }
          aria-expanded={formOpen}
          data-pressed={formOpen ? "" : undefined}
        />
        <ToolbarButton wide label="Open…" icon={<FolderOpenIcon />} onClick={() => fileRef.current?.click()} />
        <ToolbarButton
          label="Export…"
          icon={<DownloadIcon />}
          onClick={() =>
            setExportOpen((o) => {
              if (!o) setFormOpen(false);
              return !o;
            })
          }
          aria-expanded={exportOpen}
          data-pressed={exportOpen && design ? "" : undefined}
          disabled={!design || several}
        />
        <input
          ref={fileRef}
          type="file"
          accept=".cf2,.CF2,.json,text/plain,application/json"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void openFile(f);
            e.target.value = "";
          }}
        />
      </header>

      {several && (
        <div role="tablist" aria-label="Parts" className="flex shrink-0 gap-1 overflow-x-auto border-b px-2 py-1.5">
          {parts.map((p, i) => {
            const selected = p === shown;
            return (
              <button
                key={p.part_id ?? i}
                type="button"
                role="tab"
                aria-selected={selected}
                title={`${p.name} · ${p.params.board}`}
                onClick={() => setPartId(p.part_id ?? null)}
                className={cn(
                  "flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  selected ? "bg-accent font-medium" : "text-muted-foreground hover:bg-accent/50",
                )}
              >
                <span
                  aria-hidden
                  className={cn(
                    "size-1.5 rounded-full",
                    p.overall === "fail" ? "bg-destructive" : p.overall === "warn" ? "bg-warning" : "bg-success",
                  )}
                />
                {p.label ?? `Part ${i + 1}`}
                {(p.quantity ?? 1) > 1 && <span className="text-muted-foreground">× {p.quantity}</span>}
              </button>
            );
          })}
          <span className="ml-auto self-center px-1 text-[11px] text-muted-foreground">
            Export of several parts comes next
          </span>
        </div>
      )}

      {exportOpen && design && (
        <ExportBar
          key={`export-${design.id}`}
          defaultName={(source?.kind === "design" && source.stem) || design.name}
          busy={exporting}
          onExport={(name, formats) => exportFiles(name, formats)}
        />
      )}

      {formOpen && info && (
        <QuickBox
          key={`form-${design?.id ?? "new"}`}
          info={info}
          initial={design?.params}
          context={context}
          onCreated={(d, ctx) => void showDesign({ kind: "design", design: d, origin: "form" }, ctx)}
        />
      )}

      <div className="relative min-h-0 flex-1 overflow-hidden bg-canvas">
        {doc ? (
          <DielineCanvas doc={doc} visible={visible} fitToken={fitToken} onCursor={setCursor} onZoom={setZoom} />
        ) : (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-6 text-center">
            {parsed.error ? (
              <Alert variant="error" className="max-w-sm text-left" controlAlignment="first-line">
                <CircleAlertIcon />
                <AlertTitle>Couldn't read this CF2</AlertTitle>
                <AlertDescription className="wrap-anywhere">{parsed.error}</AlertDescription>
              </Alert>
            ) : (
              <>
                <span className="flex size-11 items-center justify-center rounded-xl border border-dashed text-muted-foreground">
                  <SquareDashedIcon className="size-5" />
                </span>
                <div className="flex flex-col gap-1">
                  <p className="font-medium text-sm">No dieline yet</p>
                  <p className="text-muted-foreground text-sm">
                    Ask in the chat, use Parameters, or open a CF2 or saved job (.dieline.json).
                  </p>
                </div>
              </>
            )}
          </div>
        )}
        {doc && (
          <div className="absolute bottom-3 left-3 flex max-w-[calc(100%-1.5rem)] flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border bg-popover/90 py-1 pr-1 pl-2.5 text-xs shadow-sm backdrop-blur-sm">
            {KINDS.filter((k) => present.has(k)).map((k) => (
              <Label key={k} className="h-6 cursor-pointer gap-1.5 font-normal text-xs sm:text-xs">
                <Checkbox
                  className="sm:size-3.5"
                  checked={visible[k]}
                  onCheckedChange={(checked) => setVisible({ ...visible, [k]: checked })}
                />
                <span className={`swatch ln-${k}`} />
                {KIND_LABELS[k]}
              </Label>
            ))}
            <Tip label="Fit to view (or double-click)">
              <Button variant="ghost" size="icon-xs" onClick={() => setFitToken((n) => n + 1)} aria-label="Fit">
                <ScanIcon />
              </Button>
            </Tip>
          </div>
        )}
      </div>

      {source?.kind === "design" && source.notes && !notesDismissed && (
        <Alert
          variant="warning"
          className="scroll-thin m-3 mb-0 max-h-36 w-auto shrink-0 overflow-y-auto"
          controlAlignment="first-line"
        >
          <TriangleAlertIcon />
          <AlertTitle className="flex items-start justify-between gap-2">
            <span>Reopened with today's values. Changed since this job was exported:</span>
            <button
              type="button"
              className="-mr-1 shrink-0 rounded px-1 text-warning-foreground hover:bg-warning/15"
              aria-label="Dismiss change notes"
              onClick={() => setNotesDismissed(true)}
            >
              ×
            </button>
          </AlertTitle>
          <AlertDescription>
            <ul className="list-disc pl-4">
              {source.notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
            Compare with the exported files before re-issuing them.
          </AlertDescription>
        </Alert>
      )}
      {shown && <Summary design={shown} />}
      {shown && <Checks design={shown} designChecks={design?.design_checks ?? []} />}

      <footer className="flex h-7 shrink-0 items-center gap-4 overflow-hidden whitespace-nowrap border-t px-3 text-[11px] text-muted-foreground tabular-nums">
        {doc && (
          <>
            <span className="flex items-center gap-1.5">
              <RulerIcon className="size-3" />
              Blank {fmtIn(snapInchNoise(mmToIn(doc.bounds.xmax - doc.bounds.xmin)))} ×{" "}
              {fmtIn(snapInchNoise(mmToIn(doc.bounds.ymax - doc.bounds.ymin)))} in
            </span>
            <span>{doc.primitives.length} segments</span>
            <span>{Math.round(zoom * 25.4 * 10) / 10} px/in</span>
            {cursor && (
              <span className="flex items-center gap-1.5">
                <CrosshairIcon className="size-3" />x {mmToIn(cursor.x).toFixed(3)} · y {mmToIn(cursor.y).toFixed(3)} in
              </span>
            )}
            {doc.warnings.length > 0 && (
              <span className="text-warning-foreground">{doc.warnings.length} parse warnings</span>
            )}
            {present.has("other") && (
              <span className="text-warning-foreground" title={JSON.stringify(doc.codes)}>
                unmapped line codes present
              </span>
            )}
          </>
        )}
      </footer>
      {status && (
        // Its own wrapping row, so long paths and errors are never squeezed out by the readouts.
        <div
          role={status.tone === "error" ? "alert" : "status"}
          className={cn(
            "flex shrink-0 items-start gap-2 border-t px-3 py-1.5 text-xs wrap-anywhere",
            status.tone === "error" ? "text-destructive-foreground" : "text-muted-foreground",
          )}
        >
          <span className="min-w-0 flex-1 select-text">{status.text}</span>
          {status.action && (
            <Button size="xs" variant="outline" className="shrink-0" disabled={exporting} onClick={status.action.run}>
              {exporting && <Spinner />}
              {status.action.label}
            </Button>
          )}
          <button
            type="button"
            className="shrink-0 rounded px-1 hover:bg-accent"
            aria-label="Dismiss"
            onClick={() => setStatus(null)}
          >
            ×
          </button>
        </div>
      )}
    </section>
  );
}

/** Icon + label; collapses to icon-only (label in a tooltip) when the pane is narrow. `wide` collapses sooner. */
function ToolbarButton({
  label,
  icon,
  wide = false,
  ...props
}: { label: string; icon: React.ReactNode; wide?: boolean } & React.ComponentProps<typeof Button>) {
  return (
    <Tip label={label}>
      <Button
        variant="ghost"
        size="sm"
        aria-label={label}
        className={cn("px-2", wide ? "@[48rem]:px-2.5" : "@[34rem]:px-2.5")}
        {...props}
      >
        {icon}
        <span className={cn("hidden", wide ? "@[48rem]:inline" : "@[34rem]:inline")}>{label}</span>
      </Button>
    </Tip>
  );
}

function ExportBar({
  defaultName,
  busy,
  onExport,
}: {
  defaultName: string;
  busy: boolean;
  onExport: (filename: string, formats: ExportFormat[]) => Promise<void>;
}) {
  const uid = useId();
  const [name, setName] = useState(defaultName);
  const [formats, setFormats] = useState<Record<ExportFormat, boolean>>(() => {
    const saved = loadString("export-formats", "cf2").split(",");
    const cf2 = saved.includes("cf2");
    const dxf = saved.includes("dxf");
    return cf2 || dxf ? { cf2, dxf } : { cf2: true, dxf: false };
  });
  const chosen = (Object.keys(formats) as ExportFormat[]).filter((f) => formats[f]);
  const toggle = (f: ExportFormat) => (checked: boolean) => {
    const next = { ...formats, [f]: checked };
    setFormats(next);
    const list = (Object.keys(next) as ExportFormat[]).filter((k) => next[k]);
    if (list.length) saveString("export-formats", list.join(","));
  };
  return (
    <form
      className="flex shrink-0 flex-wrap items-end gap-x-3 gap-y-2 border-b bg-background/50 p-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (busy || !chosen.length) return;
        void onExport(name, chosen);
      }}
    >
      <div className="flex min-w-40 flex-1 flex-col gap-1.5">
        <Label htmlFor={`${uid}-name`} className="font-normal text-muted-foreground text-xs sm:text-xs">
          File name (e.g. RL26-0771)
        </Label>
        <Input size="sm" id={`${uid}-name`} value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
      </div>
      <Label className="h-8 cursor-pointer gap-1.5 font-normal text-xs sm:text-xs">
        <Checkbox checked={formats.cf2} onCheckedChange={toggle("cf2")} />
        CF2 (ArtiosCAD)
      </Label>
      <Label className="h-8 cursor-pointer gap-1.5 font-normal text-xs sm:text-xs">
        <Checkbox checked={formats.dxf} onCheckedChange={toggle("dxf")} />
        DXF (sample table)
      </Label>
      <Button type="submit" size="sm" disabled={busy || !chosen.length}>
        {busy && <Spinner />}
        Save
      </Button>
    </form>
  );
}

const BOARD_SOURCE_LABEL = { artioscad: "", derived: " (inferred)", nominal: " (nominal)" };

function Summary({ design }: { design: Design }) {
  const s = design.summary;
  if (!s.panels_in) return null;
  const rows: [string, string][] = [
    ["Inside", `${fmtIn(s.inside_in.length)} × ${fmtIn(s.inside_in.width)} × ${fmtIn(s.inside_in.depth)}`],
    ["Board", `${s.board} · CAL ${fmtIn(s.cal_in)} · IL ${fmtIn(s.il_in)}${BOARD_SOURCE_LABEL[s.board_source] ?? ""}`],
    ["Panels", s.panels_in.map(fmtIn).join(" · ")],
    ["Glue lug", fmtIn(s.glue_lug_in)],
    ["Body", fmtIn(s.body_height_in)],
  ];
  // An HSC tray's one flap row is the bottom of the folded box (the drawing has it at the top).
  const tray = s.options?.use === "tray";
  if (s.options?.use !== undefined) rows.push(["Use", tray ? "Tray (flaps down)" : "Lid (flaps up)"]);
  if (s.top_flaps_in) rows.push([tray ? "Bottom flaps" : "Top flaps", s.top_flaps_in.map(fmtIn).join(" / ")]);
  if (s.bottom_flaps_in) rows.push(["Bottom flaps", s.bottom_flaps_in.map(fmtIn).join(" / ")]);
  rows.push(["Blank", `${fmtIn(s.blank_in.width)} × ${fmtIn(s.blank_in.height)}`]);
  if (s.blank_area_sqft !== undefined) rows.push(["Area", `${s.blank_area_sqft.toFixed(2)} sq ft`]);
  if (s.rule_length_in)
    rows.push([
      "Rule",
      `cut ${s.rule_length_in.cut.toFixed(1)} in · crease ${s.rule_length_in.crease.toFixed(1)} in`,
    ]);
  return (
    <Collapsible className="shrink-0 border-t">
      <CollapsibleTrigger className="group flex h-9 w-full items-center gap-2 px-3 text-left text-xs outline-none hover:bg-accent/40 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset">
        <span className="font-medium">Sizes</span>
        <span className="min-w-0 truncate text-muted-foreground">
          {s.standard} · blank {fmtIn(s.blank_in.width)} × {fmtIn(s.blank_in.height)} in
        </span>
        <span className="flex-1" />
        <ChevronRightIcon className="size-3.5 text-muted-foreground transition-transform group-data-panel-open:rotate-90" />
      </CollapsibleTrigger>
      <CollapsiblePanel>
        <div className="scroll-thin max-h-52 overflow-y-auto">
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 px-3 pt-0.5 pb-3 text-xs tabular-nums">
            {rows.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-muted-foreground">{k}</dt>
                <dd className="min-w-0 wrap-anywhere">{v}</dd>
              </div>
            ))}
          </dl>
          <p className="px-3 pb-3 text-[11px] text-muted-foreground">
            Panels listed 1 → 4 (panel 1 at the free edge); inches.
          </p>
        </div>
      </CollapsiblePanel>
    </Collapsible>
  );
}

const CHECK_ICON: Record<CheckStatus, React.ReactNode> = {
  pass: <CircleCheckIcon className="size-3.5 shrink-0 text-success" />,
  warn: <TriangleAlertIcon className="size-3.5 shrink-0 text-warning" />,
  fail: <CircleXIcon className="size-3.5 shrink-0 text-destructive" />,
  stale: <CircleDashedIcon className="size-3.5 shrink-0 text-destructive" />,
  unavailable: <CircleHelpIcon className="size-3.5 shrink-0 text-warning" />,
  info: <InfoIcon className="size-3.5 shrink-0 text-muted-foreground" />,
  not_applicable: <CircleMinusIcon className="size-3.5 shrink-0 text-muted-foreground" />,
};

const CHECK_LABEL: Record<CheckStatus, string> = {
  pass: "Pass",
  warn: "Warning",
  fail: "Fail",
  stale: "Stale",
  unavailable: "Couldn't check",
  info: "Note",
  not_applicable: "Not applicable",
};

/** Statuses whose meaning the icon alone doesn't carry: their label is shown, not only read out. */
const SHOWN_LABEL: ReadonlySet<CheckStatus> = new Set(["stale", "unavailable", "not_applicable"]);

/** Statuses that need no attention: they don't count against "n/m pass". */
const QUIET: ReadonlySet<CheckStatus> = new Set(["pass", "info", "not_applicable"]);

function Checks({ design, designChecks }: { design: Design; designChecks: CheckResult[] }) {
  // The part's checks, then the design's own (fit links, groups), which block export too.
  const checks = [...designChecks, ...design.checks];
  const overall: Design["overall"] = designChecks.some((c) => c.status === "fail" || c.status === "stale")
    ? "fail"
    : design.overall;
  const [open, setOpen] = useState(overall === "fail");
  useEffect(() => {
    if (overall === "fail") setOpen(true);
  }, [design.id, overall]);
  // info and not_applicable don't take part; everything else either passed or needs a look.
  const counted = checks.filter((c) => c.status !== "info" && c.status !== "not_applicable");
  const passed = counted.filter((c) => c.status === "pass").length;
  return (
    <Collapsible className="shrink-0 border-t" open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="group flex h-9 w-full items-center gap-2 px-3 text-left text-xs outline-none hover:bg-accent/40 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset">
        <Badge variant={STATUS_BADGE[overall]} className="uppercase tracking-wide">
          {overall}
        </Badge>
        <span className="font-medium">Checks</span>
        <span className="text-muted-foreground">
          {passed}/{counted.length} pass
        </span>
        <span className="flex-1" />
        <ChevronRightIcon className="size-3.5 text-muted-foreground transition-transform group-data-panel-open:rotate-90" />
      </CollapsibleTrigger>
      <CollapsiblePanel>
        <ul className="scroll-thin flex max-h-56 flex-col gap-1.5 overflow-y-auto px-3 pt-0.5 pb-3">
          {checks.map((c) => (
            <li key={c.id} className="flex items-start gap-2 text-xs leading-snug">
              <span className="flex h-[1lh] items-center" aria-hidden>
                {CHECK_ICON[c.status]}
              </span>
              {SHOWN_LABEL.has(c.status) ? null : <span className="sr-only">{CHECK_LABEL[c.status]}: </span>}
              <span
                className={cn(
                  "min-w-0 wrap-anywhere",
                  QUIET.has(c.status) ? "text-muted-foreground" : "text-foreground",
                )}
              >
                {SHOWN_LABEL.has(c.status) ? <span className="font-medium">{CHECK_LABEL[c.status]}: </span> : null}
                {c.message}
              </span>
            </li>
          ))}
        </ul>
      </CollapsiblePanel>
    </Collapsible>
  );
}

function Field({
  label,
  id,
  className,
  children,
}: {
  label: string;
  id: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)}>
      <Label htmlFor={id} className="font-normal text-muted-foreground text-xs sm:text-xs">
        {label}
      </Label>
      {children}
    </div>
  );
}

function defaultOptions(style: StyleInfo | undefined): Record<string, OptionValue> {
  return Object.fromEntries((style?.options ?? []).map((o) => [o.id, o.default]));
}

function QuickBox(props: {
  info: EngineInfo;
  initial?: BoxParams;
  /** Where the form was submitted from: captured when the designer presses Generate. */
  context: () => ShowContext;
  onCreated: (d: Awaited<ReturnType<typeof request<"box.create">>>, ctx: ShowContext) => void;
}) {
  const uid = useId();
  const fid = (name: string) => `${uid}-${name}`;
  const [error, setError] = useState<string | null>(null);
  const { info } = props;
  const [p, setP] = useState<BoxParams>(() => {
    if (props.initial) {
      const normalizedBoard = props.initial.board.trim().toUpperCase().replace(/\s+/g, " ");
      const matchedBoard = info.boards.find((b) => b.id === normalizedBoard)?.id ?? props.initial.board;
      return { ...props.initial, board: matchedBoard };
    }
    const style = info.styles[0];
    return {
      style: style?.id ?? "",
      length: 14,
      width: 8,
      depth: 6,
      units: "in",
      board: info.boards.find((b) => b.id === info.default_board)?.id ?? info.boards[0]?.id ?? "",
      options: defaultOptions(style),
    };
  });
  const style = info.styles.find((s) => s.id === p.style);
  const options = { ...defaultOptions(style), ...(p.options ?? {}) };
  const [busy, setBusy] = useState(false);
  // Dimensions are edited as text so fractions ("14 1/8") and partly typed values stay representable.
  const show = (v: number | string) => (typeof v === "number" && p.units === "in" ? fmtExactIn(v) : String(v));
  const [dims, setDims] = useState({ length: show(p.length), width: show(p.width), depth: show(p.depth) });
  // Number options (the gap/lap values) are edited as text too.
  const [numText, setNumText] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      Object.entries(options)
        .filter(([, v]) => typeof v === "number")
        .map(([k, v]) => [k, fmtExactIn(v as number)]),
    ),
  );
  const num = (k: keyof typeof dims) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setDims({ ...dims, [k]: e.target.value });
  // A number option is shown only while it applies: top_lap with Gap/Lap top flaps, etc.
  const numberShown = (id: string) => {
    if (id === "top_lap") return options.top_flaps === "gap_lap";
    if (id === "bottom_lap") return options.bottom_flaps === "gap_lap";
    return true;
  };
  const switchUnits = (units: "in" | "mm") => {
    if (units === p.units) return;
    // Convert what's typed, so switching units never silently reinterprets a size.
    setDims({
      length: convertLengthText(dims.length, units),
      width: convertLengthText(dims.width, units),
      depth: convertLengthText(dims.depth, units),
    });
    setP({ ...p, units });
  };

  const parsed = (): BoxParams | string => {
    const out: BoxParams = { ...p, options: { ...options } };
    for (const k of ["length", "width", "depth"] as const) {
      const v = parseLength(dims[k]);
      if (!Number.isFinite(v) || v <= 0)
        return `${k[0].toUpperCase()}${k.slice(1)} must be a number above 0, e.g. 14, 14.125 or 14 1/8.`;
      out[k] = v;
    }
    for (const o of style?.options ?? []) {
      if (o.choices) continue;
      if (!numberShown(o.id)) {
        delete out.options![o.id]; // not in use: the standard's default applies
        continue;
      }
      const v = parseLength(numText[o.id] ?? String(o.default));
      if (!Number.isFinite(v)) return `${o.label} must be a number of inches.`;
      out.options![o.id] = v;
    }
    // Only send options the chosen style has.
    out.options = Object.fromEntries(
      Object.entries(out.options!).filter(([id]) => style?.options.some((o) => o.id === id)),
    );
    return out;
  };

  const styleItems = info.styles.map((s) => ({ value: s.id, label: s.name }));
  const boardItems = info.boards.map((b) => ({
    value: b.id,
    label: `${b.id} · CAL ${fmtIn(b.cal_in)}${BOARD_SOURCE_LABEL[b.source]}`,
  }));
  const unitItems = [
    { value: "in", label: "in" },
    { value: "mm", label: "mm" },
  ];
  return (
    <form
      noValidate
      className="scroll-thin grid max-h-[40%] shrink-0 grid-cols-12 gap-x-2 gap-y-3 overflow-y-auto border-b bg-background/50 p-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (busy) return;
        const params = parsed();
        if (typeof params === "string") return setError(params);
        setError(null);
        setP(params);
        setBusy(true);
        const ctx = props.context(); // the session and design the designer is looking at now
        request("box.create", params)
          .then((d) => props.onCreated(d, ctx))
          .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
          .finally(() => setBusy(false));
      }}
    >
      <Field label="Style" id={fid("style")} className="col-span-12 @[30rem]:col-span-6">
        <Select
          items={styleItems}
          value={p.style}
          onValueChange={(v) => {
            if (v === null) return;
            const next = info.styles.find((s) => s.id === v);
            const nextOpts = { ...defaultOptions(next), ...pick(options, next) };
            setP({ ...p, style: v, options: nextOpts });
            setNumText((prev) =>
              Object.fromEntries(
                (next?.options ?? [])
                  .filter((o) => !o.choices)
                  .map((o) => [o.id, prev[o.id] ?? fmtExactIn(Number(nextOpts[o.id] ?? o.default))]),
              ),
            );
            setError(null);
          }}
        >
          <SelectTrigger size="sm" className="min-w-0" id={fid("style")}>
            <SelectValue />
          </SelectTrigger>
          <SelectPopup>
            {styleItems.map((s) => (
              <SelectItem key={s.value} value={s.value}>
                {s.label}
              </SelectItem>
            ))}
          </SelectPopup>
        </Select>
      </Field>
      <Field label="Board" id={fid("board")} className="col-span-12 @[30rem]:col-span-6">
        <Select items={boardItems} value={p.board} onValueChange={(v) => v !== null && setP({ ...p, board: v })}>
          <SelectTrigger size="sm" className="min-w-0" id={fid("board")}>
            <SelectValue />
          </SelectTrigger>
          <SelectPopup>
            {boardItems.map((b) => (
              <SelectItem key={b.value} value={b.value}>
                {b.label}
              </SelectItem>
            ))}
          </SelectPopup>
        </Select>
      </Field>
      <Field label="Inside length" id={fid("length")} className="col-span-6 @[30rem]:col-span-2">
        <Input size="sm" id={fid("length")} type="text" autoComplete="off" value={dims.length} onChange={num("length")} />
      </Field>
      <Field label="Inside width" id={fid("width")} className="col-span-6 @[30rem]:col-span-2">
        <Input size="sm" id={fid("width")} type="text" autoComplete="off" value={dims.width} onChange={num("width")} />
      </Field>
      <Field label="Inside depth" id={fid("depth")} className="col-span-6 @[30rem]:col-span-2">
        <Input size="sm" id={fid("depth")} type="text" autoComplete="off" value={dims.depth} onChange={num("depth")} />
      </Field>
      <Field label="Units" id={fid("units")} className="col-span-6 @[30rem]:col-span-2">
        <Select
          items={unitItems}
          value={p.units}
          onValueChange={(v) => v !== null && switchUnits(v as "mm" | "in")}
        >
          <SelectTrigger size="sm" className="min-w-0" id={fid("units")}>
            <SelectValue />
          </SelectTrigger>
          <SelectPopup>
            {unitItems.map((u) => (
              <SelectItem key={u.value} value={u.value}>
                {u.label}
              </SelectItem>
            ))}
          </SelectPopup>
        </Select>
      </Field>
      <div className="col-span-12 flex items-end justify-end @[30rem]:col-span-4">
        <Button type="submit" size="sm" disabled={busy} className="w-full @[30rem]:w-auto">
          {busy && <Spinner />}
          Generate
        </Button>
      </div>
      {style?.options
        .filter((o) => o.choices)
        .map((o) => {
          const items = o.choices!.map((c) => ({ value: c.value, label: c.label }));
          return (
            <Field key={o.id} label={o.label} id={fid(o.id)} className="col-span-6 @[30rem]:col-span-3">
              <Select
                items={items}
                value={String(options[o.id])}
                onValueChange={(v) => v !== null && setP({ ...p, options: { ...options, [o.id]: v } })}
              >
                <SelectTrigger size="sm" className="min-w-0" id={fid(o.id)} title={o.description}>
                  <SelectValue />
                </SelectTrigger>
                <SelectPopup>
                  {items.map((c) => (
                    <SelectItem key={c.value} value={c.value}>
                      {c.label}
                    </SelectItem>
                  ))}
                </SelectPopup>
              </Select>
            </Field>
          );
        })}
      {style?.options
        .filter((o) => !o.choices && numberShown(o.id))
        .map((o) => (
          <Field key={o.id} label={`${o.label} (in)`} id={fid(o.id)} className="col-span-6 @[30rem]:col-span-3">
            <Input
              size="sm"
              id={fid(o.id)}
              type="text"
              autoComplete="off"
              title={o.description}
              value={numText[o.id] ?? fmtIn(Number(o.default))}
              onChange={(e) => setNumText({ ...numText, [o.id]: e.target.value })}
            />
          </Field>
        ))}
      {error && (
        <p role="alert" className="col-span-12 text-destructive-foreground text-xs wrap-anywhere">
          {error}
        </p>
      )}
    </form>
  );
}

/** The current option values the next style also has. */
function pick(options: Record<string, OptionValue>, style: StyleInfo | undefined): Record<string, OptionValue> {
  return Object.fromEntries(Object.entries(options).filter(([id]) => style?.options.some((o) => o.id === id)));
}
