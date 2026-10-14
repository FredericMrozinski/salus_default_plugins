import { useQuery } from "@tanstack/react-query";
import { ReactNode, useEffect, useState } from "react";
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

/** A button that asks for confirmation inline before it acts. */
export function ConfirmButton({ children, onConfirm, className, title }: { children: ReactNode; onConfirm: () => void; className?: string; title?: string }) {
  const [asking, setAsking] = useState(false);
  useEffect(() => {
    if (!asking) return;
    const t = setTimeout(() => setAsking(false), 4000);
    return () => clearTimeout(t);
  }, [asking]);
  return asking ? (
    <button className={`${className ?? ""} danger`} onClick={() => (setAsking(false), onConfirm())}>
      Sure?
    </button>
  ) : (
    <button className={className} title={title} onClick={() => setAsking(true)}>
      {children}
    </button>
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
