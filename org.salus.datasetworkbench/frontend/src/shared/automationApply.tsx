import { useState } from "react";
import { api, encode } from "./salus";
import type { Automation } from "./types";
import { ErrorLine, Modal } from "./ui";

export type ApplyMode = "fill" | "overwrite";
type Conflicts = { conflicts: number; examples: { entry: number; field: string; current: string; derived: string }[] };
type Pending = Conflicts & { apply: (mode: ApplyMode) => Promise<unknown> };

/**
 * Applying the automation by hand (Save and apply in the automation view, Run automation now in the entries' menu).
 * First asks the backend whether existing values would change (POST …/automation/conflicts); if none would, it applies
 * right away, filling the empty fields. Otherwise a dialog shows how many values differ, with examples, and offers
 * Cancel, Fill in missing (the existing values stay) and Overwrite existing (they get the derived ones).
 *
 * `start(apply, rules)`: `apply(mode)` does the actual request; `rules` are unsaved settings to check (none: the saved
 * ones). Returns the dialog to render, and `busy` while checking or applying.
 */
export function useAutomationApply(dataset: string, onApplied?: () => void) {
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const run = async (apply: (mode: ApplyMode) => Promise<unknown>, mode: ApplyMode) => {
    setBusy(true);
    try {
      await apply(mode);
      setPending(null);
      setError(null);
      onApplied?.();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const start = async (apply: (mode: ApplyMode) => Promise<unknown>, rules?: Automation) => {
    setBusy(true);
    setError(null);
    try {
      const found = await api<Conflicts>("POST", `/datasets/${encode(dataset)}/automation/conflicts`, { json: rules ?? {} });
      if (found.conflicts === 0) await run(apply, "fill");
      else setPending({ ...found, apply });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const close = () => (setPending(null), setError(null));
  const dialog = pending && (
    <Modal title="Existing values differ" onClose={close} wide>
      <div>
        In {pending.conflicts === 1 ? "one field" : `${pending.conflicts} fields`} of existing entries the automation would give a different value
        than the one there now (typed by hand, or from earlier settings).
      </div>
      <table className="conflict-table">
        <thead>
          <tr>
            <th>Entry</th>
            <th>Field</th>
            <th>Now</th>
            <th>Automation</th>
          </tr>
        </thead>
        <tbody>
          {pending.examples.map((e) => (
            <tr key={`${e.entry}-${e.field}`}>
              <td>#{e.entry}</td>
              <td>{e.field}</td>
              <td className="mono">{e.current}</td>
              <td className="mono">{e.derived}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {pending.conflicts > pending.examples.length && <div className="faint">… and {pending.conflicts - pending.examples.length} more.</div>}
      <div className="muted">
        <b>Fill in missing</b> only fills empty fields and keeps these values. <b>Overwrite existing</b> replaces them with the automation’s.
      </div>
      <ErrorLine error={error} />
      <div className="modal-actions">
        <button onClick={close} disabled={busy}>
          Cancel
        </button>
        <button onClick={() => run(pending.apply, "fill")} disabled={busy}>
          Fill in missing
        </button>
        <button className="primary danger" onClick={() => run(pending.apply, "overwrite")} disabled={busy}>
          Overwrite existing
        </button>
      </div>
    </Modal>
  );
  return { start, dialog, busy, error: pending ? null : error };
}
