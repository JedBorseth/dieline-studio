import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { ChatPanel } from "./components/ChatPanel";
import { PreviewPane } from "./components/PreviewPane";
import { SettingsDialog } from "./components/SettingsDialog";
import { Sidebar } from "./components/Sidebar";
import { Splitter } from "./components/Splitter";
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

  const resizePreview = useCallback((dx: number) => {
    const total = mainRef.current?.clientWidth ?? 1200;
    setPreviewWidth((w) => {
      const next = Math.min(Math.max(w - dx, MIN_PREVIEW), total - MIN_CHAT);
      saveNumber("previewWidth", next);
      return next;
    });
  }, []);

  return (
    <div className="app">
      <Sidebar
        sessions={state.sessions}
        activeId={state.activeId}
        onNew={() => dispatch({ type: "new_session" })}
        onSelect={(id) => dispatch({ type: "select_session", id })}
        onSettings={() => setSettingsOpen(true)}
        engineReady={state.engineReady}
        engineVersion={info?.version}
      />
      <main className="main" ref={mainRef}>
        <ChatPanel
          session={active}
          settings={info?.settings ?? null}
          engineError={engineError}
          onSend={(text) => void send(active.id, text)}
          onStop={() => void request("chat.cancel", { session_id: active.id }).catch(() => undefined)}
          onOpenSettings={() => setSettingsOpen(true)}
        />
        <Splitter onDrag={resizePreview} />
        <PreviewPane
          width={previewWidth}
          source={state.preview}
          info={info}
          onPreview={(source) => dispatch({ type: "preview", source })}
        />
      </main>
      {settingsOpen && info && (
        <SettingsDialog
          settings={info.settings}
          onClose={() => setSettingsOpen(false)}
          onSaved={(settings) => setInfo({ ...info, settings })}
        />
      )}
    </div>
  );
}
