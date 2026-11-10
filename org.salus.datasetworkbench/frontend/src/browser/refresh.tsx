import { useEffect, useState } from "react";
import { queryClient, useRefreshProgress } from "../shared/hooks";
import { Icon } from "../shared/icons";
import { api } from "../shared/salus";

// Refresh (both tabs of the Workbench: Sources and Datasets): reads every source cohort again in the backend (one job,
// the cohorts one after the other, see backend scan_all), then reloads what is shown. Its state lives here, not in a tab,
// so both tabs show the same button state and the same progress bar, whichever one started it.

let refreshing = false;
const listeners = new Set<() => void>();
const setRefreshing = (on: boolean) => ((refreshing = on), listeners.forEach((l) => l()));

export function useRefreshing(): boolean {
  const [, force] = useState(0);
  useEffect(() => {
    const listener = () => force((n) => n + 1);
    listeners.add(listener);
    return () => void listeners.delete(listener);
  }, []);
  return refreshing;
}

/** Waits until the backend has no running jobs (asks it, since events do not always arrive). A Refresh of a large remote
 *  folder can take long, so this waits up to two hours, asking every second. */
export async function waitForJobs() {
  for (let i = 0; i < 7200; i++) {
    const { running } = await api<{ running: number }>("GET", "/jobs");
    if (!running) return;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

/** Rescans every source, waits for the scans by asking the backend and reloads what is shown. */
export async function refresh() {
  if (refreshing) return;
  setRefreshing(true);
  try {
    await api("POST", "/scan");
    await waitForJobs();
  } catch (error) {
    window.alert(error instanceof Error ? error.message : String(error));
  } finally {
    setRefreshing(false);
    queryClient.invalidateQueries();
  }
}

export function RefreshButton() {
  const busy = useRefreshing();
  return (
    <button className="icon-button" title="Refresh: look for changed files in all source cohorts" disabled={busy} onClick={refresh}>
      <Icon name="refresh" size={22} />
    </button>
  );
}

const PHASES: Record<string, string> = { update: "saving", datasets: "updating the datasets", ingest: "reading the CSV" };

/** Progress of Refresh, under the toolbar; shows nothing while no Refresh runs. It does not block anything: the backend
 *  reads the cohorts one after the other in the background, and the rest of the workbench keeps working meanwhile. The
 *  bar moves on per cohort (how many files a folder holds is only known once it has been read). */
export function RefreshBar() {
  const busy = useRefreshing();
  const p = useRefreshProgress();
  if (!busy) return null;
  const where = p && p.count ? `${p.index} of ${p.count}${p.source ? `: ${p.source}` : ""}` : "";
  const what = !p ? "starting…" : PHASES[p.phase] ?? (p.files ? `${p.files.toLocaleString()} files so far` : "reading…");
  return (
    <div className="refresh-bar" role="status" aria-live="polite">
      <div className="refresh-text">
        <span>Refreshing{where ? ` ${where}` : "…"}</span>
        <span className="faint"> · {what}</span>
      </div>
      {p && p.count ? <progress value={p.index - 1 + (p.phase === "scan" ? 0.3 : 0.8)} max={p.count} /> : <progress />}
    </div>
  );
}
