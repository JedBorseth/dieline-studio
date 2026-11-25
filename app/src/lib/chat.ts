// Pure helpers for chat tool labels, model pricing, and path inputs (shared by UI components and unit tests).

import type { OpenRouterModel } from "./protocol";

function formatUsdPerMtok(n: number): string {
  if (Number.isInteger(n)) return `$${n}`;
  const fixed = n.toFixed(4).replace(/0+$/, "");
  const [whole, frac = ""] = fixed.split(".");
  return `$${whole}.${frac.padEnd(2, "0")}`;
}

/** Format OpenRouter per-million-token pricing, handling free (0/0), dynamic (< 0), and missing values. */
export function formatModelPrice(
  inputPerMtok: number | null | undefined,
  outputPerMtok: number | null | undefined,
): string | null {
  if (
    typeof inputPerMtok !== "number" ||
    typeof outputPerMtok !== "number" ||
    !Number.isFinite(inputPerMtok) ||
    !Number.isFinite(outputPerMtok)
  ) {
    return null;
  }
  if (inputPerMtok < 0 || outputPerMtok < 0) return "Dynamic pricing";
  if (inputPerMtok === 0 && outputPerMtok === 0) return "Free";
  return `${formatUsdPerMtok(inputPerMtok)}/${formatUsdPerMtok(outputPerMtok)} per M tokens in/out`;
}

/** Format the helper line below the Settings model input. */
export function formatModelSummary(
  chosen: OpenRouterModel | undefined,
  liveLoaded: boolean,
  fallbackLabel?: string,
): string {
  if (chosen) {
    const parts = [chosen.label];
    const price = formatModelPrice(chosen.input_per_mtok, chosen.output_per_mtok);
    if (price) parts.push(price);
    if (chosen.reasoning === false) parts.push("no reasoning effort");
    return parts.join(" · ");
  }
  if (liveLoaded) return "Not in OpenRouter's list of models with tools and image input.";
  return fallbackLabel
    ? `${fallbackLabel} · Any OpenRouter model with tool calling and image input.`
    : "Any OpenRouter model with tool calling and image input.";
}

/** Format a one-line description for a chat tool call card. */
export function formatToolLabel(
  name: string,
  input: Record<string, unknown> | null | undefined,
  result: unknown,
): string {
  const args = input ?? {};
  const str = (v: unknown) => (typeof v === "string" ? v : v === undefined || v === null ? "" : JSON.stringify(v));
  const file = (p: unknown) => {
    const s = str(p).trim();
    return s ? (s.split(/[\\/]/).pop() ?? "") : "";
  };
  const region = Array.isArray(args.region) && args.region.length === 4 ? ` · zoom ${args.region.join(", ")}` : "";

  switch (name) {
    case "create_box": {
      const { style, length, width, depth, units, board } = args as Record<string, string | number | undefined>;
      if (style !== undefined && length !== undefined && width !== undefined && depth !== undefined) {
        const dims = `${length} × ${width} × ${depth}${units ? ` ${units}` : ""}`;
        return `create_box · ${style} · ${dims}${board ? ` · ${board}` : ""}`;
      }
      return "create_box";
    }
    case "export_files": {
      const paths = (result as { paths?: string[] } | undefined)?.paths;
      return paths?.length ? `export_files → ${paths.join(", ")}` : "export_files";
    }
    case "view_design": {
      const f = file(args.path);
      return `view_design${f ? ` · ${f}` : ""}${region}`;
    }
    case "get_geometry": {
      const f = file(args.path);
      const lt = str(args.line_type);
      return `get_geometry${f ? ` · ${f}` : ""}${lt ? ` · ${lt}` : ""}${region}`;
    }
    case "search_files": {
      const q = str(args.query);
      const total = (result as { total_matches?: number } | undefined)?.total_matches;
      return `search_files${q ? ` · "${q}"` : ""}${total !== undefined ? ` · ${total} found` : ""}`;
    }
    case "read_file": {
      const f = file(args.path);
      const page =
        typeof args.page === "number" && args.page > 1
          ? args.page
          : (result as { page?: number } | undefined)?.page;
      const pageSuffix = typeof page === "number" && page > 1 ? ` · p. ${page}` : "";
      return `read_file${f ? ` · ${f}` : ""}${pageSuffix}`;
    }
    case "web_search": {
      const q = str(args.query);
      const results = (result as { results?: unknown[] } | undefined)?.results;
      const count = Array.isArray(results) ? ` · ${results.length} ${results.length === 1 ? "result" : "results"}` : "";
      return `web_search${q ? ` · "${q}"` : ""}${count}`;
    }
    case "web_fetch": {
      const url = str(args.url) || str((result as { url?: unknown } | undefined)?.url);
      return `web_fetch${url ? ` · ${url}` : ""}`;
    }
    default:
      return name;
  }
}

/** Trim whitespace and strip matching surrounding double quotes (Windows Explorer "Copy as path"). */
export function cleanPathInput(raw: string): string {
  const s = raw.trim();
  if (s.length >= 2 && s.startsWith('"') && s.endsWith('"')) {
    return s.slice(1, -1).trim();
  }
  return s;
}

/** Parse a newline-separated list of folder paths. */
export function parsePathList(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map(cleanPathInput)
    .filter(Boolean);
}
