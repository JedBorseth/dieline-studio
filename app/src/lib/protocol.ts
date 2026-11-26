// Types for the engine's JSON-RPC protocol (engine/src/dieline_engine/rpc.py).
// Keep the two in sync.

/** A design's overall result: fail blocks export, warn needs a look. */
export type OverallStatus = "pass" | "warn" | "fail";
/** One check's result. stale counts as fail, unavailable as warn; info and not_applicable never change the overall. */
export type CheckStatus = OverallStatus | "info" | "not_applicable" | "unavailable" | "stale";

/** What a check is about: actionable (geometry, feature, fold, partition, artwork, fit) or a notice. */
export type CheckCategory =
  | "geometry"
  | "feature"
  | "fold"
  | "partition"
  | "artwork"
  | "fit"
  | "machine"
  | "calibration"
  | "export";

export interface CheckResult {
  id: string;
  status: CheckStatus;
  message: string;
  category?: CheckCategory;
  part_id?: string;
  /** The feature it's about: the preview highlights it. */
  feature_id?: string;
  /** Where to zoom: [x0, y0, x1, y1] in the design's units on the flat blank. */
  region?: [number, number, number, number];
}

/** A feature as stored on a part: parameters and a placement, never coordinates (engine features.py). */
export interface FeatureSpec {
  id?: string;
  kind: "handhole" | "hand_flap" | "vent" | "window" | "score" | "perforation" | "tear_strip" | "notch";
  label?: string;
  [key: string]: unknown;
}

/** A feature as the engine resolved it on a part. stale: it no longer fits; it blocks export. */
export interface FeatureInfo {
  id: string;
  kind: FeatureSpec["kind"];
  label: string;
  status: "ok" | "stale";
  reason?: string;
  faces: string[];
  copies: number;
  /** In plain words: faces, size, and whether each distance runs to the edge or the centre. */
  summary: string;
  selector: unknown;
  region?: [number, number, number, number];
  copy_regions?: [number, number, number, number][];
}

/** One drawn segment, tagged for highlighting (design units, flat blank, y up). */
export interface SceneSegment {
  type: "cut" | "crease" | "perforation" | "score" | "annotation";
  op: string | null;
  kind: string | null;
  provenance: "structure" | "feature";
  feature_id?: string;
  face_id?: string;
  /** A stale feature's ghost: drawn, never manufactured. */
  stale?: boolean;
  arc: boolean;
  start: [number, number];
  end: [number, number];
  center?: [number, number];
  clockwise?: boolean;
  full_circle?: boolean;
}

/** Whether an export target can carry the design (an unverified rule blocks it, not the design). */
export interface TargetReadiness {
  ready: boolean;
  blocking: string[];
  target?: string;
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
  /** Features on the part, each with a stable engine-made id. The form never sends these. */
  features?: FeatureSpec[];
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
  overall: OverallStatus;
  summary: DesignSummary;
  /** Set on designs the engine holds for a session (every design except a stateless box.create). */
  design_id?: string;
  revision?: number;
  /** The session's commit order: a design with a lower seq is older, whatever its id. */
  seq?: number;
  /** Every part; the top-level fields above are the first part's. */
  parts?: DesignPart[];
  /** Checks of the design as a whole (fit links, groups). */
  design_checks?: CheckResult[];
  /** Every segment tagged with its operation, provenance, feature and face. */
  scene?: SceneSegment[];
  /** The part's features as resolved. */
  features?: FeatureInfo[];
  /** Each export target's readiness; a block here isn't in overall. */
  targets?: Partial<Record<ExportFormat, TargetReadiness>>;
}

/** One part of a design: a box with its own id, label and quantity. */
export interface DesignPart extends Omit<Design, "parts" | "design_checks"> {
  part_id: string;
  label: string;
  quantity: number;
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
/** The design changed since the UI showed it; the UI fetches the current one. */
export const DESIGN_CHANGED = -32002;
/** Another export holds a destination folder; try again in a moment. */
export const EXPORT_LOCKED = -32003;
/** A destination's lock looks abandoned: the designer may break it. */
export const EXPORT_LOCK_STALE = -32004;

/** A part as a job record keeps it: enough to rebuild it with its id. */
export interface JobPart {
  part_id: string;
  label: string;
  quantity: number;
  piece_no: string | null;
  params: BoxParams;
  /** What regenerating with today's rules changed for this part. */
  changes: string[];
}

/** Whether the files a record lists are still the set it exported. */
export type SetStatus = "complete" | "pending" | "incomplete" | "unverified";

interface JobOpenCommon {
  stem: string | null;
  exported_at?: string | null;
  engine?: string | null;
  version: 1 | 2;
  set_status: SetStatus;
  set_notes: string[];
}

export type JobOpenResult =
  | (JobOpenCommon & {
      mode: "regenerate";
      /** The first part, rebuilt with today's rules (the others are in parts). */
      design: Design;
      /** What differs from the export: board values corrected since, regenerated sizes. */
      changes: string[];
      parts: JobPart[];
      /** The design's groups and fit links, as exported, so a reopened design keeps its fit checks. */
      groups: unknown[];
      links: unknown[];
    })
  | (JobOpenCommon & {
      mode: "exported";
      /** Exactly what was exported, from the record's own snapshot. */
      parts: { part_id: string; label: string; quantity: number; name: string; cf2: string }[];
    });

export interface ExportFile {
  part_id: string;
  label: string;
  quantity: number;
  format: ExportFormat;
  path: string;
  destination: "main" | "sample_table";
}

export interface SampleTable {
  /** published: in the folder; pending: waiting beside it for the designer to confirm the pickup;
   *  blocked: a failed move couldn't be undone (its journal is kept for the designer). */
  status: "none" | "published" | "pending" | "blocked";
  folder?: string;
  txn?: string;
  files?: string[];
  replaces?: string[];
  reason?: string;
  error?: string;
  warning?: string;
}

export interface ExportResult {
  path: string;
  paths: string[];
  /** The .dieline.json job record written next to the files; open it to reopen the design. */
  job_path: string;
  /** Files of an earlier export under this name, moved to the superseded folder. */
  superseded: string[];
  overall: OverallStatus;
  name: string;
  files: ExportFile[];
  sample_table: SampleTable;
  /** Interrupted exports of this name that were finished or undone first. */
  recovered: string[];
}

/** What an export would write, before the designer saves. */
export interface ExportPlan {
  ready: boolean;
  blocking: string[];
  files: ExportFile[];
  job_path: string | null;
  stem?: string;
  /** Existing files of this name that saving would move to superseded. */
  replaces: string[];
  /** The sample-table files would wait for the designer to confirm the pickup. */
  pickup: boolean;
  /** Each format's own readiness, so a format blocked by an unverified rule can be unticked. */
  formats?: Partial<Record<ExportFormat, { ready: boolean; blocking: string[] }>>;
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
  /** The form's Generate and a reopened job: a new design for the session. */
  "design.replace": [
    {
      session_id: string;
      /** The parts of a reopened job, in place of params. */
      parts?: Pick<JobPart, "part_id" | "label" | "quantity" | "piece_no" | "params">[];
      groups?: unknown[];
      links?: unknown[];
      params?: BoxParams;
      /** The design the designer was looking at when they asked (null: none). Refused if it changed. */
      expected?: { design_id: string; revision: number } | null;
    },
    Design,
  ];
  "design.get": [{ session_id: string }, Design | null];
  "box.export": [
    (
      | { session_id: string; design_id: string; revision: number }
      | { params: BoxParams }
    ) & {
      filename?: string;
      formats?: ExportFormat[];
      overwrite?: boolean;
      confirm_pickup?: boolean;
      /** The id of the one abandoned lock the designer agreed to break (from the error's data). */
      break_lock?: string;
      /** Perforations as dashed cuts in the sample-table DXF (unverified, so blocked for now). */
      dxf_expand_perfs?: boolean;
    },
    ExportResult,
  ];
  "export.plan": [
    (
      | { session_id: string; design_id: string; revision: number }
      | { params: BoxParams }
    ) & { filename?: string; formats?: ExportFormat[]; dxf_expand_perfs?: boolean },
    ExportPlan,
  ];
  "export.pickup": [
    { job_path: string; txn: string; break_lock?: string },
    { status: "published"; files: string[]; superseded: string[] },
  ];
  /** At start: finish or undo exports a crash interrupted. */
  "export.recover": [Record<string, never>, { notes: string[] }];
  "job.open": [{ record: unknown; mode?: "exported" | "regenerate" }, JobOpenResult];
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
  /** origin: "chat" for the agent's changes; "form" for the designer's own, whose replies the UI shows. */
  | { type: "design"; session_id: string | null; design: Design; origin?: "chat" | "form" }
  /** Sample-table files waiting for the designer to confirm the pickup (whoever exported them). */
  | ({ type: "export.pending"; job_path: string } & SampleTable);
