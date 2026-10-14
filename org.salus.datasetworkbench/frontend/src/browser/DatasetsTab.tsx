import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, encode, navigate, setState } from "../shared/salus";
import type { DatasetDetail, DatasetSummary } from "../shared/types";
import { Icon } from "../shared/icons";
import { useMenu } from "../shared/menu";
import { Badge, Empty, ErrorLine, Field, Modal } from "../shared/ui";
import { RenameDialog } from "./dialogs";

export function DatasetsTab() {
  const queryClient = useQueryClient();
  const datasets = useQuery({ queryKey: ["datasets"], queryFn: () => api<DatasetSummary[]>("GET", "/datasets") });
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<DatasetSummary | null>(null);
  const [renaming, setRenaming] = useState<DatasetSummary | null>(null);
  const showMenu = useMenu();

  const open = async (id: string, editSchema = false) => {
    setSelected(id);
    await setState(id, null);
    // The viewer lists the entries, the editor shows one entry (or the schema of a new dataset).
    await navigate("dataset-viewer", { type: "open_dataset", dataset: id });
    await navigate("entry-editor", editSchema ? { type: "edit_schema", dataset: id } : { type: "open_dataset", dataset: id });
  };

  const remove = useMutation({
    mutationFn: (id: string) => api("DELETE", `/datasets/${encode(id)}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["datasets"] });
      setDeleting(null);
    },
  });

  return (
    <>
      <div className="toolbar">
        <button className="small" onClick={() => setCreating(true)}>
          <Icon name="add" size={22} /> Dataset
        </button>
        <span className="grow" />
        <button className="icon-button" title="Refresh" onClick={() => queryClient.invalidateQueries({ queryKey: ["datasets"] })}>
          <Icon name="refresh" size={22} />
        </button>
      </div>
      <div className="scroll" onContextMenu={(e) => showMenu(e, [{ label: "New dataset…", icon: <Icon name="add" />, onSelect: () => setCreating(true) }, { label: "Refresh", icon: <Icon name="refresh" />, onSelect: () => queryClient.invalidateQueries({ queryKey: ["datasets"] }) }])}>
        <ErrorLine error={datasets.error} />
        {datasets.isLoading && <Empty>Loading…</Empty>}
        {datasets.data?.length === 0 && <Empty>No datasets yet.</Empty>}
        <ul className="tree">
          {datasets.data?.map((d) => (
            <li key={d.id}>
              <div className={`tree-row dataset ${selected === d.id ? "selected" : ""}`} onClick={() => open(d.id)}
                onContextMenu={(e) =>
                  showMenu(e, [
                    { label: "Open", icon: <Icon name="open" />, onSelect: () => open(d.id) },
                    { label: "Edit schema", onSelect: () => open(d.id, true) },
                    { label: "Rename…", icon: <Icon name="edit" />, onSelect: () => setRenaming(d) },
                    "separator",
                    { label: "Delete…", icon: <Icon name="delete" />, danger: true, onSelect: () => setDeleting(d) },
                  ])
                }
              >
                <span className="tree-name">{d.name}</span>
                <span className="tree-badges">
                  {d.error ? (
                    <span className="badge badge-bad" title={d.error}>
                      broken
                    </span>
                  ) : (
                    <>
                      <span className="faint">{d.entries}</span>
                      {d.states.corrupted > 0 && <Badge state="corrupted">{d.states.corrupted} corrupted</Badge>}
                      {d.states.incomplete > 0 && <Badge state="incomplete">{d.states.incomplete} incomplete</Badge>}
                    </>
                  )}
                </span>
              </div>
            </li>
          ))}
        </ul>
      </div>
      {renaming && (
        <RenameDialog
          title="Rename dataset"
          current={renaming.name}
          run={(name) => api("PATCH", `/datasets/${encode(renaming.id)}`, { json: { name } })}
          onClose={() => setRenaming(null)}
        />
      )}
      {deleting && (
        <Modal title={`Delete dataset “${deleting.name}”`} onClose={() => setDeleting(null)}>
          <div>
            The dataset with its {deleting.entries} entr{deleting.entries === 1 ? "y" : "ies"} is deleted. The sources stay untouched. This cannot be undone.
          </div>
          <ErrorLine error={remove.error} />
          <div className="modal-actions">
            <button onClick={() => setDeleting(null)}>Cancel</button>
            <button className="danger" disabled={remove.isPending} onClick={() => remove.mutate(deleting.id)}>
              Delete
            </button>
          </div>
        </Modal>
      )}
      {creating && (
        <NewDataset
          onClose={() => setCreating(false)}
          onCreated={(dataset) => {
            queryClient.invalidateQueries({ queryKey: ["datasets"] });
            open(dataset.id, true);
          }}
        />
      )}
    </>
  );
}

function NewDataset({ onClose, onCreated }: { onClose: () => void; onCreated: (dataset: DatasetDetail) => void }) {
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => api<DatasetDetail>("POST", "/datasets", { json: { name } }),
    onSuccess: (dataset) => {
      onCreated(dataset);
      onClose();
    },
  });
  return (
    <Modal title="New dataset" onClose={onClose}>
      <Field label="Name" hint="You define the schema (the fields of every entry) next.">
        <input autoFocus value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && name.trim() && create.mutate()} />
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
