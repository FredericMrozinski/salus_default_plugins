import { useQuery } from "@tanstack/react-query";
import { ReactNode, useState } from "react";
import { api, encode } from "./salus";
import { cellTone, CELL_STATE_LABEL, ENTRY_STATE_LABEL, message } from "./format";
import { useDebounced, useDismiss } from "./hooks";
import type { Candidate, CellState, EntryState, GroupNode, SourceNode, TreeNode } from "./types";
import "./ui.css";

export function Badge({ state, children, title }: { state: CellState | EntryState | "info"; children?: ReactNode; title?: string }) {
  const tone = state === "info" ? "info" : cellTone(state);
  const label = state === "info" ? "" : state in CELL_STATE_LABEL ? CELL_STATE_LABEL[state as CellState] : ENTRY_STATE_LABEL[state as EntryState];
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children ?? label}
    </span>
  );
}

export function ErrorLine({ error }: { error: unknown }) {
  return error ? <div className="error-line">{message(error)}</div> : null;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useDismiss<HTMLDivElement>(onClose);
  return (
    <div className="modal-backdrop">
      <div className={`modal ${wide ? "modal-wide" : ""}`} ref={ref} role="dialog" aria-label={title}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

/** Flattens the source tree. */
export function flattenSources(nodes: TreeNode[]): SourceNode[] {
  return nodes.flatMap((n) => (n.type === "source" ? [n] : flattenSources((n as GroupNode).children)));
}

export function useTree() {
  return useQuery({ queryKey: ["tree"], queryFn: () => api<TreeNode[]>("GET", "/tree") });
}

/** Deleting needs the word "delete" typed into a field, so that no stray click deletes data. Returns the field and
 *  whether it holds the word. */
export function useDeleteWord(label = "Type “delete” to confirm"): [ReactNode, boolean] {
  const [text, setText] = useState("");
  const field = (
    <Field label={label}>
      <input autoFocus value={text} placeholder="delete" autoComplete="off" spellCheck={false} onChange={(e) => setText(e.target.value)} />
    </Field>
  );
  return [field, text.trim().toLowerCase() === "delete"];
}

/** A dialog that says what is lost and deletes only once "delete" is typed. */
export function DeleteDialog({ title, children, action = "Delete", pending, error, onConfirm, onClose }: {
  title: string;
  children: ReactNode;
  action?: string;
  pending?: boolean;
  error?: unknown;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const [field, confirmed] = useDeleteWord();
  return (
    <Modal title={title} onClose={onClose}>
      {children}
      {field}
      <ErrorLine error={error} />
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="danger" disabled={!confirmed || pending} onClick={onConfirm}>
          {action}
        </button>
      </div>
    </Modal>
  );
}

/** Reference picker: a search field on top of a list of matching source items (server-side search). */
export function ReferencePicker({
  dataset,
  slot,
  onPick,
  onClose,
}: {
  dataset: string;
  slot: number;
  onPick: (candidate: Candidate) => void;
  onClose: () => void;
}) {
  const [text, setText] = useState("");
  const q = useDebounced(text, 200);
  const ref = useDismiss<HTMLDivElement>(onClose);
  const results = useQuery({
    queryKey: ["candidates", dataset, slot, q],
    queryFn: () => api<Candidate[]>("GET", `/datasets/${encode(dataset)}/slots/${slot}/candidates`, { query: { q, limit: 40 } }),
    staleTime: 2000,
  });
  return (
    <div className="picker" ref={ref}>
      <input autoFocus placeholder="Search…" value={text} onChange={(e) => setText(e.target.value)} />
      <div className="picker-list">
        {results.isLoading && <Empty>Searching…</Empty>}
        {results.error ? <ErrorLine error={results.error} /> : null}
        {results.data?.length === 0 && <Empty>Nothing found.</Empty>}
        {results.data?.map((c) => (
          <button key={`${c.source}/${c.item}`} className="picker-item" onClick={() => onPick(c)}>
            <span className="picker-label">{c.label}</span>
            <span className="picker-detail">
              {c.source_name}
              {c.detail ? ` · ${c.detail}` : ""}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
