// Dev-only: lets the UI run in a normal browser (`pnpm dev`) against the real
// Python engine, without the Tauri shell. Spawns the engine, proxies
// POST /__engine/rpc to it and streams notifications on GET /__engine/events
// (server-sent events). Never part of a production build.
//
// Only same-machine pages served by this dev server may call it: the Host
// header must be a loopback name (blocks DNS rebinding), any Origin must be
// this server's, and RPC bodies must be application/json (so a cross-site
// page can't send one without a CORS preflight, which is never granted).

import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import type { IncomingMessage, ServerResponse } from "node:http";
import readline from "node:readline";
import { fileURLToPath } from "node:url";
import type { Plugin } from "vite";

type Json = Record<string, unknown>;
type Settle = (msg: Json) => void;

const REPLY_TIMEOUT_MS = 60_000;
const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"]);

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

function hostname(hostHeader: string | undefined): string {
  if (!hostHeader) return "";
  return hostHeader.startsWith("[") ? hostHeader.slice(0, hostHeader.indexOf("]") + 1) : hostHeader.split(":")[0];
}

function allowed(req: IncomingMessage): boolean {
  if (!LOOPBACK.has(hostname(req.headers.host))) return false;
  const origin = req.headers.origin;
  if (origin === undefined) return true; // same-origin GETs (EventSource) may omit it
  try {
    const o = new URL(origin);
    return LOOPBACK.has(o.hostname === "::1" ? "[::1]" : o.hostname) && o.host === req.headers.host;
  } catch {
    return false;
  }
}

function reject(res: ServerResponse, status: number, message: string) {
  res.statusCode = status;
  res.setHeader("content-type", "application/json");
  res.end(JSON.stringify({ error: { message } }));
}

export function engineBridge(): Plugin {
  let child: ChildProcessWithoutNullStreams | null = null;
  let nextId = 1;
  // id -> settle function, tagged with the child that owns it
  const pending = new Map<number, { owner: ChildProcessWithoutNullStreams; settle: Settle }>();
  const clients = new Set<ServerResponse>();

  const broadcast = (payload: unknown) => {
    const data = `data: ${JSON.stringify(payload)}\n\n`;
    for (const res of clients) res.write(data);
  };

  const failOwnedBy = (owner: ChildProcessWithoutNullStreams, message: string) => {
    for (const [id, p] of pending) {
      if (p.owner === owner) {
        pending.delete(id);
        p.settle({ error: { message } });
      }
    }
  };

  const start = (): ChildProcessWithoutNullStreams => {
    const [cmd, args] = engineCommand();
    const proc = spawn(cmd, args, { stdio: ["pipe", "pipe", "pipe"], env: process.env });
    child = proc;
    proc.stdout.setEncoding("utf8");
    readline.createInterface({ input: proc.stdout }).on("line", (line) => {
      let msg: Json;
      try {
        msg = JSON.parse(line) as Json;
      } catch {
        console.warn("[engine] non-JSON stdout:", line);
        return;
      }
      if (typeof msg.id === "number") {
        const p = pending.get(msg.id);
        if (p) {
          pending.delete(msg.id);
          p.settle(msg);
        }
      } else if (msg.method === "event") {
        broadcast(msg.params);
      }
    });
    proc.stderr.on("data", (chunk: Buffer) => process.stderr.write(`[engine] ${chunk}`));
    proc.stdin.on("error", (err) => failOwnedBy(proc, `could not send to engine: ${err.message}`));
    proc.on("error", (err) => {
      // Spawn failure (e.g. uv not on PATH): no 'close' may follow, so clean up here.
      console.error("[engine] failed to start:", err.message);
      if (child === proc) child = null;
      failOwnedBy(proc, `engine failed to start: ${err.message}`);
    });
    // 'close' fires after stdout has drained, so buffered replies are delivered first.
    proc.on("close", (code) => {
      if (child === proc) child = null;
      failOwnedBy(proc, "engine exited");
      broadcast({ type: "engine.exit", code });
    });
    return proc;
  };

  const call = (method: string, params: unknown) =>
    new Promise<Json>((resolve) => {
      const proc = child ?? start();
      const id = nextId++;
      const timer = setTimeout(() => {
        if (pending.delete(id)) resolve({ error: { message: `engine did not answer \`${method}\` in time` } });
      }, REPLY_TIMEOUT_MS);
      pending.set(id, {
        owner: proc,
        settle: (msg) => {
          clearTimeout(timer);
          resolve(msg);
        },
      });
      proc.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    });

  const readBody = (req: IncomingMessage) =>
    new Promise<string>((resolve, reject) => {
      let body = "";
      req.setEncoding("utf8"); // decode across chunk boundaries correctly
      req.on("data", (c: string) => (body += c));
      req.on("end", () => resolve(body));
      req.on("error", reject);
    });

  return {
    name: "dieline-engine-bridge",
    apply: "serve",
    configureServer(server) {
      start();
      server.httpServer?.on("close", () => {
        for (const res of clients) res.end();
        clients.clear();
        child?.kill();
      });
      server.middlewares.use("/__engine/rpc", (req, res) => {
        if (req.method !== "POST") return reject(res, 405, "POST only");
        if (!allowed(req)) return reject(res, 403, "forbidden");
        if (!(req.headers["content-type"] ?? "").startsWith("application/json"))
          return reject(res, 415, "expected application/json");
        readBody(req)
          .then(async (body) => {
            let parsed: unknown;
            try {
              parsed = JSON.parse(body);
            } catch {
              return reject(res, 400, "bad JSON");
            }
            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed))
              return reject(res, 400, "expected a JSON object");
            const { method, params } = parsed as { method?: unknown; params?: unknown };
            if (typeof method !== "string") return reject(res, 400, "method must be a string");
            const reply = await call(method, params ?? {});
            res.setHeader("content-type", "application/json");
            res.end(JSON.stringify({ result: reply.result, error: reply.error }));
          })
          .catch((err: unknown) => {
            if (!res.headersSent) reject(res, 500, err instanceof Error ? err.message : String(err));
          });
      });
      server.middlewares.use("/__engine/events", (req, res) => {
        if (!allowed(req)) return reject(res, 403, "forbidden");
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
