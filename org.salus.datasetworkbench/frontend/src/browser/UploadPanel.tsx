import { DragEvent, useEffect, useRef, useState } from "react";
import { message } from "../shared/format";
import { api } from "../shared/salus";
import { closeUploadPanel, enqueue, PanelTarget, useUploadPanel } from "./uploadQueue";
import { Dropped, fromDrop, fromFileList, Picked } from "./upload";

/**
 * The docked panel for choosing what to upload into a source (or one of its folders). It only chooses: once the upload
 * starts, the panel closes and the progress bar (UploadProgress) takes over. If something already exists at the destination
 * there is one question for the whole batch: overwrite all or cancel.
 */
export function UploadPanel() {
  const target = useUploadPanel();
  return target ? <Panel key={target.nonce} target={target} /> : null;
}

type Phase = "choose" | "checking" | "conflict";

function Panel({ target }: { target: PanelTarget }) {
  const { source, folder } = target;
  const isTable = source.kind === "table";
  const [phase, setPhase] = useState<Phase>("choose");
  const [picked, setPicked] = useState<Picked[]>([]);
  const [existing, setExisting] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const filesInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);

  const destination = (rel: string) => (folder ? `${folder}/${rel}` : rel);
  const where = `${source.path}${folder ? ` / ${folder}` : ""}`;

  const start = (items: Picked[], overwrite: boolean) => {
    enqueue(source, items.map((p) => ({ file: p.file, path: destination(p.rel) })), overwrite);
    closeUploadPanel();
  };

  const handle = async ({ picked: items, skipped }: Dropped) => {
    setError("");
    setNote(skipped > 0 ? `${skipped} hidden file${skipped === 1 ? "" : "s"} skipped.` : "");
    if (items.length === 0) return;
    if (isTable && (items.length !== 1 || items[0].rel.includes("/") || !/\.csv$/i.test(items[0].rel))) {
      setError("A table source takes a single CSV file.");
      return;
    }
    setPicked(items);
    setPhase("checking");
    try {
      const { existing: found } = await api<{ existing: string[] }>("POST", "/upload/check", {
        json: { source: source.id, paths: items.map((p) => destination(p.rel)) },
      });
      if (found.length === 0) start(items, false);
      else (setExisting(found), setPhase("conflict"));
    } catch (e) {
      setError(message(e));
      setPhase("choose");
    }
  };

  // Files that were dropped on the source in the tree.
  useEffect(() => {
    if (target.initial) void handle(target.initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const drop = async (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (phase !== "choose") return;
    const items = e.dataTransfer.items;
    await handle(items?.length ? await fromDrop(items) : fromFileList(e.dataTransfer.files));
  };

  return (
    <div className="upload-panel" role="region" aria-label="Upload">
      <div className="upload-panel-head">
        <span className="upload-title">
          Upload to <b>{where}</b>
        </span>
        <button className="icon-button" onClick={closeUploadPanel} aria-label="Close" title="Close">
          ✕
        </button>
      </div>

      {phase === "choose" && (
        <div
          className={`dropzone ${dragging ? "dragging" : ""}`}
          onDragOver={(e) => (e.preventDefault(), e.stopPropagation(), setDragging(true))}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => (e.stopPropagation(), void drop(e))}
        >
          <div>{isTable ? "Drop a CSV file here" : "Drop files or folders here"}</div>
          <div className="row dropzone-buttons">
            <button className="small" onClick={() => filesInput.current?.click()}>
              {isTable ? "Choose a CSV file…" : "Choose files…"}
            </button>
            {!isTable && (
              <button className="small" onClick={() => folderInput.current?.click()}>
                Choose a folder…
              </button>
            )}
          </div>
          <input
            ref={filesInput}
            type="file"
            hidden
            multiple={!isTable}
            accept={isTable ? ".csv,text/csv" : undefined}
            onChange={(e) => {
              if (e.target.files) void handle(fromFileList(e.target.files));
              e.target.value = "";
            }}
          />
          <input
            ref={folderInput}
            type="file"
            hidden
            {...({ webkitdirectory: "", directory: "" } as object)}
            onChange={(e) => {
              if (e.target.files) void handle(fromFileList(e.target.files));
              e.target.value = "";
            }}
          />
        </div>
      )}

      {phase === "checking" && <div className="upload-message">Checking what already exists…</div>}

      {phase === "conflict" && (
        <div className="upload-message">
          <div>
            {isTable ? (
              <>This source already has a CSV file ({existing[0]}). Replace it with <b>{picked[0]?.rel}</b>?</>
            ) : (
              <>
                <b>
                  {existing.length} of {picked.length}
                </b>{" "}
                file{picked.length === 1 ? "" : "s"} already exist{existing.length === 1 ? "s" : ""} here:{" "}
                <span className="mono">{existing.slice(0, 4).join(", ")}</span>
                {existing.length > 4 ? ` and ${existing.length - 4} more` : ""}. Overwrite all of them, or cancel the whole upload?
              </>
            )}
          </div>
          <div className="faint">Dataset entries that use an overwritten file will show “source changed”.</div>
          <div className="row">
            <button className="primary small" onClick={() => start(picked, true)}>
              {isTable ? "Replace" : "Overwrite all"}
            </button>
            <button
              className="small"
              onClick={() => {
                setPicked([]);
                setExisting([]);
                setPhase("choose");
              }}
            >
              Cancel upload
            </button>
          </div>
        </div>
      )}

      {note && <div className="faint upload-note">{note}</div>}
      {error && <div className="error-line">{error}</div>}
    </div>
  );
}
