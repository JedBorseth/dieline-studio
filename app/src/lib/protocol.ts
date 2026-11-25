// Types for the engine's JSON-RPC protocol (engine/src/dieline_engine/rpc.py).
// Keep the two in sync.

export type CheckStatus = "pass" | "warn" | "fail";

export interface CheckResult {
  id: string;
  status: CheckStatus;
  message: string;
}

export interface BoxParams {
  style: string;
  length: number;
  width: number;
  depth: number;
  units: "mm" | "in";
  board: string;
}

export interface DesignSummary {
  style: string;
  inside_dims_mm: { length: number; width: number; depth: number };
  board: string;
  caliper_mm: number;
  calibrated: boolean;
  panels_mm: Record<string, number>;
  blank_mm: { width: number; height: number };
  [key: string]: unknown;
}

export interface Design {
  id: string;
  name: string;
  params: BoxParams;
  cf2: string;
  checks: CheckResult[];
  overall: CheckStatus;
  summary: DesignSummary;
}

export interface StyleInfo {
  id: string;
  name: string;
  description: string;
  calibrated: boolean;
}

export interface BoardInfo {
  id: string;
  name: string;
  flute: string;
  caliper_mm: number;
  corrugated: boolean;
}

export interface ModelInfo {
  id: string;
  label: string;
}

export interface Settings {
  model: string;
  effort: string;
  output_dir: string;
  output_dir_resolved: string;
  has_api_key: boolean;
  models: ModelInfo[];
  efforts: string[];
}

export interface EngineInfo {
  version: string;
  styles: StyleInfo[];
  boards: BoardInfo[];
  settings: Settings;
}

export interface ExportResult {
  path: string;
  overall: CheckStatus;
  name: string;
}

export interface SettingsUpdate {
  model?: string;
  effort?: string;
  output_dir?: string;
  /** null or "" clears the stored key */
  api_key?: string | null;
}

export interface Methods {
  "engine.info": [Record<string, never>, EngineInfo];
  "box.create": [BoxParams, Design];
  "box.export": [{ params: BoxParams; filename?: string }, ExportResult];
  "settings.get": [Record<string, never>, Settings];
  "settings.update": [SettingsUpdate, Settings];
  "chat.send": [{ session_id: string; text: string }, { accepted: true }];
  "chat.cancel": [{ session_id: string }, { ok: true }];
  "chat.reset": [{ session_id: string }, { ok: true }];
}

export type Method = keyof Methods;
export type ParamsOf<M extends Method> = Methods[M][0];
export type ResultOf<M extends Method> = Methods[M][1];

export type EngineEvent =
  | { type: "engine.ready"; version: string }
  | { type: "engine.exit"; code: number | null }
  | { type: "chat.text"; session_id: string; text: string }
  | { type: "chat.thinking"; session_id: string; text: string }
  | { type: "chat.tool_start"; session_id: string; tool_use_id: string; name: string }
  | {
      type: "chat.tool_result";
      session_id: string;
      tool_use_id: string;
      name: string;
      input: Record<string, unknown> | null;
      result: unknown;
      is_error: boolean;
    }
  | { type: "chat.turn_end"; session_id: string; stop_reason: string }
  | { type: "chat.error"; session_id: string; message: string }
  | { type: "design"; session_id: string | null; design: Design };
