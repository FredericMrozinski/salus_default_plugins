import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ReactNode, useEffect, useState, useSyncExternalStore } from "react";
import { useDebounced } from "./hooks";
import { api } from "./salus";
import { Empty, ErrorLine, Field, Modal } from "./ui";

/** The workbench folder, kept in the plugin's config directory by the backend. */
export type Settings = {
  root: string | null; // what is stored
  active: boolean; // whether the backend uses it (false if nothing is stored, or the stored folder cannot be used)
  error: string | null;
  suggestion: string;
  suggestion_exists: boolean;
  config_file: string | null;
};

type RootCheck = { path: string | null; exists?: boolean; will_create?: boolean; has_workbench?: boolean; has_files?: boolean; problem: string | null };

export function useSettings() {
  return useQuery({ queryKey: ["settings"], queryFn: () => api<Settings>("GET", "/settings"), staleTime: 0 });
}

/** Shows the app once the workbench folder is set up, and asks for the folder before that. */
export function RootGuard({ children }: { children: ReactNode }) {
  const settings = useSettings();
  if (settings.isLoading) return <Empty>Loading…</Empty>;
  if (settings.error) return <ErrorLine error={settings.error} />;
  if (!settings.data?.active) {
    return (
      <div className="setup">
        <RootForm settings={settings.data!} />
      </div>
    );
  }
  return <>{children}</>;
}

/**
 * Asks where the workbench keeps its data and applies the answer. `onCancel` is given when this is a change, not the first setup.
 */
export function RootForm({ settings, onDone, onCancel }: { settings: Settings; onDone?: () => void; onCancel?: () => void }) {
  const queryClient = useQueryClient();
  const changing = settings.active && !!onCancel;
  const [text, setText] = useState(settings.root ?? settings.suggestion);
  const wanted = useDebounced(text, 250);
  const check = useQuery({
    queryKey: ["settings-check", wanted],
    queryFn: () => api<RootCheck>("POST", "/settings/check", { json: { root: wanted } }),
    staleTime: 0,
    gcTime: 0,
  });
  const apply = useMutation({
    mutationFn: () => api("PUT", "/settings", { json: { root: text } }),
    onSuccess: () => {
      queryClient.invalidateQueries(); // everything shown so far belonged to the old folder
      onDone?.();
    },
  });
  const info = check.data && wanted === text ? check.data : null;
  const blocked = !text.trim() || !!info?.problem || apply.isPending;

  let hint: ReactNode = null;
  if (info?.problem) hint = <span className="bad">{info.problem}</span>;
  else if (info?.has_workbench) hint = <span className="good">A workbench is in this folder already. Its source cohorts and datasets will be used.</span>;
  else if (info?.will_create) hint = <>The folder does not exist yet; it will be created, with the folders “sources” and “datasets” in it.</>;
  else if (info?.has_files) hint = <>Other files are in this folder. The workbench only adds the folders “sources” and “datasets” and leaves the rest alone.</>;
  else if (info) hint = <>An empty folder: the workbench creates “sources” and “datasets” in it.</>;

  return (
    <form
      className="root-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (!blocked) apply.mutate();
      }}
    >
      {!changing && <h2>Where should the workbench keep its data?</h2>}
      <p className="muted">
        Source cohorts are kept in the folder <b>sources</b> and datasets in the folder <b>datasets</b>, both inside one folder on the server. Enter the full path of that
        folder. Use a folder on a local disk of the server: the databases in it do not work reliably on network drives.
      </p>
      {settings.error && <div className="warn-line">{settings.error}</div>}
      <Field label="Workbench folder" hint={settings.suggestion_exists && !settings.root ? "A folder from before was found at the suggested place." : undefined}>
        <input
          className="mono"
          autoFocus
          spellCheck={false}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="/path/to/SalusWorkbench"
          onFocus={(e) => e.target.select()}
        />
      </Field>
      <div className="root-hint">{hint ?? " "}</div>
      {changing && (
        <p className="muted">Nothing is deleted: the source cohorts and datasets of the current folder stay where they are, but they are no longer listed here.</p>
      )}
      <ErrorLine error={apply.error} />
      <div className="modal-actions">
        {onCancel && (
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
        )}
        <button type="submit" className="primary" disabled={blocked}>
          {changing ? "Use this folder" : "Set up the workbench here"}
        </button>
      </div>
      {settings.config_file && <div className="faint root-config">The choice is stored in {settings.config_file}</div>}
    </form>
  );
}

// ---- changing the folder later (from a menu)

let dialogOpen = false;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());
export const openRootDialog = () => ((dialogOpen = true), emit());
const closeRootDialog = () => ((dialogOpen = false), emit());
const useRootDialogOpen = () => useSyncExternalStore((l) => (listeners.add(l), () => void listeners.delete(l)), () => dialogOpen);

export function RootDialog() {
  const open = useRootDialogOpen();
  const settings = useSettings();
  useEffect(() => {
    if (open) settings.refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  if (!open || !settings.data) return null;
  return (
    <Modal title="Workbench folder" onClose={closeRootDialog} wide>
      <RootForm settings={settings.data} onDone={closeRootDialog} onCancel={closeRootDialog} />
    </Modal>
  );
}
