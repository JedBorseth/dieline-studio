import {
  ArrowUpIcon,
  BrainIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CircleAlertIcon,
  CircleCheckIcon,
  CircleXIcon,
  CpuIcon,
  DiamondIcon,
  InfoIcon,
  KeyRoundIcon,
  SquareIcon,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";

import type { CheckStatus, Settings } from "../lib/protocol";
import type { ChatItem, Session } from "../lib/store";
import { cn } from "../lib/utils";
import { Tip } from "./Tip";
import { Alert, AlertDescription, AlertTitle } from "./ui/alert";
import { Badge } from "./ui/badge";
import { Button, InlineButton } from "./ui/button";
import { Collapsible, CollapsiblePanel, CollapsibleTrigger } from "./ui/collapsible";
import { Spinner } from "./ui/spinner";
import { Textarea } from "./ui/textarea";

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

export const STATUS_BADGE: Record<CheckStatus, "success" | "warning" | "error"> = {
  pass: "success",
  warn: "warning",
  fail: "error",
};

export function ChatPanel({ session, settings, engineError, onSend, onStop, onOpenSettings }: Props) {
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [session.items]);

  const modelLabel = settings?.models.find((m) => m.id === settings.model)?.label ?? settings?.model;

  return (
    <section className="flex min-w-0 flex-1 flex-col bg-background">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b px-5">
        <h1 className="truncate font-medium text-sm">{session.title}</h1>
        {session.busy && <Spinner size="sm" tone="muted" />}
      </header>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto" ref={listRef}>
        <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col gap-4 px-5 py-6">
          {engineError && (
            <Alert variant="error" controlAlignment="first-line">
              <CircleAlertIcon />
              <AlertTitle>Can't reach the engine</AlertTitle>
              <AlertDescription>
                <span className="wrap-anywhere">{engineError}</span>
                <span className="text-xs">
                  Desktop app: the sidecar failed to start. Browser dev: run the Vite dev server.
                </span>
              </AlertDescription>
            </Alert>
          )}
          {settings && !settings.has_api_key && (
            <Alert variant="info" controlAlignment="first-line">
              <KeyRoundIcon />
              <AlertDescription className="block">
                Chat needs an Anthropic API key.{" "}
                <InlineButton className="text-primary [--inline-button-text-align:start] [--inline-button-white-space:normal]" onClick={onOpenSettings}>
                  Add one in Settings
                </InlineButton>
                . The parameter form in the preview pane works without one.
              </AlertDescription>
            </Alert>
          )}
          {session.items.length === 0 ? (
            <EmptyState busy={session.busy} onSend={onSend} />
          ) : (
            session.items.map((item) => <Item key={item.id} item={item} />)
          )}
          {session.busy && (
            <div className="flex items-center gap-2 text-muted-foreground text-sm">
              <Spinner size="sm" />
              Working…
            </div>
          )}
        </div>
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

function EmptyState({ busy, onSend }: { busy: boolean; onSend: (text: string) => void }) {
  return (
    <div className="my-auto flex flex-col items-center gap-5 py-10 text-center">
      <span className="flex size-11 items-center justify-center rounded-xl border bg-card text-primary shadow-xs">
        <DiamondIcon className="size-5" />
      </span>
      <div className="flex flex-col gap-1.5">
        <h2 className="font-semibold text-lg tracking-tight">Describe the box</h2>
        <p className="max-w-md text-muted-foreground text-sm">
          The engine draws the dieline; Claude only picks the parameters.
        </p>
      </div>
      <div className="flex max-w-xl flex-wrap justify-center gap-2">
        {SUGGESTIONS.map((s) => (
          <Button
            key={s}
            variant="outline"
            size="sm-multiline"
            className="max-w-full rounded-[1.25rem] font-normal before:rounded-[calc(1.25rem-1px)]"
            onClick={() => onSend(s)}
            disabled={busy}
          >
            {s}
          </Button>
        ))}
      </div>
    </div>
  );
}

function Item({ item }: { item: ChatItem }) {
  switch (item.kind) {
    case "user":
      return (
        <div className="max-w-[80%] self-end whitespace-pre-wrap rounded-2xl rounded-br-md bg-secondary px-3.5 py-2 wrap-anywhere">
          {item.text}
        </div>
      );
    case "assistant":
      return (
        <div className="flex max-w-full flex-col gap-2">
          {item.thinking && (
            <Collapsible>
              <CollapsibleTrigger className="group flex items-center gap-1.5 rounded-md text-muted-foreground text-xs outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
                <BrainIcon className="size-3.5" />
                Reasoning
                <ChevronRightIcon className="size-3.5 transition-transform group-data-panel-open:rotate-90" />
              </CollapsibleTrigger>
              <CollapsiblePanel>
                <div className="mt-2 whitespace-pre-wrap border-l-2 pl-3 text-muted-foreground text-xs leading-relaxed">
                  {item.thinking}
                </div>
              </CollapsiblePanel>
            </Collapsible>
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
      return (
        <Alert variant={item.tone === "error" ? "error" : "info"} controlAlignment="first-line">
          {item.tone === "error" ? <CircleAlertIcon /> : <InfoIcon />}
          <AlertDescription className="block wrap-anywhere">{item.text}</AlertDescription>
        </Alert>
      );
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
  const overall = (item.result as { overall?: CheckStatus } | undefined)?.overall;
  const error = item.status === "error" ? (item.result as { error?: string } | undefined)?.error : undefined;
  return (
    <Collapsible
      className={cn(
        "rounded-lg border bg-card text-sm shadow-xs/5",
        item.status === "error" && "border-destructive/30",
      )}
    >
      <CollapsibleTrigger className="group flex h-9 w-full items-center gap-2.5 rounded-lg px-3 text-left outline-none hover:bg-accent/40 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset">
        {item.status === "running" ? (
          <Spinner size="sm" className="text-primary" />
        ) : item.status === "error" ? (
          <CircleXIcon className="size-3.5 shrink-0 text-destructive" />
        ) : (
          <CircleCheckIcon className="size-3.5 shrink-0 text-success" />
        )}
        <span className="min-w-0 flex-1 truncate font-mono text-xs">{label}</span>
        {overall && (
          <Badge variant={STATUS_BADGE[overall]} className="uppercase tracking-wide">
            {overall}
          </Badge>
        )}
        <ChevronRightIcon className="size-3.5 shrink-0 text-muted-foreground transition-transform group-data-panel-open:rotate-90" />
      </CollapsibleTrigger>
      <CollapsiblePanel>
        {error && <div className="px-3 pb-2 text-destructive-foreground text-xs">{error}</div>}
        {item.result !== undefined && (
          <pre className="scroll-thin m-0 max-h-60 overflow-auto border-t px-3 py-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
            {JSON.stringify(item.result, null, 2)}
          </pre>
        )}
      </CollapsiblePanel>
    </Collapsible>
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

  const submit = () => {
    const t = text.trim();
    if (!t || props.busy) return;
    props.onSend(t);
    setText("");
  };

  return (
    <div className="mx-auto w-full max-w-3xl shrink-0 px-5 pb-5">
      <div className="@container rounded-2xl border bg-card shadow-sm transition-[border-color,box-shadow] focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/15 dark:shadow-black/30">
        {/* field-sizing: content grows the textarea with its text, up to max-h-64. */}
        <Textarea
          unstyled
          className="block px-1.5 pt-2 [&_textarea]:resize-none [&_textarea]:placeholder:text-placeholder"
          value={text}
          placeholder="Describe a box, or ask for a change…"
          rows={1}
          aria-label="Message"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="flex items-center gap-2 px-2 pt-1 pb-2">
          <Tip label="Model and effort">
            <Button
              variant="ghost-muted"
              size="xs"
              className="min-w-0 shrink rounded-full before:rounded-full"
              onClick={props.onModelClick}
            >
              <CpuIcon />
              <span className="truncate">{props.modelLabel}</span>
              <ChevronDownIcon className="opacity-60" />
            </Button>
          </Tip>
          <span className="hidden min-w-0 flex-1 truncate text-right text-muted-foreground text-xs @md:inline">
            Enter to send · Shift+Enter for a new line
          </span>
          <span className="flex-1 @md:hidden" />
          {props.busy ? (
            <Tip label="Stop">
              <Button
                variant="destructive"
                size="icon-sm-pill"
                onClick={props.onStop}
                aria-label="Stop"
              >
                <SquareIcon className="size-3 fill-current" />
              </Button>
            </Tip>
          ) : (
            <Button size="icon-sm-pill" onClick={submit} disabled={!text.trim()} aria-label="Send">
              <ArrowUpIcon />
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
