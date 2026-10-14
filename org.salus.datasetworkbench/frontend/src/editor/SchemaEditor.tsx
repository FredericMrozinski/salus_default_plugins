import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, encode } from "../shared/salus";
import { Constraints, DatasetDetail, Slot, SlotKind, SourceNode, VALUE_TYPES } from "../shared/types";
import { ErrorLine, Field, flattenSources, useTree } from "../shared/ui";

/** A slot as edited: numbers and lists are held as text until saved. */
type Draft = {
  key: number;
  id?: number;
  name: string;
  label: string;
  description: string;
  required: boolean;
  unique: boolean;
  kind: SlotKind;
  value_type: Slot["value_type"];
  min: string;
  max: string;
  unit: string;
  max_length: string;
  regex: string;
  choices: string;
  default: string;
  sources: string[];
  extensions: string;
  columns: string[];
  originalKind?: SlotKind;
};

let nextKey = 1;

function toDraft(slot: Slot): Draft {
  const c = slot.constraints;
  const text = (v: unknown) => (v === undefined || v === null ? "" : String(v));
  return {
    key: nextKey++,
    id: slot.id,
    name: slot.name,
    label: slot.label,
    description: slot.description,
    required: slot.required,
    unique: slot.unique,
    kind: slot.kind,
    originalKind: slot.id ? slot.kind : undefined,
    value_type: slot.value_type,
    min: text(c.min),
    max: text(c.max),
    unit: text(c.unit),
    max_length: text(c.max_length),
    regex: text(c.regex),
    choices: (c.choices ?? []).join(", "),
    default: text(c.default),
    sources: slot.sources,
    extensions: slot.extensions.join(", "),
    columns: slot.columns,
  };
}

function fromDraft(d: Draft): Partial<Slot> {
  const constraints: Constraints & Record<string, unknown> = {};
  if (d.kind === "value") {
    if (d.default !== "") constraints.default = d.default;
    if (d.value_type === "integer" || d.value_type === "float") {
      if (d.min !== "") constraints.min = Number(d.min);
      if (d.max !== "") constraints.max = Number(d.max);
      if (d.unit) constraints.unit = d.unit;
    }
    if (d.value_type === "text") {
      if (d.max_length !== "") constraints.max_length = Number(d.max_length);
      if (d.regex) constraints.regex = d.regex;
    }
    if (d.value_type === "choice") constraints.choices = d.choices.split(",").map((s) => s.trim()).filter(Boolean);
  }
  const list = (s: string) => s.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean);
  return {
    id: d.id,
    name: d.name,
    label: d.label || d.name,
    description: d.description,
    required: d.required,
    unique: d.unique,
    kind: d.kind,
    value_type: d.kind === "value" ? d.value_type : null,
    constraints,
    sources: d.kind === "value" ? [] : d.sources,
    extensions: d.kind === "file" ? list(d.extensions) : [],
    columns: d.kind === "row" ? d.columns : [],
  };
}

const blank = (n: number): Draft =>
  toDraft({ name: `field_${n}`, label: "", description: "", required: false, unique: false, kind: "value", value_type: "text", constraints: {}, sources: [], extensions: [], columns: [] });

export function SchemaEditor({ dataset, onDone }: { dataset: DatasetDetail; onDone: () => void }) {
  const queryClient = useQueryClient();
  const tree = useTree();
  const sources = flattenSources(tree.data ?? []);
  const [name, setName] = useState(dataset.name);
  const [drafts, setDrafts] = useState<Draft[]>(() => dataset.schema.map(toDraft));
  const dangling = new Map(dataset.schema.map((s) => [s.id, s.dangling_sources ?? []]));

  const save = useMutation({
    mutationFn: async () => {
      if (name.trim() !== dataset.name) await api("PATCH", `/datasets/${encode(dataset.id)}`, { json: { name } });
      return api<DatasetDetail>("PUT", `/datasets/${encode(dataset.id)}/schema`, { json: { slots: drafts.map(fromDraft) } });
    },
    onSuccess: (saved) => {
      queryClient.setQueryData(["dataset", dataset.id], saved);
      queryClient.invalidateQueries({ queryKey: ["datasets"] });
      onDone();
    },
  });

  const update = (key: number, patch: Partial<Draft>) => setDrafts((all) => all.map((d) => (d.key === key ? { ...d, ...patch } : d)));
  const move = (index: number, by: number) =>
    setDrafts((all) => {
      const copy = [...all];
      const [item] = copy.splice(index, 1);
      copy.splice(index + by, 0, item);
      return copy;
    });
  const remove = (d: Draft) => {
    if (d.id && !window.confirm(`Remove the field “${d.label || d.name}”? Its values in all entries are deleted when you save.`)) return;
    setDrafts((all) => all.filter((x) => x.key !== d.key));
  };

  return (
    <div className="app">
      <div className="toolbar">
        <h2>Schema of</h2>
        <input className="grow" value={name} onChange={(e) => setName(e.target.value)} aria-label="Dataset name" />
        <button onClick={onDone}>Cancel</button>
        <button className="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          Save schema
        </button>
      </div>
      <div className="scroll form-scroll">
        <div className="form">
          <div className="muted">
            Every entry has one value per field. Changing the schema never blocks you: entries that no longer fit are marked <i>incomplete</i> and can be fixed one by one.
          </div>
          <ErrorLine error={save.error} />
          {drafts.map((d, index) => (
            <SlotCard
              key={d.key}
              draft={d}
              sources={sources}
              dangling={d.id ? dangling.get(d.id) ?? [] : []}
              first={index === 0}
              last={index === drafts.length - 1}
              onChange={(patch) => update(d.key, patch)}
              onMove={(by) => move(index, by)}
              onRemove={() => remove(d)}
            />
          ))}
          <div>
            <button onClick={() => setDrafts([...drafts, blank(drafts.length + 1)])}>+ Add field</button>
          </div>
        </div>
      </div>
    </div>
  );
}

function SlotCard({ draft: d, sources, dangling, first, last, onChange, onMove, onRemove }: {
  draft: Draft;
  sources: SourceNode[];
  dangling: string[];
  first: boolean;
  last: boolean;
  onChange: (patch: Partial<Draft>) => void;
  onMove: (by: number) => void;
  onRemove: () => void;
}) {
  const wanted = d.kind === "file" ? "file" : "table";
  const candidates = sources.filter((s) => s.kind === wanted && s.id);
  const columns = [...new Set(candidates.filter((s) => d.sources.includes(s.id!)).flatMap((s) => (s.columns ?? []).map((c) => c.name)))];
  const toggle = (list: string[], value: string) => (list.includes(value) ? list.filter((x) => x !== value) : [...list, value]);

  return (
    <div className="slot-card">
      <div className="slot-card-head">
        <input className="mono" value={d.name} onChange={(e) => onChange({ name: e.target.value })} aria-label="Field name" placeholder="name" />
        <input value={d.label} onChange={(e) => onChange({ label: e.target.value })} aria-label="Label" placeholder="Label" />
        <select value={d.kind} onChange={(e) => onChange({ kind: e.target.value as SlotKind, sources: [], columns: [], value_type: e.target.value === "value" ? d.value_type ?? "text" : null })}>
          <option value="value">value</option>
          <option value="file">file</option>
          <option value="row">table row</option>
        </select>
        <button className="icon-button" disabled={first} onClick={() => onMove(-1)} title="Move up">
          ▲
        </button>
        <button className="icon-button" disabled={last} onClick={() => onMove(1)} title="Move down">
          ▼
        </button>
        <button className="icon-button" onClick={onRemove} title="Remove this field">
          ✕
        </button>
      </div>
      <div className="slot-card-body">
        <div className="row">
          <label className="check">
            <input type="checkbox" checked={d.required} onChange={(e) => onChange({ required: e.target.checked })} /> Required
          </label>
          <label className="check" title="No two entries may have the same value">
            <input type="checkbox" checked={d.unique} onChange={(e) => onChange({ unique: e.target.checked })} /> Unique
          </label>
        </div>
        <input value={d.description} onChange={(e) => onChange({ description: e.target.value })} placeholder="Description (optional)" />
        {d.originalKind && d.originalKind !== d.kind && <div className="warn-line">Changing the kind clears this field’s values in all entries when you save.</div>}

        {d.kind === "value" && (
          <div className="grid2">
            <Field label="Type">
              <select value={d.value_type ?? "text"} onChange={(e) => onChange({ value_type: e.target.value as Draft["value_type"] })}>
                {VALUE_TYPES.map((t) => (
                  <option key={t}>{t}</option>
                ))}
              </select>
            </Field>
            {(d.value_type === "integer" || d.value_type === "float") && (
              <>
                <Field label="Minimum">
                  <input value={d.min} onChange={(e) => onChange({ min: e.target.value })} />
                </Field>
                <Field label="Maximum">
                  <input value={d.max} onChange={(e) => onChange({ max: e.target.value })} />
                </Field>
                <Field label="Unit (shown next to the value)">
                  <input value={d.unit} onChange={(e) => onChange({ unit: e.target.value })} />
                </Field>
              </>
            )}
            {d.value_type === "text" && (
              <>
                <Field label="Maximum length">
                  <input value={d.max_length} onChange={(e) => onChange({ max_length: e.target.value })} />
                </Field>
                <Field label="Pattern (regular expression)">
                  <input className="mono" value={d.regex} onChange={(e) => onChange({ regex: e.target.value })} />
                </Field>
              </>
            )}
            {d.value_type === "choice" && (
              <Field label="Allowed values" hint="Separated by commas">
                <input value={d.choices} onChange={(e) => onChange({ choices: e.target.value })} />
              </Field>
            )}
            <Field label="Default for new entries">
              <input value={d.default} onChange={(e) => onChange({ default: e.target.value })} />
            </Field>
          </div>
        )}

        {d.kind !== "value" && (
          <>
            <div className="field">
              <span className="field-label">{d.kind === "file" ? "Sources of files" : "Tables"} (at least one)</span>
              {candidates.length === 0 && <span className="faint">There is no {wanted} source yet. Create one in the Workbench.</span>}
              {candidates.map((s) => (
                <label key={s.id!} className="check">
                  <input type="checkbox" checked={d.sources.includes(s.id!)} onChange={() => onChange({ sources: toggle(d.sources, s.id!) })} />
                  {s.path}
                </label>
              ))}
              {dangling.map((id) => (
                <span key={id} className="warn-line">
                  A source used here was removed ({id}). Entries referencing it are “source missing”.
                </span>
              ))}
            </div>
            {d.kind === "file" && (
              <Field label="File types" hint="Extensions separated by commas, e.g. svs, tif. Empty means any file.">
                <input value={d.extensions} onChange={(e) => onChange({ extensions: e.target.value })} />
              </Field>
            )}
            {d.kind === "row" && (
              <div className="field">
                <span className="field-label">Columns of the row that belong to the dataset</span>
                <span className="field-hint">None checked means all columns. Choose only what the dataset may contain (e.g. a pseudonym, not a name).</span>
                {columns.length === 0 && <span className="faint">Choose a table first.</span>}
                <div className="row wrap">
                  {columns.map((c) => (
                    <label key={c} className="check">
                      <input type="checkbox" checked={d.columns.includes(c)} onChange={() => onChange({ columns: toggle(d.columns, c) })} />
                      <span className="mono">{c}</span>
                    </label>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
