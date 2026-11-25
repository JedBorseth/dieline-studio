// Types for the engine's JSON-RPC protocol (engine/src/dieline_engine/rpc.py).
// Keep the two in sync.

export type CheckStatus = "pass" | "warn" | "fail";

export interface CheckResult {
  id: string;
  status: CheckStatus;
  message: string;
}

export type OptionValue = string | number;
type BoardSource = "artioscad" | "derived" | "nominal";

export interface BoxParams {
  style: string;
  /** Inside dimensions: numbers, or fraction strings like "14 1/8" (the engine parses them). */
  length: number | string;
  width: number | string;
  depth: number | string;
  units: "in" | "mm";
  board: string;
  options?: Record<string, OptionValue>;
}

/** Facts about a generated design. All lengths in inches. */
export interface DesignSummary {
  style: string;
  standard: string;
  units: "in";
  inside_in: { length: number; width: number; depth: number };
  board: string;
  board_verified: boolean;
  board_source: BoardSource;
  cal_in: number;
  il_in: number;
  og_in?: number;
  corrugated?: boolean;
  options: Record<string, OptionValue>;
  calibrated: boolean;
  lug_extension_in?: number;
  panels_in: number[];
  glue_lug_in: number;
  body_height_in: number;
  top_depth_in?: number;
  bottom_depth_in?: number;
  bottom_flaps_in: number[] | null;
  top_flaps_in: number[] | null;
  slot_in: number;
  blank_in: { width: number; height: number };
  rounded_in?: Record<string, number>;
  pointage?: number;
  blank_area_sqft?: number;
  rule_length_in?: { cut: number; crease: number };
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

export interface StyleOptionInfo {
  id: string;
  label: string;
  description: string;
  default: OptionValue;
  /** Present for a choice; absent for a number in inches. */
  choices?: { value: string; label: string }[];
  min?: number | null;
  max?: number | null;
}

export interface StyleInfo {
  id: string;
  name: string;
  standard: string;
  description: string;
  calibrated: boolean;
  options: StyleOptionInfo[];
}

export interface BoardInfo {
  id: string;
  name: string;
  flute: string;
  cal_in: number;
  il_in: number;
  og_in: number;
  caliper_mm: number;
  verified: boolean;
  source: BoardSource;
  evidence: string;
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
  /** DXF exports go here when set (the designer's sample-table folder). */
  dxf_dir: string;
  /** Web search provider for the chat (client-side, as in OpenCode); "off" also disables fetching pages. */
  search_provider: SearchProvider;
  search_providers: SearchProvider[];
  /** Whether the user stored their own key for a provider (else it runs keyless, rate-limited). */
  has_search_key: Partial<Record<SearchProvider, boolean>>;
  /** Folders the chat may search and read, read-only. Empty: the work OneDrive synced on this PC. */
  reference_dirs: string[];
  reference_dirs_resolved: string[];
  /** An OpenRouter API key is stored. */
  has_api_key: boolean;
  /** Suggested OpenRouter models; any OpenRouter model id with tools and image input works. */
  models: ModelInfo[];
  efforts: string[];
}

export type SearchProvider = "exa" | "tavily" | "off";

/** A usable model from OpenRouter's live list (tools and image input). */
export interface OpenRouterModel {
  id: string;
  label: string;
  context: number | null;
  input_per_mtok: number | null;
  output_per_mtok: number | null;
  reasoning: boolean;
}

export interface EngineInfo {
  version: string;
  styles: StyleInfo[];
  boards: BoardInfo[];
  default_board: string;
  settings: Settings;
}

export type ExportFormat = "cf2" | "dxf";

/** Engine error code for an export whose name is already taken; retry with overwrite. */
export const EXPORT_EXISTS = -32001;

export interface JobOpenResult {
  design: Design;
  /** What differs from the export: board values corrected since, regenerated sizes. */
  changes: string[];
  stem: string | null;
  exported_at?: string | null;
  engine?: string | null;
}

export interface ExportResult {
  path: string;
  paths: string[];
  /** The .dieline.json job record written next to the files; open it to regenerate the design. */
  job_path: string;
  /** Files of an earlier export under this name, moved to the superseded folder. */
  superseded: string[];
  overall: CheckStatus;
  name: string;
}

export interface SettingsUpdate {
  model?: string;
  effort?: string;
  output_dir?: string;
  dxf_dir?: string;
  search_provider?: SearchProvider;
  /** Own search keys; null or "" clears the stored key. */
  exa_api_key?: string | null;
  tavily_api_key?: string | null;
  reference_dirs?: string[];
  /** null or "" clears the stored key */
  api_key?: string | null;
}

export interface Methods {
  "engine.info": [Record<string, never>, EngineInfo];
  "box.create": [BoxParams, Design];
  "box.export": [
    { params: BoxParams; filename?: string; formats?: ExportFormat[]; overwrite?: boolean },
    ExportResult,
  ];
  "job.open": [{ record: unknown }, JobOpenResult];
  "chat.set_design": [{ session_id: string; params: BoxParams }, { ok: true }];
  "settings.get": [Record<string, never>, Settings];
  "models.list": [{ refresh?: boolean }, { models: OpenRouterModel[] }];
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
      /** PNG data URL of the picture the model was shown (create_box, view_design, read_file). */
      image?: string;
    }
  | { type: "chat.turn_end"; session_id: string; stop_reason: string }
  | { type: "chat.error"; session_id: string; message: string }
  | { type: "design"; session_id: string | null; design: Design };
