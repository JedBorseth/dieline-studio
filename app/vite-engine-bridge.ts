// Dev-only: lets the UI run in a normal browser (`pnpm dev`) against the real
// Python engine, without the Tauri shell. Spawns the engine, proxies
// POST /__engine/rpc to it and streams notifications on GET /__engine/events
// (server-sent events). Never part of a production build.

import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import readline from "node:readline";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { Plugin } from "vite";

type Json = Record<string, unknown>;

function engineCommand(): [string, string[]] {
  // DIELINE_ENGINE_CMD is a JSON array, e.g. ["C:/venv/Scripts/python.exe","-m","dieline_engine"].
  const raw = process.env.DIELINE_ENGINE_CMD;
  if (raw) {
    const cmd = JSON.parse(raw) as string[];
    return [cmd[0], cmd.slice(1)];
  }
  const engineDir = fileURLToPath(new URL("../engine", import.meta.url));
  return ["uv", ["run", "--project", engineDir, "dieline-engine"]];
}

export function engineBridge(): Plugin {
  let child: ChildProcessWithoutNullStreams | null = null;
  let nextId = 1;
  const pending = new Map<number, (msg: Json) => void>();
  const clients = new Set<ServerResponse>();

  const broadcast = (payload: unknown) => {
    const data = `data: ${JSON.stringify(payload)}\n\n`;
    for (const res of clients) res.write(data);
  };

  const start = () => {
    const [cmd, args] = engineCommand();
    const proc = spawn(cmd, args, { stdio: ["pipe", "pipe", "pipe"], env: process.env });
    child = proc;
    readline.createInterface({ input: proc.stdout }).on("line", (line) => {
      let msg: Json;
      try {
        msg = JSON.parse(line) as Json;
      } catch {
        console.warn("[engine] non-JSON stdout:", line);
        return;
      }
      if (typeof msg.id === "number" && pending.has(msg.id)) {
        pending.get(msg.id)!(msg);
        pending.delete(msg.id);
      } else if (msg.method === "event") {
        broadcast(msg.params);
      }
    });
    proc.stderr.on("data", (chunk: Buffer) => process.stderr.write(`[engine] ${chunk}`));
    proc.on("exit", (code) => {
      if (child === proc) child = null;
      for (const resolve of pending.values()) resolve({ error: { message: "engine exited" } });
      pending.clear();
      broadcast({ type: "engine.exit", code });
    });
    proc.on("error", (err) => console.error("[engine] failed to start:", err.message));
  };

  const call = (method: string, params: unknown) =>
    new Promise<Json>((resolve) => {
      if (!child) start();
      const id = nextId++;
      pending.set(id, resolve);
      child!.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    });

  const readBody = (req: IncomingMessage) =>
    new Promise<string>((resolve, reject) => {
      let body = "";
      req.on("data", (c: Buffer) => (body += c));
      req.on("end", () => resolve(body));
      req.on("error", reject);
    });

  return {
    name: "dieline-engine-bridge",
    apply: "serve",
    configureServer(server) {
      start();
      server.httpServer?.on("close", () => child?.kill());
      server.middlewares.use("/__engine/rpc", (req, res) => {
        if (req.method !== "POST") {
          res.statusCode = 405;
          res.end();
          return;
        }
        void readBody(req).then(async (body) => {
          let parsed: { method?: string; params?: unknown };
          try {
            parsed = JSON.parse(body) as typeof parsed;
          } catch {
            res.statusCode = 400;
            res.end(JSON.stringify({ error: { message: "bad JSON" } }));
            return;
          }
          const reply = await call(String(parsed.method), parsed.params ?? {});
          res.setHeader("content-type", "application/json");
          res.end(JSON.stringify({ result: reply.result, error: reply.error }));
        });
      });
      server.middlewares.use("/__engine/events", (req, res) => {
        res.writeHead(200, {
          "content-type": "text/event-stream",
          "cache-control": "no-cache",
          connection: "keep-alive",
        });
        res.write(": connected\n\n");
        clients.add(res);
        req.on("close", () => clients.delete(res));
      });
    },
  };
}
