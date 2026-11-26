import { DiamondIcon, PlusIcon, SettingsIcon } from "lucide-react";

import type { OverallStatus } from "../lib/protocol";
import type { Session } from "../lib/store";
import { cn } from "../lib/utils";
import { Tip } from "./Tip";
import { Button } from "./ui/button";
import { ScrollArea } from "./ui/scroll-area";
import { Spinner } from "./ui/spinner";

interface Props {
  sessions: Session[];
  activeId: string;
  onNew: () => void;
  onSelect: (id: string) => void;
  onSettings: () => void;
  engineReady: boolean;
  engineVersion?: string;
}

const DOT: Record<OverallStatus, string> = {
  pass: "bg-success",
  warn: "bg-warning",
  fail: "bg-destructive",
};

export function Sidebar({ sessions, activeId, onNew, onSelect, onSettings, engineReady, engineVersion }: Props) {
  return (
    <aside className="flex w-60 shrink-0 flex-col border-r bg-sidebar">
      <div className="flex h-12 shrink-0 items-center gap-2.5 px-4">
        <span
          className="flex size-6 items-center justify-center rounded-md bg-primary text-primary-foreground"
          aria-hidden
        >
          <DiamondIcon className="size-3.5" strokeWidth={2.5} />
        </span>
        <span className="font-semibold tracking-tight">Dieline Studio</span>
      </div>

      <div className="px-3 pb-2">
        <Button variant="outline" size="sm" className="w-full justify-start" onClick={onNew}>
          <PlusIcon />
          New design
        </Button>
      </div>

      <div className="px-4 pt-2 pb-1 font-medium text-[11px] text-muted-foreground uppercase tracking-wider">
        Designs
      </div>
      <ScrollArea className="flex-1" scrollFade>
        <nav className="flex flex-col gap-px px-2 pb-2" aria-label="Designs">
          {sessions.map((s) => {
            const active = s.id === activeId;
            return (
              <button
                key={s.id}
                type="button"
                className={cn(
                  "flex h-8 w-full items-center gap-2.5 rounded-md px-2.5 text-left text-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
                  active
                    ? "bg-accent font-medium text-foreground"
                    : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                )}
                aria-current={active ? "true" : undefined}
                onClick={() => onSelect(s.id)}
                title={s.title}
              >
                {s.busy ? (
                  <Spinner className="size-3 shrink-0 text-primary" />
                ) : (
                  <span
                    className={cn(
                      "mx-0.5 size-2 shrink-0 rounded-full",
                      s.design ? DOT[s.design.overall] : "bg-muted-foreground/30",
                    )}
                  />
                )}
                <span className="truncate">{s.title}</span>
              </button>
            );
          })}
        </nav>
      </ScrollArea>

      <div className="flex h-12 shrink-0 items-center justify-between gap-2 border-t px-3">
        <span
          className={cn(
            "flex min-w-0 items-center gap-2 text-xs",
            engineReady ? "text-muted-foreground" : "text-destructive-foreground",
          )}
        >
          <span
            className={cn("size-1.5 shrink-0 rounded-full", engineReady ? "bg-success" : "bg-destructive")}
            aria-hidden
          />
          <span className="truncate">{engineReady ? `Engine ${engineVersion ?? ""}` : "Engine offline"}</span>
        </span>
        <Tip label="Settings">
          <Button variant="ghost" size="icon-sm" onClick={onSettings} aria-label="Settings">
            <SettingsIcon />
          </Button>
        </Tip>
      </div>
    </aside>
  );
}
