import { QueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { onEvent } from "./salus";
import type { WorkbenchEvent } from "./types";

export const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 5_000, refetchOnWindowFocus: false, retry: 1 } },
});

// Backend events make the cached views stale; active views refetch.
onEvent((event) => {
  switch (event.type) {
    case "tree_changed":
      queryClient.invalidateQueries({ queryKey: ["tree"] });
      queryClient.invalidateQueries({ queryKey: ["search"] });
      queryClient.invalidateQueries({ queryKey: ["datasets"] });
      break;
    case "source_changed":
      queryClient.invalidateQueries({ queryKey: ["tree"] });
      queryClient.invalidateQueries({ queryKey: ["search"] });
      queryClient.invalidateQueries({ queryKey: ["files", event.source] });
      queryClient.invalidateQueries({ queryKey: ["issues", event.source] });
      queryClient.invalidateQueries({ queryKey: ["suggest", event.source] });
      break;
    case "dataset_changed":
      queryClient.invalidateQueries({ queryKey: ["datasets"] });
      queryClient.invalidateQueries({ queryKey: ["dataset", event.dataset] });
      queryClient.invalidateQueries({ queryKey: ["entries", event.dataset] });
      queryClient.invalidateQueries({ queryKey: ["stats", event.dataset] });
      queryClient.invalidateQueries({ queryKey: ["entry", event.dataset] });
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
