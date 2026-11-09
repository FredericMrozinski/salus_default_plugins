import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useDebounced } from "../shared/hooks";
import { api, encode } from "../shared/salus";
import type { Automation, AutomationPreview, AutomationRule, DatasetDetail, Slot } from "../shared/types";
import { Icon } from "../shared/icons";
import { ErrorLine, Modal } from "../shared/ui";

type Mode = "empty" | "source" | "derive";
type Draft = { mode: Mode; from: string; read: string; value: string };

const kindText = (slot: Slot) => (slot.kind === "value" ? slot.value_type ?? "value" : slot.kind);

function toDrafts(schema: Slot[], automation: Automation | undefined): Record<string, Draft> {
  const out: Record<string, Draft> = {};
  for (const slot of schema) {
    const rule = automation?.rules[String(slot.id)];
    out[String(slot.id)] =
      rule?.mode === "derive"
        ? { mode: "derive", from: String(rule.from), read: rule.read, value: rule.value }
        : { mode: rule?.mode === "source" ? "source" : "empty", from: "", read: "", value: "" };
  }
  return out;
}

function toAutomation(drafts: Record<string, Draft>): Automation {
  const rules: Record<string, AutomationRule> = {};
  for (const [id, d] of Object.entries(drafts)) {
    if (d.mode === "source") rules[id] = { mode: "source" };
    else if (d.mode === "derive") rules[id] = { mode: "derive", from: Number(d.from), read: d.read, value: d.value };
  }
  return { rules };
}

/** Automation settings of a dataset: which field is created from a source cohort, how the others derive from fields. */
export function AutomationDialog({ dataset, onClose }: { dataset: DatasetDetail; onClose: () => void }) {
  const queryClient = useQueryClient();
  const schema = dataset.schema;
  const [drafts, setDrafts] = useState(() => toDrafts(schema, dataset.automation));
  const [sample, setSample] = useState<string | null>(null); // null: the first item of the source field
  const automation = toAutomation(drafts);
  const request = useDebounced(JSON.stringify({ ...automation, sample }), 300);
  const preview = useQuery({
    queryKey: ["automation-preview", dataset.id, request],
    queryFn: () => api<AutomationPreview>("POST", `/datasets/${encode(dataset.id)}/automation/preview`, { json: JSON.parse(request) }),
    placeholderData: (previous) => previous,
  });
  const save = useMutation({
    mutationFn: () => api("PUT", `/datasets/${encode(dataset.id)}/automation`, { json: automation }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dataset", dataset.id] });
      onClose();
    },
  });

  const set = (id: string, change: Partial<Draft>) =>
    setDrafts((all) => {
      const next = { ...all, [id]: { ...all[id], ...change } };
      if (change.mode === "source") for (const other of Object.keys(next)) if (other !== id && next[other].mode === "source") next[other] = { ...next[other], mode: "empty" };
      if (change.mode === "derive" && !next[id].from) {
        const source = Object.keys(next).find((k) => next[k].mode === "source" && k !== id);
        next[id] = { ...next[id], from: source ?? "", read: next[id].read || "^(.*)$", value: next[id].value || "\\1" };
      }
      return next;
    });

  const sourceId = Object.keys(drafts).find((k) => drafts[k].mode === "source");
  const p = preview.data;
  const samples = p?.samples ?? [];
  const custom = sample !== null && !samples.includes(sample);

  return (
    <Modal title="Automation" onClose={onClose} wide>
      <details className="muted auto-intro">
        <summary>
          One field can be <b>created from a source cohort</b>; the others are left empty or <b>derived</b> from a field.
        </summary>
        <p>
          Every refresh adds an entry for each item of the source field’s cohorts that no entry has yet. A derived field searches the <i>read</i> regex in the
          text of another field (file path, row key or value; also a derived field) and writes the <i>value</i> template with the groups: <code>\1</code>,{" "}
          <code>\2</code>, <code>\g&lt;name&gt;</code>, <code>\g&lt;0&gt;</code> for the whole match. A file or row field links the item the text names. Only empty
          fields are filled; when a derivation fails the field stays empty and is flagged.
        </p>
      </details>

      <div className="auto-sample">
        <span className="field-label">Preview with</span>
        <select
          value={custom ? "\u0000custom" : sample ?? p?.sample ?? ""}
          onChange={(e) => setSample(e.target.value === "\u0000custom" ? "" : e.target.value)}
          disabled={!sourceId && !custom}
        >
          {samples.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
          {samples.length === 0 && <option value="">{sourceId ? "(no items in the cohort yet)" : "(no source field)"}</option>}
          <option value={"\u0000custom"}>Your own text…</option>
        </select>
        {(custom || !sourceId) && (
          <input className="mono grow" placeholder="Sample text of the source field" value={sample ?? ""} onChange={(e) => setSample(e.target.value)} />
        )}
      </div>

      <div className="auto-rules">
        {schema.map((slot) => {
          const id = String(slot.id);
          const d = drafts[id];
          const result = p?.results[id];
          return (
            <div key={id} className={`auto-rule ${d.mode}`}>
              <div className="auto-rule-head">
                <span className="auto-name" title={slot.name}>
                  {d.mode === "source" && <Icon name="bolt" size={16} className="icon auto-bolt" title="Created from the source cohort" />}
                  {slot.label}
                </span>
                <span className="faint">{kindText(slot)}</span>
                <select value={d.mode} onChange={(e) => set(id, { mode: e.target.value as Mode })}>
                  <option value="empty">Leave empty</option>
                  {slot.kind !== "value" && <option value="source">Create from source cohort entry</option>}
                  <option value="derive">Derive from field</option>
                </select>
              </div>
              {d.mode === "source" && (
                <div className="auto-result">
                  <span className="faint">sample:</span> <span className="mono">{p?.sample || "—"}</span>
                </div>
              )}
              {d.mode === "derive" && (
                <>
                  <div className="auto-derive">
                    <select value={d.from} onChange={(e) => set(id, { from: e.target.value })} title="The field to read">
                      <option value="">Choose a field…</option>
                      {schema
                        .filter((s) => String(s.id) !== id)
                        .map((s) => (
                          <option key={s.id} value={String(s.id)}>
                            {s.label}
                          </option>
                        ))}
                    </select>
                    <input className="mono" placeholder="read regex, e.g. (P\d+)_" value={d.read} spellCheck={false} onChange={(e) => set(id, { read: e.target.value })} title="Read: a regex searched in the field's text" />
                    <span className="faint">→</span>
                    <input className="mono" placeholder="value, e.g. \1" value={d.value} spellCheck={false} onChange={(e) => set(id, { value: e.target.value })} title="Value: the template, with \1 … or \g<name>" />
                  </div>
                  <div className="auto-result">
                    {result?.error ? (
                      <span className="auto-error">✕ {result.error}</span>
                    ) : result?.text != null ? (
                      <>
                        <span className="faint">gives</span> <span className="mono auto-ok">{result.text}</span>
                        {result.note && <span className="faint"> · {result.note}</span>}
                      </>
                    ) : (
                      <span className="faint">{result?.note ?? "…"}</span>
                    )}
                  </div>
                </>
              )}
            </div>
          );
        })}
      </div>

      <ErrorLine error={save.error ?? preview.error ?? (p?.error ? new Error(p.error) : null)} />
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={save.isPending || !!p?.error} onClick={() => save.mutate()} title="Saves the settings and applies them now; later every refresh applies them">
          Save and apply
        </button>
      </div>
    </Modal>
  );
}
