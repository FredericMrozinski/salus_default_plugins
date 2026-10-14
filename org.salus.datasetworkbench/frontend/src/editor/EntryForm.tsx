import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { message } from "../shared/format";
import { api, encode, salus, setState } from "../shared/salus";
import type { Candidate, Cell, DatasetDetail, EntryDetail, Slot } from "../shared/types";
import { Badge, ConfirmButton, Empty, ErrorLine, ReferencePicker } from "../shared/ui";

export function EntryForm({ dataset, entry, onEditSchema, onClosed }: { dataset: DatasetDetail; entry: number | null; onEditSchema: () => void; onClosed: () => void }) {
  const queryClient = useQueryClient();
  const detail = useQuery({
    queryKey: ["entry", dataset.id, entry],
    queryFn: () => api<EntryDetail>("GET", `/datasets/${encode(dataset.id)}/entries/${entry}`),
    enabled: entry !== null,
    retry: false,
  });
  const remove = useMutation({
    mutationFn: () => api("DELETE", `/datasets/${encode(dataset.id)}/entries/${entry}`),
    onSuccess: () => {
      setState(dataset.id, null).catch(() => undefined);
      onClosed();
    },
  });

  const head = (
    <div className="toolbar">
      <h2>
        {dataset.name}
        {entry !== null && <span className="muted"> · entry #{entry}</span>}
      </h2>
      {detail.data && <Badge state={detail.data.state} />}
      <button className="small" onClick={onEditSchema}>
        Edit schema
      </button>
      {entry !== null && detail.data && (
        <ConfirmButton className="small" onConfirm={() => remove.mutate()} title="Delete this entry">
          Delete
        </ConfirmButton>
      )}
    </div>
  );

  if (dataset.schema.length === 0)
    return (
      <div className="app">
        {head}
        <Empty>
          This dataset has no fields yet.{" "}
          <button className="primary small" onClick={onEditSchema}>
            Define the schema
          </button>
        </Empty>
      </div>
    );
  if (entry === null)
    return (
      <div className="app">
        {head}
        <Empty>Select an entry in the list on the right, or add one with “+ Entry”.</Empty>
      </div>
    );
  return (
    <div className="app">
      {head}
      <div className="scroll form-scroll">
        <ErrorLine error={detail.error ?? remove.error} />
        {detail.isLoading && <Empty>Loading…</Empty>}
        {detail.data && (
          <div className="form">
            {dataset.schema.map((slot) => {
              const cell = detail.data.cells.find((c) => c.slot === slot.id);
              return cell ? (
                <SlotField
                  key={slot.id}
                  dataset={dataset.id}
                  entry={entry}
                  slot={slot}
                  cell={cell}
                  onSaved={(saved) => queryClient.setQueryData(["entry", dataset.id, entry], saved)}
                />
              ) : null;
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function SlotField({ dataset, entry, slot, cell, onSaved }: { dataset: string; entry: number; slot: Slot; cell: Cell; onSaved: (entry: EntryDetail) => void }) {
  const base = `/datasets/${encode(dataset)}/entries/${entry}/cells/${slot.id}`;
  const save = useMutation({ mutationFn: (payload: object) => api<EntryDetail>("PUT", base, { json: payload }), onSuccess: onSaved });
  const mark = useMutation({ mutationFn: () => api<EntryDetail>("POST", `${base}/mark-valid`), onSuccess: onSaved });
  const tone = cell.state === "ok" ? "" : `slot-${cell.state}`;

  return (
    <div className={`slot ${tone}`}>
      <div className="slot-head">
        <span className="slot-label">
          {slot.label}
          {slot.required && <span className="req" title="Required"> *</span>}
        </span>
        {cell.state !== "ok" && !(cell.state === "incomplete" && !slot.required) && <Badge state={cell.state} />}
        <span className="slot-kind faint">{slot.kind === "value" ? slot.value_type : slot.kind}</span>
      </div>
      {slot.description && <div className="slot-description faint">{slot.description}</div>}
      {slot.kind === "value" ? (
        <ValueInput slot={slot} value={cell.value} onCommit={(value) => save.mutate({ value })} />
      ) : (
        <RefInput dataset={dataset} slot={slot} cell={cell} onPick={(c) => save.mutate({ source: c.source, item: c.item })} onClear={() => save.mutate({ item: null })} />
      )}
      {cell.detail && cell.state !== "incomplete" && <div className={`slot-detail ${cell.state}`}>{describe(cell)}</div>}
      {(cell.state === "source_changed" || cell.state === "source_missing") && (
        <div className="slot-actions">
          <button className="small" disabled={mark.isPending} onClick={() => mark.mutate()} title={cell.state === "source_missing" ? "Clears the reference" : "Accept the changed content"}>
            {cell.state === "source_missing" ? "Mark as valid (clear reference)" : "Mark as valid"}
          </button>
        </div>
      )}
      <ErrorLine error={save.error ?? mark.error} />
    </div>
  );
}

function describe(cell: Cell): string {
  if (cell.state === "source_changed") return "The source was edited after it was added here. Check it, then mark it as valid.";
  if (cell.state === "source_missing") return `The referenced item is gone (${cell.detail}). Pick a replacement or mark as valid to clear the reference.`;
  return cell.detail ?? "";
}

function ValueInput({ slot, value, onCommit }: { slot: Slot; value: unknown; onCommit: (value: unknown) => void }) {
  const type = slot.value_type;
  const unit = slot.constraints.unit;
  const current = value === null || value === undefined ? "" : String(value);
  const commit = (text: string) => text !== current && onCommit(text === "" ? null : text);
  // Uncontrolled inputs keyed by the stored value: typing never fights the refetch, and a saved normalization shows up.
  const key = `${slot.id}:${current}`;

  if (type === "boolean")
    return (
      <select key={key} defaultValue={current} onChange={(e) => commit(e.target.value)}>
        <option value=""></option>
        <option value="true">yes</option>
        <option value="false">no</option>
      </select>
    );
  if (type === "choice")
    return (
      <select key={key} defaultValue={current} onChange={(e) => commit(e.target.value)}>
        <option value=""></option>
        {current && !(slot.constraints.choices ?? []).includes(current) && <option value={current}>{current} (not allowed)</option>}
        {(slot.constraints.choices ?? []).map((c) => (
          <option key={c}>{c}</option>
        ))}
      </select>
    );
  const input =
    type === "text" ? (
      <textarea key={key} rows={2} defaultValue={current} onBlur={(e) => commit(e.target.value)} />
    ) : (
      <input
        key={key}
        type={type === "integer" || type === "float" ? "number" : type === "date" ? "date" : type === "datetime" ? "datetime-local" : "text"}
        step={type === "integer" ? 1 : type === "float" ? "any" : undefined}
        defaultValue={type === "datetime" ? current.slice(0, 19) : current}
        onBlur={(e) => commit(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      />
    );
  return unit ? (
    <div className="row">
      {input}
      <span className="muted">{unit}</span>
    </div>
  ) : (
    input
  );
}

function RefInput({ dataset, slot, cell, onPick, onClear }: { dataset: string; slot: Slot; cell: Cell; onPick: (c: Candidate) => void; onClear: () => void }) {
  const [picking, setPicking] = useState(false);
  const [openError, setOpenError] = useState<string | null>(null);
  const ref = cell.ref;
  return (
    <div className="ref">
      {ref ? (
        <div className="ref-current">
          <div className="ref-label">
            <span className="mono">{ref.label ?? ref.item}</span>
            {ref.source_name && <span className="faint"> · {ref.source_name}</span>}
          </div>
          <div className="ref-actions">
            {slot.kind === "file" && ref.path && cell.state !== "source_missing" && (
              <button className="small" onClick={() => salus.openFile(ref.path!).then(() => setOpenError(null), (e) => setOpenError(message(e)))}>
                Open
              </button>
            )}
            <button className="small" onClick={() => setPicking(true)}>
              Change…
            </button>
            <button className="small" onClick={onClear}>
              Clear
            </button>
          </div>
        </div>
      ) : (
        <div className="ref-current">
          <span className="faint">nothing selected</span>
          <div className="ref-actions">
            <button className="small" onClick={() => setPicking(true)}>
              Choose…
            </button>
          </div>
        </div>
      )}
      {openError && <ErrorLine error={openError} />}
      {slot.kind === "row" && ref?.values && (
        <table className="ref-values">
          <tbody>
            {Object.entries(ref.values).map(([k, v]) => (
              <tr key={k}>
                <th>{k}</th>
                <td>{v === null ? <span className="faint">—</span> : String(v)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {picking && (
        <ReferencePicker
          dataset={dataset}
          slot={slot.id!}
          onClose={() => setPicking(false)}
          onPick={(c) => {
            setPicking(false);
            onPick(c);
          }}
        />
      )}
    </div>
  );
}
