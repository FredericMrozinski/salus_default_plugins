import { QueryCache, QueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError, onEvent } from "./salus";
import type { WorkbenchEvent } from "./types";

export const queryClient: QueryClient = new QueryClient({
  // Any answer "the workbench folder is not set up" means the setup screen is due: look at the settings again.
  queryCache: new QueryCache({
    onError: (error) => {
      if (error instanceof ApiError && error.status === 412) queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
  }),
  defaultOptions: { queries: { staleTime: 5_000, refetchOnWindowFocus: false, retry: 1 } },
});

// Backend events make the cached views stale; active views refetch. Events come in bursts (every file of a folder upload
// sends source_changed and tree_changed; a refresh sends one per source and dataset), and every refetch is a request that
// reads the disk: so they are collected for a moment and each view refetches once for the burst.
const INVALIDATE_DELAY_MS = 400;
const staleKeys = new Map<string, unknown[]>();
let invalidateTimer: ReturnType<typeof setTimeout> | null = null;

function invalidate(...keys: unknown[][]) {
  for (const key of keys) staleKeys.set(JSON.stringify(key), key);
  invalidateTimer ??= setTimeout(() => {
    invalidateTimer = null;
    const keys = [...staleKeys.values()];
    staleKeys.clear();
    for (const queryKey of keys) queryClient.invalidateQueries({ queryKey });
  }, INVALIDATE_DELAY_MS);
}

onEvent((event) => {
  switch (event.type) {
    case "config_changed": // another folder: nothing shown so far is valid any more
      queryClient.invalidateQueries();
      break;
    case "tree_changed":
      invalidate(["tree"], ["search"], ["datasets"]);
      break;
    case "source_changed":
      invalidate(["tree"], ["search"], ["files", event.source], ["issues", event.source], ["suggest", event.source]);
      break;
    case "dataset_changed":
      invalidate(["datasets"], ["dataset", event.dataset], ["entries", event.dataset], ["stats", event.dataset], ["entry", event.dataset]);
      break;
  }
});

/** Latest progress of running background jobs by target (a source or dataset id). */
export type JobInfo = { kind: string; text: string; done: boolean; error?: string };
const jobs = new Map<string, JobInfo>();
const jobListeners = new Set<() => void>();
onEvent((event: WorkbenchEvent) => {
  if (event.type === "job") {
    const detail = event.files !== undefined ? `${event.files} files` : event.rows !== undefined ? `${event.rows} rows` : "";
    jobs.set(event.target, { kind: event.kind, text: `${event.kind}… ${detail}`, done: false });
  } else if (event.type === "job_done") {
    jobs.set(event.target, { kind: event.kind, text: event.ok ? "" : event.error ?? "failed", done: true, error: event.ok ? undefined : event.error });
    setTimeout(() => {
      const current = jobs.get(event.target);
      if (current?.done) {
        jobs.delete(event.target);
        jobListeners.forEach((l) => l());
      }
    }, event.ok ? 1500 : 15000);
  } else return;
  jobListeners.forEach((l) => l());
});

/** The Refresh job (backend scan_all): which source cohort it is reading, how many files it has seen there. */
export type RefreshProgress = { index: number; count: number; source: string; phase: string; files: number };
let refreshProgress: RefreshProgress | null = null;
const refreshListeners = new Set<() => void>();
onEvent((event: WorkbenchEvent) => {
  if (event.type === "job" && event.kind === "refresh") {
    refreshProgress = {
      index: event.index ?? refreshProgress?.index ?? 0,
      count: event.count ?? refreshProgress?.count ?? 0,
      source: event.source ?? refreshProgress?.source ?? "",
      phase: event.phase ?? "scan",
      files: event.files ?? (event.phase === "scan" ? 0 : refreshProgress?.files ?? 0),
    };
  } else if (event.type === "job_done" && event.kind === "refresh") refreshProgress = null;
  else return;
  refreshListeners.forEach((l) => l());
});

export function useRefreshProgress(): RefreshProgress | null {
  const [, force] = useState(0);
  useEffect(() => {
    const listener = () => force((n) => n + 1);
    refreshListeners.add(listener);
    return () => void refreshListeners.delete(listener);
  }, []);
  return refreshProgress;
}

export function useJob(target: string | null | undefined): JobInfo | undefined {
  const [, force] = useState(0);
  useEffect(() => {
    const listener = () => force((n) => n + 1);
    jobListeners.add(listener);
    return () => void jobListeners.delete(listener);
  }, []);
  return target ? jobs.get(target) : undefined;
}

export function useDebounced<T>(value: T, ms = 250): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}

/** Closes a popup when the user clicks outside of it or presses Escape. */
export function useDismiss<T extends HTMLElement>(onClose: () => void) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const down = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const key = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", down);
      document.removeEventListener("keydown", key);
    };
  }, [onClose]);
  return ref;
}
