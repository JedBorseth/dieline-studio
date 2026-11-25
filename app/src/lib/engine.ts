// Engine client. Inside the desktop app it goes through the Tauri shell
// (src-tauri/src/engine.rs); in a plain browser during `pnpm dev` it goes
// through the Vite dev bridge (vite-engine-bridge.ts). Same protocol either way.

import type { EngineEvent, Method, ParamsOf, ResultOf } from "./protocol";

type Listener = (event: EngineEvent) => void;

interface Transport {
  request(method: string, params: unknown): Promise<unknown>;
  subscribe(listener: Listener): () => void;
}

export class EngineError extends Error {
  constructor(
    message: string,
    readonly code?: number,
  ) {
    super(message);
    this.name = "EngineError";
  }
}

export const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

function tauriTransport(): Transport {
  const core = import("@tauri-apps/api/core");
  const events = import("@tauri-apps/api/event");
  return {
    async request(method, params) {
      const { invoke } = await core;
      try {
        return await invoke("engine_request", { method, params });
      } catch (err) {
        // The shell rejects with { code, message } or a plain string.
        if (err && typeof err === "object" && "message" in err) {
          const e = err as { message: string; code?: number };
          throw new EngineError(e.message, e.code);
        }
        throw new EngineError(String(err));
      }
    },
    subscribe(listener) {
      let unlisten: (() => void) | undefined;
      let cancelled = false;
      void events.then(({ listen }) =>
        listen<EngineEvent>("engine-event", (e) => listener(e.payload)).then((fn) => {
          if (cancelled) fn();
          else unlisten = fn;
        }),
      );
      return () => {
        cancelled = true;
        unlisten?.();
      };
    },
  };
}

function devBridgeTransport(): Transport {
  return {
    async request(method, params) {
      const res = await fetch("/__engine/rpc", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ method, params }),
      });
      const body = (await res.json()) as { result?: unknown; error?: { code?: number; message: string } };
      if (body.error) throw new EngineError(body.error.message, body.error.code);
      return body.result;
    },
    subscribe(listener) {
      const source = new EventSource("/__engine/events");
      source.onmessage = (msg) => listener(JSON.parse(msg.data) as EngineEvent);
      return () => source.close();
    },
  };
}

const transport: Transport = isTauri ? tauriTransport() : devBridgeTransport();

export function request<M extends Method>(method: M, params: ParamsOf<M>): Promise<ResultOf<M>> {
  return transport.request(method, params) as Promise<ResultOf<M>>;
}

export function subscribe(listener: Listener): () => void {
  return transport.subscribe(listener);
}
