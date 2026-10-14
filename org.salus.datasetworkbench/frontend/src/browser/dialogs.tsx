import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, encode } from "../shared/salus";
import type { Column, Issue, SourceNode, TreeNode } from "../shared/types";
import { ErrorLine, Field, Modal, Empty, useTree } from "../shared/ui";

export function NewGroupDialog({ parent, onClose }: { parent: string; onClose: () => void }) {
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => api("POST", "/groups", { json: { parent, name } }),
    onSuccess: onClose,
  });
  return (
    <Modal title="New folder" onClose={onClose}>
      <Field label="Name" hint={`In ${parent || "the top level"}`}>
        <input autoFocus value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && name && create.mutate()} />
      </Field>
      <ErrorLine error={create.error} />
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!name.trim() || create.isPending} onClick={() => create.mutate()}>
          Create
        </button>
      </div>
    </Modal>
  );
}

export function NewSourceDialog({ group, kind, onClose, onCreated }: { group: string; kind: "file" | "table"; onClose: () => void; onCreated: (source: SourceNode) => void }) {
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => api<SourceNode>("POST", "/sources", { json: { group, name, kind } }),
    onSuccess: (source) => {
      onCreated(source);
      onClose();
    },
  });
  return (
    <Modal title={kind === "file" ? "New general source cohort" : "New tabular source cohort"} onClose={onClose}>
      <Field label="Name" hint={`In ${group || "the top level"}. An existing folder with that name is adopted.`}>
        <input autoFocus value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && name.trim() && !create.isPending && create.mutate()} />
      </Field>
      <div className="muted">
        {kind === "file"
          ? "A general source cohort holds files of any kind: slides, images, PDFs, reports …"
          : "A tabular source cohort is one CSV file; every row is an item that datasets can use."}
      </div>
      <ErrorLine error={create.error} />
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!name.trim() || create.isPending} onClick={() => create.mutate()}>
          Create
        </button>
      </div>
    </Modal>
  );
}

const TYPES = ["text", "integer", "float", "boolean", "date", "datetime"];

export function TableSetupDialog({ source, onClose }: { source: SourceNode; onClose: () => void }) {
  const [columns, setColumns] = useState<Column[] | null>(null);
  const [autoKey, setAutoKey] = useState(false);
  const suggestion = useQuery({
    queryKey: ["suggest", source.id],
    queryFn: () => api<{ csv: string; columns: Column[]; sampled: number; ignored: number; auto_key: boolean }>("GET", `/sources/${encode(source.id!)}/suggest-columns`),
    gcTime: 0,
  });
  useEffect(() => {
    if (!suggestion.data) return;
    // Keep what was declared before for columns that still exist; the rest is the inference.
    const declared = new Map((source.columns ?? []).map((c) => [c.name, c]));
    const hasDeclared = declared.size > 0 && suggestion.data.columns.every((c) => declared.has(c.name));
    setColumns(suggestion.data.columns.map((c) => (hasDeclared ? { ...declared.get(c.name)! } : c)));
    setAutoKey(hasDeclared ? !!source.auto_key : suggestion.data.auto_key);
  }, [suggestion.data, source.columns]);
  const save = useMutation({
    mutationFn: () => api("PUT", `/sources/${encode(source.id!)}/columns`, { json: { columns, auto_key: autoKey } }),
    onSuccess: onClose,
  });
  return (
    <Modal title={`Columns of ${source.name}`} onClose={onClose} wide>
      {suggestion.isLoading && <Empty>Reading the CSV file…</Empty>}
      <ErrorLine error={suggestion.error} />
      {columns && (
        <>
          {(suggestion.data?.ignored ?? 0) > 0 && (
            <div className="muted">
              {suggestion.data!.ignored} column(s) without a name are ignored.
            </div>
          )}
          <div className="muted">
            Check the types of {suggestion.data?.csv} and choose the <b>key</b>, which identifies each row. Rows that do not fit are reported and cannot be used.
          </div>
          <div className="field">
            <label className="check">
              <input type="radio" name="keymode" checked={!autoKey} onChange={() => setAutoKey(false)} />
              Use a column as the key (its values must be unique and never empty)
            </label>
            <label className="check">
              <input type="radio" name="keymode" checked={autoKey} onChange={() => setAutoKey(true)} />
              Generate the keys: each row is identified by its position in the file (1, 2, 3 …)
            </label>
            {autoKey && (
              <span className="field-hint">
                Rows added at the end are safe. Inserting or reordering rows shifts the keys; entries that use the shifted rows then show “source changed” when the selected columns differ.
              </span>
            )}
          </div>
          <table className="setup-table">
            <thead>
              <tr>
                <th>Column</th>
                <th>Type</th>
                <th>Key</th>
              </tr>
            </thead>
            <tbody>
              {columns.map((c, i) => (
                <tr key={c.name}>
                  <td className="mono">{c.name}</td>
                  <td>
                    <select value={c.type} onChange={(e) => setColumns(columns.map((x, j) => (j === i ? { ...x, type: e.target.value } : x)))}>
                      {TYPES.map((t) => (
                        <option key={t}>{t}</option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input type="radio" name="key" disabled={autoKey} checked={c.is_key && !autoKey} onChange={() => setColumns(columns.map((x, j) => ({ ...x, is_key: j === i })))} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <ErrorLine error={save.error} />
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!columns || save.isPending || (!autoKey && !columns.some((c) => c.is_key))} onClick={() => save.mutate()}>
          Use these columns
        </button>
      </div>
    </Modal>
  );
}

export function IssuesDialog({ source, onClose }: { source: SourceNode; onClose: () => void }) {
  const issues = useQuery({ queryKey: ["issues", source.id], queryFn: () => api<Issue[]>("GET", `/sources/${encode(source.id!)}/issues`, { query: { limit: 500 } }) });
  return (
    <Modal title={`Rows that cannot be used in ${source.name}`} onClose={onClose} wide>
      {issues.isLoading && <Empty>Loading…</Empty>}
      <ErrorLine error={issues.error} />
      {issues.data && issues.data.length === 0 && <Empty>No problems.</Empty>}
      {issues.data && issues.data.length > 0 && (
        <>
          <div className="muted">
            {source.issues} problem(s){(source.issues ?? 0) > issues.data.length ? `, showing ${issues.data.length}` : ""}. Fix the CSV file and upload it again.
          </div>
          <div className="issue-list">
            <table>
              <thead>
                <tr>
                  <th>Line</th>
                  <th>Key</th>
                  <th>Column</th>
                  <th>Problem</th>
                </tr>
              </thead>
              <tbody>
                {issues.data.map((i, n) => (
                  <tr key={n}>
                    <td>{i.row_number}</td>
                    <td className="mono">{i.key}</td>
                    <td className="mono">{i.column}</td>
                    <td>{i.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Modal>
  );
}

export type Usage = { entries: number; datasets: { id: string; name: string; entries: number }[] };

/** Says which dataset entries are affected by an action. */
export function UsageNote({ usage, what, consequence }: { usage: Usage | undefined; what: string; consequence: string }) {
  if (!usage) return <div className="faint">Checking which datasets use {what}…</div>;
  if (usage.entries === 0) return <div className="faint">No dataset entry uses {what}.</div>;
  return (
    <div className="warn-line">
      {usage.entries} dataset entr{usage.entries === 1 ? "y" : "ies"} use {what} ({usage.datasets.map((d) => `${d.name}: ${d.entries}`).join(", ")}). {consequence}
    </div>
  );
}

export function RenameDialog({ title, current, hint, run, onClose }: { title: string; current: string; hint?: string; run: (name: string) => Promise<unknown>; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(current);
  const rename = useMutation({
    mutationFn: () => run(name.trim()),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tree"] });
      queryClient.invalidateQueries({ queryKey: ["datasets"] });
      onClose();
    },
  });
  const unchanged = name.trim() === current;
  return (
    <Modal title={title} onClose={onClose}>
      <Field label="New name" hint={hint}>
        <input
          autoFocus
          value={name}
          onFocus={(e) => e.target.setSelectionRange(0, current.includes(".") ? current.lastIndexOf(".") : current.length)}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && name.trim() && !unchanged && rename.mutate()}
        />
      </Field>
      <ErrorLine error={rename.error} />
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!name.trim() || unchanged || rename.isPending} onClick={() => rename.mutate()}>
          Rename
        </button>
      </div>
    </Modal>
  );
}

export function DeleteItemDialog({ source, path, folder, onClose }: { source: SourceNode; path: string; folder: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const usage = useQuery({
    queryKey: ["usage", source.id, path],
    queryFn: () => api<Usage>("GET", `/sources/${encode(source.id!)}/usage`, { query: { path } }),
    gcTime: 0,
  });
  const remove = useMutation({
    mutationFn: () => api("DELETE", `/sources/${encode(source.id!)}/items`, { query: { path } }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tree"] });
      queryClient.invalidateQueries({ queryKey: ["files", source.id] });
      onClose();
    },
  });
  const name = path.split("/").pop();
  return (
    <Modal title={`Delete “${name}”`} onClose={onClose}>
      <div>
        {folder ? "The folder and everything in it are" : "The file is"} deleted from disk. That cannot be undone.
      </div>
      <UsageNote usage={usage.data} what={folder ? "files in it" : "this file"} consequence="They will show “source missing”." />
      <ErrorLine error={remove.error ?? usage.error} />
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="danger" disabled={remove.isPending} onClick={() => remove.mutate()}>
          Delete from disk
        </button>
      </div>
    </Modal>
  );
}

export function RemoveSourceDialog({ source, onClose }: { source: SourceNode; onClose: () => void }) {
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: (files: boolean) => api("DELETE", `/sources/${encode(source.id!)}`, { query: { files: files ? "1" : "0" } }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tree"] });
      onClose();
    },
  });
  const count = source.kind === "file" ? `${source.items ?? 0} file(s)` : "its CSV file";
  const usage = useQuery({ queryKey: ["usage", source.id, ""], queryFn: () => api<Usage>("GET", `/sources/${encode(source.id!)}/usage`), gcTime: 0 });
  return (
    <Modal title={`Remove source “${source.name}”`} onClose={onClose}>
      <UsageNote usage={usage.data} what="this source" consequence="They will show “source missing” either way." />
      <div className="muted">
        <b>Remove only</b> forgets the source; the folder and {count} stay on disk. <b>Delete from disk</b> also deletes the folder with everything in it. That cannot be undone.
      </div>
      <ErrorLine error={remove.error} />
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button disabled={remove.isPending} onClick={() => remove.mutate(false)}>
          Remove only
        </button>
        <button className="danger" disabled={remove.isPending} onClick={() => remove.mutate(true)}>
          Delete from disk
        </button>
      </div>
    </Modal>
  );
}

export function DeleteGroupDialog({ path, onClose }: { path: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const contents = useQuery({ queryKey: ["group", path], queryFn: () => api<{ sources: string[]; empty: boolean; usage: Usage }>("GET", "/groups/contents", { query: { path } }), gcTime: 0 });
  const remove = useMutation({
    mutationFn: () => api("DELETE", "/groups", { query: { path, force: "1" } }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tree"] });
      onClose();
    },
  });
  const n = contents.data?.sources.length ?? 0;
  return (
    <Modal title={`Delete folder “${path}”`} onClose={onClose}>
      <ErrorLine error={contents.error} />
      {contents.isLoading && <Empty>Looking inside…</Empty>}
      {contents.data &&
        (contents.data.empty ? (
          <div>The folder is empty.</div>
        ) : (
          <div>
            The folder and <b>everything in it</b> are deleted from disk, including {n} source{n === 1 ? "" : "s"} with all their files. That cannot be undone.
          </div>
        ))}
      {contents.data && !contents.data.empty && <UsageNote usage={contents.data.usage} what="these sources" consequence="They will show “source missing”." />}
      <ErrorLine error={remove.error} />
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="danger" disabled={!contents.data || remove.isPending} onClick={() => remove.mutate()}>
          Delete from disk
        </button>
      </div>
    </Modal>
  );
}

export function groupPaths(nodes: TreeNode[]): string[] {
  return nodes.flatMap((n) => (n.type === "group" ? [n.path, ...groupPaths(n.children)] : []));
}

/** Whether moving `path` into the folder `to` ("" is the top level) makes sense. */
export function canMove(path: string, to: string): boolean {
  const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  return to !== path && !to.startsWith(path + "/") && to !== parent;
}

export async function moveNode(path: string, to: string): Promise<string> {
  return (await api<{ path: string }>("POST", "/move", { json: { path, to } })).path;
}

export function MoveDialog({ path, onClose, onMoved }: { path: string; onClose: () => void; onMoved: () => void }) {
  const queryClient = useQueryClient();
  const tree = useTree();
  const [to, setTo] = useState<string | null>(null);
  const move = useMutation({
    mutationFn: () => moveNode(path, to!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tree"] });
      onMoved();
      onClose();
    },
  });
  const targets = ["", ...groupPaths(tree.data ?? [])].filter((g) => canMove(path, g));
  return (
    <Modal title={`Move “${path.split("/").pop()}”`} onClose={onClose}>
      <div className="muted">Datasets keep working: they refer to sources by id, not by where they are.</div>
      <div className="move-list">
        {targets.length === 0 && <Empty>There is no other place to move it to. Create a folder first.</Empty>}
        {targets.map((g) => (
          <label key={g || "/"} className="check move-target">
            <input type="radio" name="target" checked={to === g} onChange={() => setTo(g)} />
            {g === "" ? "Top level" : g}
          </label>
        ))}
      </div>
      <ErrorLine error={move.error} />
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={to === null || move.isPending} onClick={() => move.mutate()}>
          Move here
        </button>
      </div>
    </Modal>
  );
}
