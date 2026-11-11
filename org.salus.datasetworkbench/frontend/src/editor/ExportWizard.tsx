import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ReactNode, useEffect, useState } from "react";
import { bytes } from "../shared/format";
import { useDebounced } from "../shared/hooks";
import { api, encode } from "../shared/salus";
import type { DatasetDetail, ExportFormat, ExportOptions, ExportPreview, ExportStatus } from "../shared/types";
import { ErrorLine, Field, Modal, useDeleteWord } from "../shared/ui";

const STEPS = ["Entries", "Keys", "Sources", "Destination", "Export"] as const;
const EXTENSION: Record<ExportFormat, string> = { folder: "", zip: ".zip", json: ".json" };

/** Swaps the extension of a destination for another format's (a path the user typed keeps its folder and name). */
function withFormat(path: string, format: ExportFormat): string {
  if (!path) return path;
  return path.replace(/\.(zip|json)$/i, "") + EXTENSION[format];
}

/** Export of a dataset, a view in the center panel: steps like the Pharaoh plugin's. The backend (export.py) checks the
 *  options and the destination on every change (`/export/preview`), so each page shows what the export would do. */
export function ExportWizard({ dataset, onDone }: { dataset: DatasetDetail; onDone: () => void }) {
  const base = `/datasets/${encode(dataset.id)}/export`;
  const queryClient = useQueryClient();
  const [step, setStep] = useState(0);
  const [options, setOptions] = useState<ExportOptions>({
    include: { incomplete: false, corrupted: false },
    keys: Object.fromEntries(dataset.schema.map((s) => [String(s.id), s.name])),
    copy_sources: false,
    paths: "absolute",
    prefixes: {},
    format: "folder",
    destination: "",
  });
  const set = (change: Partial<ExportOptions>) => setOptions((o) => ({ ...o, ...change }));
  const request = useDebounced(JSON.stringify(options), 300);
  const preview = useQuery({
    queryKey: ["export-preview", dataset.id, request],
    queryFn: () => api<ExportPreview>("POST", `${base}/preview`, { json: JSON.parse(request) }),
    placeholderData: (previous) => previous,
  });
  const p = preview.data;
  // Until the user types a destination it follows the default for the format.
  useEffect(() => {
    if (!options.destination && p?.default_destination) set({ destination: p.default_destination });
  }, [p?.default_destination, options.destination]);

  // An export that is running (the wizard was closed and opened again) is shown at once.
  const status = useQuery({
    queryKey: ["export-status", dataset.id],
    queryFn: () => api<ExportStatus>("GET", base),
    refetchInterval: (q) => (q.state.data?.state === "running" ? 700 : false),
  });
  useEffect(() => {
    if (status.data?.state === "running" && step !== 4) setStep(4);
  }, [status.data?.state]);

  const start = useMutation({
    mutationFn: (overwrite: boolean) => api("POST", base, { json: { options, overwrite } }),
    onSuccess: () => {
      setConfirm(false);
      // not the last export's result while the first poll is under way
      queryClient.setQueryData<ExportStatus>(["export-status", dataset.id], { state: "running", progress: { phase: "collect" } });
      setStep(4);
    },
  });
  const [confirm, setConfirm] = useState(false);
  const exportNow = () => (p?.destination.replace ? setConfirm(true) : start.mutate(false));

  const pages = [<EntriesPage key="e" p={p} options={options} set={set} />, <KeysPage key="k" dataset={dataset} p={p} options={options} set={set} />,
    <SourcesPage key="s" p={p} options={options} set={set} />, <DestinationPage key="d" p={p} options={options} set={set} />,
    <ProgressPage key="r" status={status.data} />];
  const blocked = step === 1 ? !!p?.error : step === 3 ? !p || !!p.error || !!p.destination.error || preview.isFetching || request !== JSON.stringify(options) : false;
  const running = status.data?.state === "running";

  return (
    <div className="app">
      <div className="toolbar">
        <h2 title={dataset.name}>Export {dataset.name}</h2>
      </div>
      <div className="wiz-steps">
        {STEPS.map((name, i) => (
          <div key={name} className={`wiz-step ${i === step ? "current" : i < step ? "done" : ""}`}>
            <span className="wiz-num">{i < step ? "✓" : i + 1}</span>
            {name}
          </div>
        ))}
      </div>
      <div className="scroll wiz-page">
        <ErrorLine error={preview.error} />
        {pages[step]}
        {step === 3 && !confirm && <ErrorLine error={start.error} />}
      </div>
      <div className="wiz-footer">
        {step < 4 && <button onClick={onDone}>Cancel</button>}
        <span className="grow" />
        {step > 0 && step < 4 && <button onClick={() => setStep(step - 1)}>Back</button>}
        {step < 3 && (
          <button className="primary" disabled={blocked} onClick={() => setStep(step + 1)}>
            Next
          </button>
        )}
        {step === 3 && (
          <button className="primary" disabled={blocked || start.isPending} onClick={exportNow}>
            Export
          </button>
        )}
        {step === 4 && running && <button onClick={() => api("POST", `${base}/cancel`).then(() => status.refetch())}>Cancel export</button>}
        {step === 4 && !running && (
          <>
            <button onClick={() => setStep(3)}>Back</button>
            <button className="primary" onClick={onDone}>
              Done
            </button>
          </>
        )}
      </div>
      {confirm && p && <ReplaceDialog p={p} pending={start.isPending} error={start.error} onConfirm={() => start.mutate(true)} onClose={() => (setConfirm(false), start.reset())} />}
    </div>
  );
}

type PageProps = { p: ExportPreview | undefined; options: ExportOptions; set: (change: Partial<ExportOptions>) => void };

function Radio({ on, title, children, disabled, onPick }: { on: boolean; title: string; children?: ReactNode; disabled?: boolean; onPick: () => void }) {
  return (
    <label className={`wiz-radio ${on ? "on" : ""} ${disabled ? "disabled" : ""}`}>
      <input type="radio" checked={on} disabled={disabled} onChange={onPick} />
      <div className="wiz-radio-body">
        <strong>{title}</strong>
        {children}
      </div>
    </label>
  );
}

function EntriesPage({ p, options, set }: PageProps) {
  const counts = p?.counts ?? { valid: 0, incomplete: 0, corrupted: 0 };
  const option = (state: "incomplete" | "corrupted", text: string) => (
    <label className={`check wiz-check ${counts[state] === 0 ? "faint" : ""}`}>
      <input
        type="checkbox"
        disabled={counts[state] === 0}
        checked={options.include[state] && counts[state] > 0}
        onChange={(e) => set({ include: { ...options.include, [state]: e.target.checked } })}
      />
      <span>
        Also export the <b>{counts[state]}</b> {state} {counts[state] === 1 ? "entry" : "entries"}
        <span className="muted"> — {text}</span>
      </span>
    </label>
  );
  return (
    <div className="wiz-body">
      <h3>Which entries?</h3>
      <p className="muted">
        The dataset is exported as one JSON file: an object per entry with a key for every field. The <b>{counts.valid}</b> valid{" "}
        {counts.valid === 1 ? "entry is" : "entries are"} always exported.
      </p>
      {option("incomplete", "a required field is empty or a value does not fit its field; such fields are exported as they are, empty ones as null")}
      {option("corrupted", "a file or row they use was changed or deleted; deleted ones are exported as null")}
      {p && <p className="muted">{p.entries} {p.entries === 1 ? "entry" : "entries"} will be exported.</p>}
    </div>
  );
}

function KeysPage({ dataset, p, options, set }: PageProps & { dataset: DatasetDetail }) {
  return (
    <div className="wiz-body">
      <h3>The keys of the entries</h3>
      <p className="muted">Each field becomes a key of every exported entry. By default it is the field’s name; change it here to rename it in the export only.</p>
      <table className="wiz-table">
        <thead>
          <tr>
            <th>Field</th>
            <th>Kind</th>
            <th>Key in the export</th>
          </tr>
        </thead>
        <tbody>
          {dataset.schema.map((slot) => {
            const id = String(slot.id);
            return (
              <tr key={id}>
                <td title={slot.name}>{slot.label}</td>
                <td className="muted">{slot.kind === "value" ? slot.value_type : slot.kind}</td>
                <td>
                  <input className="mono" value={options.keys[id] ?? ""} spellCheck={false} onChange={(e) => set({ keys: { ...options.keys, [id]: e.target.value } })} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="row">
        <button className="small" onClick={() => set({ keys: Object.fromEntries(dataset.schema.map((s) => [String(s.id), s.name])) })}>
          Use the field names
        </button>
      </div>
      {p?.error && <div className="error-line">{p.error}</div>}
      {p?.sample && (
        <Field label="The first entry would be">
          <pre className="wiz-sample mono">{JSON.stringify(p.sample, null, 2)}</pre>
        </Field>
      )}
    </div>
  );
}

function SourcesPage({ p, options, set }: PageProps) {
  const fileCohorts = (p?.sources ?? []).filter((s) => s.kind === "file");
  const example = (prefix: string) => (prefix ? (/[\\/]$/.test(prefix) ? prefix : `${prefix}/`) : "") + "folder/slide.svs";
  return (
    <div className="wiz-body">
      <h3>The source cohorts</h3>
      <p className="muted">File fields are exported as the path of their file; row fields as the row’s key and values.</p>
      <div className="wiz-radios">
        <Radio on={options.copy_sources} title="Copy the source files into the export" onPick={() => set({ copy_sources: true, format: options.format === "json" ? "folder" : options.format, destination: options.format === "json" ? withFormat(options.destination, "folder") : options.destination })}>
          <span className="muted">
            The files the exported entries use (and the CSV of each table cohort) are duplicated into the export, under <code>sources/</code> with the cohorts’ folders; the
            JSON names them relative to itself, so the export can be moved anywhere.
            {p && options.copy_sources && ` ${p.files} files, ${bytes(p.bytes)}.`}
          </span>
        </Radio>
        <Radio on={!options.copy_sources} title="Refer to the files where they are" onPick={() => set({ copy_sources: false })}>
          <span className="muted">Only the JSON is written; it names the files on this server.</span>
          {!options.copy_sources && (
            <div className="wiz-radios inner">
              <Radio on={options.paths === "absolute"} title="Absolute paths" onPick={() => set({ paths: "absolute" })}>
                <span className="muted">The full path on this server, e.g. <code>{fileCohorts[0] ? `${fileCohorts[0].location ?? ""}/folder/slide.svs` : "/…/cohort/folder/slide.svs"}</code></span>
              </Radio>
              <Radio on={options.paths === "relative"} title="Paths inside each cohort" onPick={() => set({ paths: "relative" })}>
                <span className="muted">The path relative to the cohort’s folder, after a prefix you may give per cohort (none: just the relative path).</span>
                {options.paths === "relative" && (
                  fileCohorts.length === 0 ? (
                    <span className="faint">No field refers to a file cohort.</span>
                  ) : (
                    <table className="wiz-table">
                      <thead>
                        <tr>
                          <th>Cohort</th>
                          <th>Prefix</th>
                          <th>Example</th>
                        </tr>
                      </thead>
                      <tbody>
                        {fileCohorts.map((s) => {
                          const prefix = options.prefixes[s.id] ?? "";
                          return (
                            <tr key={s.id}>
                              <td title={s.folder}>{s.name}</td>
                              <td>
                                <input className="mono" placeholder="none" value={prefix} spellCheck={false} onChange={(e) => set({ prefixes: { ...options.prefixes, [s.id]: e.target.value } })} />
                              </td>
                              <td className="mono muted">{example(prefix.trim())}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  )
                )}
              </Radio>
            </div>
          )}
        </Radio>
      </div>
    </div>
  );
}

function DestinationPage({ p, options, set }: PageProps) {
  const format = (f: ExportFormat) => set({ format: f, destination: withFormat(options.destination, f) });
  const d = p?.destination;
  return (
    <div className="wiz-body">
      <h3>Where to?</h3>
      <div className="wiz-radios">
        <Radio on={options.format === "folder"} title="A folder" onPick={() => format("folder")}>
          <span className="muted">
            <code>dataset.json</code>
            {options.copy_sources && <> and the copied files under <code>sources/</code></>} in a new folder.
          </span>
        </Radio>
        <Radio on={options.format === "zip"} title="A zip file" onPick={() => format("zip")}>
          <span className="muted">The same in one zip file{options.copy_sources && " (the files are stored, not compressed again)"}.</span>
        </Radio>
        <Radio on={options.format === "json"} title="Only the JSON file" disabled={options.copy_sources} onPick={() => format("json")}>
          <span className="muted">{options.copy_sources ? "Not with copied source files: they need a folder or a zip file." : "One standalone JSON file."}</span>
        </Radio>
      </div>
      <Field label={options.format === "folder" ? "Folder (on this server)" : "File (on this server)"}>
        <input className="mono" value={options.destination} spellCheck={false} onChange={(e) => set({ destination: e.target.value })} />
      </Field>
      {d?.error && <div className="error-line">{d.error}</div>}
      {d && !d.error && d.replace && (
        <div className="warn-line">{d.replace === "folder" ? "This folder holds an earlier export: it will be replaced (you are asked first)." : "This file exists: it will be replaced (you are asked first)."}</div>
      )}
      {p?.error && <div className="error-line">{p.error}</div>}
      {p && (
        <div className="wiz-summary">
          <div>
            <span className="field-label">Entries</span>
            <b>{p.entries}</b>
          </div>
          <div>
            <span className="field-label">Fields</span>
            <b>{p.fields.length}</b>
          </div>
          {options.copy_sources && (
            <div>
              <span className="field-label">Copied files</span>
              <b>
                {p.files} · {bytes(p.bytes)}
              </b>
            </div>
          )}
          {p.free !== null && (
            <div>
              <span className="field-label">Free there</span>
              <b className={options.copy_sources && p.bytes > p.free ? "auto-error" : ""}>{bytes(p.free)}</b>
            </div>
          )}
          {p.missing > 0 && (
            <div>
              <span className="field-label">Missing files or rows</span>
              <b className="auto-error">{p.missing}</b>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ProgressPage({ status }: { status: ExportStatus | undefined }) {
  if (!status || status.state === "idle") return <div className="wiz-body muted">Starting…</div>;
  const pr = status.progress ?? {};
  const fraction = pr.total_bytes ? (pr.bytes ?? 0) / pr.total_bytes : pr.phase === "finish" ? 1 : 0;
  if (status.state === "running")
    return (
      <div className="wiz-body">
        <h3>Exporting…</h3>
        <div className="wiz-bar">
          <div style={{ width: `${Math.round(fraction * 100)}%` }} />
        </div>
        <div className="muted">
          {pr.phase === "collect" && "Reading the entries…"}
          {pr.phase === "copy" && `${pr.done ?? 0} of ${pr.total ?? 0} files · ${bytes(pr.bytes ?? 0)} of ${bytes(pr.total_bytes ?? 0)}`}
          {pr.phase === "finish" && "Putting the export in place…"}
        </div>
      </div>
    );
  if (status.state === "done" && status.result)
    return (
      <div className="wiz-body">
        <h3>Exported</h3>
        <div className="wiz-bar done">
          <div style={{ width: "100%" }} />
        </div>
        <p>
          {status.result.entries} {status.result.entries === 1 ? "entry" : "entries"}
          {status.result.files > 0 && ` and ${status.result.files} files (${bytes(status.result.bytes)})`} to
        </p>
        <pre className="wiz-sample mono">{status.result.path}</pre>
        {status.result.missing > 0 && <div className="warn-line">{status.result.missing} referenced files or rows were gone and are exported as null.</div>}
      </div>
    );
  return (
    <div className="wiz-body">
      <h3>{status.state === "cancelled" ? "Cancelled" : "The export failed"}</h3>
      <p className="muted">{status.state === "cancelled" ? "Nothing was written; an earlier export at the destination is unchanged." : status.error}</p>
    </div>
  );
}

/** Replacing an earlier export: a file needs a click, a folder (everything in it goes) the word "delete". */
function ReplaceDialog({ p, pending, error, onConfirm, onClose }: { p: ExportPreview; pending: boolean; error: unknown; onConfirm: () => void; onClose: () => void }) {
  const folder = p.destination.replace === "folder";
  const [field, typed] = useDeleteWord();
  return (
    <Modal title={folder ? "Replace the earlier export?" : "Replace the file?"} onClose={onClose}>
      <div>
        <span className="mono">{p.destination.path}</span>{" "}
        {folder
          ? "holds an earlier export. The folder and everything in it are deleted and replaced by the new export once that is complete."
          : "exists. It is replaced by the new export once that is complete."}
      </div>
      {folder && field}
      <ErrorLine error={error} />
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary danger" disabled={pending || (folder && !typed)} onClick={onConfirm}>
          Replace
        </button>
      </div>
    </Modal>
  );
}
