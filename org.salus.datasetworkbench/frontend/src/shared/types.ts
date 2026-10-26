export type Column = { name: string; type: string; is_key: boolean };

export type SourceNode = {
  type: "source";
  id: string | null;
  name: string;
  path: string;
  kind: "file" | "table";
  state: string; // ready | needs_setup | no_csv | broken
  items?: number;
  missing?: number;
  last_scan?: string | null;
  rows?: number;
  issues?: number;
  csv?: string | null;
  csv_path?: string | null;
  csv_files?: { name: string; path: string; size: number }[];
  columns?: Column[];
  auto_key?: boolean;
  error?: string | null;
};
export type GroupNode = { type: "group"; name: string; path: string; children: TreeNode[] };
export type TreeNode = SourceNode | GroupNode;

export type FileItem = { id: string; path: string; name: string; ext: string; size: number; status: string; abs_path: string };
export type SearchHit = FileItem & { source: string; source_name: string; source_path: string; kind: "file" | "table" };
export type FolderItem = { name: string; path: string; count: number; missing: number };
export type FileListing = { dir: string; folders: FolderItem[]; files: FileItem[]; more: boolean };
export type Issue = { row_number: number; key: string | null; column: string | null; code: string; message: string };

export type EntryState = "valid" | "incomplete" | "corrupted";
export type CellState = "ok" | "incomplete" | "invalid" | "source_missing" | "source_changed";
export type SlotKind = "value" | "file" | "row";
export const VALUE_TYPES = ["text", "integer", "float", "boolean", "date", "datetime", "choice"] as const;
export type ValueType = (typeof VALUE_TYPES)[number];

export type Constraints = {
  default?: string | number | boolean;
  min?: number;
  max?: number;
  unit?: string;
  max_length?: number;
  regex?: string;
  choices?: string[];
};

export type Slot = {
  id?: number;
  name: string;
  label: string;
  description: string;
  required: boolean;
  unique: boolean;
  kind: SlotKind;
  value_type: ValueType | null;
  constraints: Constraints;
  sources: string[];
  extensions: string[];
  columns: string[];
  dangling_sources?: string[];
  source_names?: Record<string, string | null>;
};

export type DatasetSummary = {
  id: string;
  name: string;
  entries: number;
  states: Record<EntryState, number>;
  slots: number;
  error?: string;
};
export type DatasetDetail = DatasetSummary & { schema: Slot[] };

export type EntryRow = { id: number; state: EntryState; cells: Record<string, { text: string | null; state: CellState }> };
export type EntryPage = { total: number; entries: EntryRow[] };

export type CellRef = {
  source: string;
  item: string;
  label: string | null;
  source_name: string | null;
  path: string | null;
  values: Record<string, unknown> | null;
};
export type Cell = { slot: number; name: string; state: CellState; detail: string | null; value: unknown; ref: CellRef | null };
export type EntryDetail = { id: number; state: EntryState; created_at: string; updated_at: string; cells: Cell[] };

export type Candidate = { source: string; source_name: string; item: string; label: string; detail?: string };

export type Stats = {
  entries: number;
  states: Record<EntryState, number>;
  items: Record<CellState, number>;
  slots: { slot: number; name: string; label: string; required: boolean; filled: number; states: Record<CellState, number> }[];
};

export type Nav =
  | { type: "open_dataset"; dataset: string }
  | { type: "show_entry"; dataset: string; entry: number }
  | { type: "edit_schema"; dataset: string };

export type WorkbenchEvent =
  | { type: "tree_changed" }
  | { type: "config_changed" }
  | { type: "source_changed"; source: string }
  | { type: "dataset_changed"; dataset: string; entry?: number; schema?: boolean; deleted?: boolean }
  | { type: "state"; dataset: string | null; entry: number | null }
  | { type: "job"; job: string; kind: string; target: string; phase?: string; files?: number; rows?: number }
  | { type: "job_done"; job: string; kind: string; target: string; ok: boolean; error?: string };
