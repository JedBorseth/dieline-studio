import {
  ChevronRightIcon,
  CircleAlertIcon,
  CircleCheckIcon,
  CircleXIcon,
  CrosshairIcon,
  DownloadIcon,
  FolderOpenIcon,
  RulerIcon,
  ScanIcon,
  SlidersHorizontalIcon,
  SquareDashedIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { useMemo, useRef, useState } from "react";

import { type Cf2Doc, type LineKind, parseCf2 } from "../lib/cf2";
import { EngineError, request } from "../lib/engine";
import type { BoxParams, CheckStatus, EngineInfo } from "../lib/protocol";
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
    <section className="@container flex min-w-0 shrink-0 flex-col bg-card" style={{ width }}>
      <header className="flex h-12 shrink-0 items-center gap-1 border-b pr-2 pl-4">
        <h2 className="min-w-0 truncate font-medium text-sm" title={title}>
          {title}
        </h2>
        {source && (
          <Badge variant="outline" className="ml-1.5 font-normal text-muted-foreground">
            {source.kind === "file" ? "file" : source.origin === "agent" ? "from chat" : "from form"}
          </Badge>
        )}
        <div className="min-w-2 flex-1" />
        <ToolbarButton
          label="Parameters"
          icon={<SlidersHorizontalIcon />}
          onClick={() => setFormOpen((o) => !o)}
          aria-expanded={formOpen}
          data-pressed={formOpen ? "" : undefined}
        />
        <ToolbarButton wide label="Open CF2…" icon={<FolderOpenIcon />} onClick={() => fileRef.current?.click()} />
        <ToolbarButton
          label="Export CF2"
          icon={<DownloadIcon />}
          onClick={() => void exportCf2()}
          disabled={!design}
        />
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

      <div className="relative min-h-0 flex-1 bg-canvas">
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
                  <p className="text-muted-foreground text-sm">Ask in the chat, use Parameters, or open a CF2 file.</p>
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

      {design && <Checks key={design.id} design={design} />}

      <footer className="flex h-7 shrink-0 items-center gap-4 overflow-hidden whitespace-nowrap border-t px-3 text-[11px] text-muted-foreground tabular-nums">
        {doc && (
          <>
            <span className="flex items-center gap-1.5">
              <RulerIcon className="size-3" />
              Blank {(doc.bounds.xmax - doc.bounds.xmin).toFixed(1)} × {(doc.bounds.ymax - doc.bounds.ymin).toFixed(1)} mm
            </span>
            <span>{doc.primitives.length} segments</span>
            <span>{Math.round(zoom * 100) / 100} px/mm</span>
            {cursor && (
              <span className="flex items-center gap-1.5">
                <CrosshairIcon className="size-3" />x {cursor.x.toFixed(1)} · y {cursor.y.toFixed(1)}
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
        {status && (
          <span className={cn("min-w-0 truncate", status.tone === "error" && "text-destructive-foreground")}>
            {status.text}
          </span>
        )}
      </footer>
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

const CHECK_ICON: Record<CheckStatus, React.ReactNode> = {
  pass: <CircleCheckIcon className="size-3.5 shrink-0 text-success" />,
  warn: <TriangleAlertIcon className="size-3.5 shrink-0 text-warning" />,
  fail: <CircleXIcon className="size-3.5 shrink-0 text-destructive" />,
};

const CHECK_LABEL: Record<string, string> = { pass: "Pass", warn: "Warning", fail: "Fail" };

function Checks({ design }: { design: NonNullable<Extract<PreviewSource, { kind: "design" }>["design"]> }) {
  const failing = design.checks.filter((c) => c.status !== "pass");
  return (
    <Collapsible className="shrink-0 border-t" defaultOpen={design.overall === "fail"}>
      <CollapsibleTrigger className="group flex h-9 w-full items-center gap-2 px-3 text-left text-xs outline-none hover:bg-accent/40 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset">
        <Badge variant={STATUS_BADGE[design.overall]} className="uppercase tracking-wide">
          {design.overall}
        </Badge>
        <span className="font-medium">Checks</span>
        <span className="text-muted-foreground">
          {design.checks.length - failing.length}/{design.checks.length} pass
        </span>
        <span className="flex-1" />
        <ChevronRightIcon className="size-3.5 text-muted-foreground transition-transform group-data-panel-open:rotate-90" />
      </CollapsibleTrigger>
      <CollapsiblePanel>
        <ul className="scroll-thin flex max-h-56 flex-col gap-1.5 overflow-y-auto px-3 pt-0.5 pb-3">
          {design.checks.map((c) => (
            <li key={c.id} className="flex items-start gap-2 text-xs leading-snug">
              <span className="flex h-[1lh] items-center" aria-hidden>
                {CHECK_ICON[c.status]}
              </span>
              <span className="sr-only">{CHECK_LABEL[c.status]}: </span>
              <span
                className={cn(
                  "min-w-0 wrap-anywhere",
                  c.status === "pass" ? "text-muted-foreground" : "text-foreground",
                )}
              >
                {c.message}
              </span>
            </li>
          ))}
        </ul>
      </CollapsiblePanel>
    </Collapsible>
  );
}

function Field({ label, className, children }: { label: string; className?: string; children: React.ReactNode }) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)}>
      <Label className="font-normal text-muted-foreground text-xs sm:text-xs">{label}</Label>
      {children}
    </div>
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
  // Dimensions are edited as text so an empty or partly typed field stays representable.
  const [dims, setDims] = useState({ length: String(p.length), width: String(p.width), depth: String(p.depth) });
  const num = (k: keyof typeof dims) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setDims({ ...dims, [k]: e.target.value });

  const parsed = (): BoxParams | string => {
    const out = { ...p };
    for (const k of ["length", "width", "depth"] as const) {
      const v = Number(dims[k]);
      if (dims[k].trim() === "" || !Number.isFinite(v) || v <= 0 || v > 3000)
        return `${k[0].toUpperCase()}${k.slice(1)} must be a number above 0 and at most 3000.`;
      out[k] = v;
    }
    return out;
  };

  const styleItems = info.styles.map((s) => ({ value: s.id, label: s.name }));
  const boardItems = info.boards.map((b) => ({ value: b.id, label: `${b.name} (${b.caliper_mm} mm)` }));
  const unitItems = [
    { value: "mm", label: "mm" },
    { value: "in", label: "in" },
  ];

  return (
    <form
      className="grid shrink-0 grid-cols-12 gap-x-2 gap-y-3 border-b bg-background/50 p-3"
      onSubmit={(e) => {
        e.preventDefault();
        const params = parsed();
        if (typeof params === "string") return props.onError(params);
        setP(params);
        setBusy(true);
        request("box.create", params)
          .then(props.onCreated)
          .catch((err: unknown) => props.onError(err instanceof Error ? err.message : String(err)))
          .finally(() => setBusy(false));
      }}
    >
      <Field label="Style" className="col-span-12 @[30rem]:col-span-6">
        <Select items={styleItems} value={p.style} onValueChange={(v) => v !== null && setP({ ...p, style: v })}>
          <SelectTrigger size="sm" className="min-w-0" aria-label="Style">
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
      <Field label="Board" className="col-span-12 @[30rem]:col-span-6">
        <Select items={boardItems} value={p.board} onValueChange={(v) => v !== null && setP({ ...p, board: v })}>
          <SelectTrigger size="sm" className="min-w-0" aria-label="Board">
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
      <Field label="Length" className="col-span-3 @[30rem]:col-span-2">
        <Input size="sm" type="number" step="any" inputMode="decimal" value={dims.length} onChange={num("length")} aria-label="Length" />
      </Field>
      <Field label="Width" className="col-span-3 @[30rem]:col-span-2">
        <Input size="sm" type="number" step="any" inputMode="decimal" value={dims.width} onChange={num("width")} aria-label="Width" />
      </Field>
      <Field label="Depth" className="col-span-3 @[30rem]:col-span-2">
        <Input size="sm" type="number" step="any" inputMode="decimal" value={dims.depth} onChange={num("depth")} aria-label="Depth" />
      </Field>
      <Field label="Units" className="col-span-3 @[30rem]:col-span-2">
        <Select
          items={unitItems}
          value={p.units}
          onValueChange={(v) => v !== null && setP({ ...p, units: v as "mm" | "in" })}
        >
          <SelectTrigger size="sm" className="min-w-0" aria-label="Units">
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
    </form>
  );
}
