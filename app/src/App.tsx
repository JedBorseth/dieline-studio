import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { ChatPanel } from "./components/ChatPanel";
import { PreviewPane } from "./components/PreviewPane";
import { SettingsDialog } from "./components/SettingsDialog";
import { Sidebar } from "./components/Sidebar";
import { Splitter } from "./components/Splitter";
import { TooltipProvider } from "./components/ui/tooltip";
import { EngineError, request, subscribe } from "./lib/engine";
import type { EngineInfo } from "./lib/protocol";
import { initialState, reducer } from "./lib/store";
import { loadNumber, saveNumber } from "./lib/prefs";

const MIN_CHAT = 360;
const MIN_PREVIEW = 320;

export default function App() {
  const [state, dispatch] = useReducer(reducer, undefined, initialState);
  const [info, setInfo] = useState<EngineInfo | null>(null);
  const [engineError, setEngineError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [previewWidth, setPreviewWidth] = useState(() => loadNumber("previewWidth", 640));
  const mainRef = useRef<HTMLDivElement>(null);
  const [mainWidth, setMainWidth] = useState(0);

  // Track the space beside the sidebar so the preview never squeezes the chat
  // below MIN_CHAT (or overflows) when the window is resized.
  useEffect(() => {
    const el = mainRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setMainWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const maxPreview = mainWidth ? Math.max(MIN_PREVIEW, mainWidth - MIN_CHAT) : Infinity;
  const effectivePreview = Math.min(Math.max(previewWidth, MIN_PREVIEW), maxPreview);

  const refreshInfo = useCallback(async () => {
    try {
      const result = await request("engine.info", {});
      setInfo(result);
      setEngineError(null);
      dispatch({ type: "engine_ready", ready: true });
    } catch (err) {
      setEngineError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    const unsubscribe = subscribe((event) => {
      dispatch({ type: "engine", event });
      if (event.type === "engine.ready") void refreshInfo();
    });
    void refreshInfo();
    return unsubscribe;
  }, [refreshInfo]);

  const active = state.sessions.find((s) => s.id === state.activeId) ?? state.sessions[0];

  const send = useCallback(async (sessionId: string, text: string) => {
    dispatch({ type: "user_message", sessionId, text });
    try {
      await request("chat.send", { session_id: sessionId, text });
    } catch (err) {
      const message = err instanceof EngineError ? err.message : String(err);
      dispatch({ type: "send_failed", sessionId, message });
    }
  }, []);

  const resizePreview = useCallback(
    (dx: number) => {
      const next = Math.min(Math.max(effectivePreview - dx, MIN_PREVIEW), maxPreview);
      setPreviewWidth(next);
      saveNumber("previewWidth", next);
    },
    [effectivePreview, maxPreview],
  );
  const openSettings = useCallback(() => {
    if (info) setSettingsOpen(true); // nothing to show until the engine has reported its settings
  }, [info]);

  return (
    <TooltipProvider delay={400}>
      <div className="flex h-full">
        <Sidebar
          sessions={state.sessions}
          activeId={state.activeId}
          onNew={() => dispatch({ type: "new_session" })}
          onSelect={(id) => dispatch({ type: "select_session", id })}
          onSettings={openSettings}
          engineReady={state.engineReady}
          engineVersion={info?.version}
        />
        <main className="flex min-w-0 flex-1" ref={mainRef}>
          <ChatPanel
            session={active}
            settings={info?.settings ?? null}
            engineError={engineError}
            onSend={(text) => void send(active.id, text)}
            onStop={() => void request("chat.cancel", { session_id: active.id }).catch(() => undefined)}
            onOpenSettings={openSettings}
            onChooseLayout={async (candidateId) => {
              const sessionId = active.id;
              const design = await request("layout.choose", { session_id: sessionId, candidate_id: candidateId });
              dispatch({ type: "preview", source: { kind: "design", design, origin: "form" }, sessionId });
            }}
          />
          <Splitter
            onDrag={resizePreview}
            value={effectivePreview}
            min={MIN_PREVIEW}
            max={Number.isFinite(maxPreview) ? maxPreview : effectivePreview}
          />
          <PreviewPane
            width={effectivePreview}
            source={state.preview}
            info={info}
            sessionId={active.id}
            sessionDesign={active.design}
            onPreview={(source, sessionId) => dispatch({ type: "preview", source, sessionId })}
          />
        </main>
        {settingsOpen && info && (
          <SettingsDialog
            settings={info.settings}
            onClose={() => setSettingsOpen(false)}
            onSaved={(settings) => setInfo((prev) => (prev ? { ...prev, settings } : prev))}
          />
        )}
      </div>
    </TooltipProvider>
  );
}
