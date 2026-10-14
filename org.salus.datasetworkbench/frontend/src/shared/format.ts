import type { CellState, EntryState } from "./types";

export function bytes(n: number | null | undefined): string {
  if (n == null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

export const ENTRY_STATE_LABEL: Record<EntryState, string> = { valid: "valid", incomplete: "incomplete", corrupted: "corrupted" };

export const CELL_STATE_LABEL: Record<CellState, string> = {
  ok: "ok",
  incomplete: "incomplete",
  invalid: "invalid",
  source_missing: "source missing",
  source_changed: "source changed",
};

export function cellTone(state: CellState | EntryState): "ok" | "warn" | "bad" {
  if (state === "ok" || state === "valid") return "ok";
  if (state === "incomplete") return "warn";
  return "bad";
}

export function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
