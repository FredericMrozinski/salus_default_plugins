import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, DragEvent, useContext, useState } from "react";
import { bytes } from "../shared/format";
import { useDebounced, useJob } from "../shared/hooks";
import { api, encode } from "../shared/salus";
import type { FileListing, GroupNode, SearchHit, SourceNode, TreeNode } from "../shared/types";
import { Icon } from "../shared/icons";
import { MenuItem, useMenu } from "../shared/menu";
import { Empty, ErrorLine, flattenSources, useTree } from "../shared/ui";
import { copyText, openPath } from "./actions";
import { Dropped, fromDrop } from "./upload";
import { openUploadPanel } from "./uploadQueue";
import { canMove, DeleteGroupDialog, DeleteItemDialog, IssuesDialog, MoveDialog, moveNode, NewGroupDialog, NewSourceDialog, RemoveSourceDialog, RenameDialog, TableSetupDialog } from "./dialogs";

type Dialog =
  | { type: "group"; parent: string }
  | { type: "source"; group: string; kind: "file" | "table" }
  | { type: "setup"; source: SourceNode }
  | { type: "issues"; source: SourceNode }
  | { type: "upload"; source: SourceNode; folder?: string; initial?: Dropped }
  | { type: "remove"; source: SourceNode }
  | { type: "delete-group"; path: string }
  | { type: "move"; path: string }
  | { type: "rename-group"; path: string }
  | { type: "rename-source"; source: SourceNode }
  | { type: "rename-item"; source: SourceNode; path: string }
  | { type: "delete-item"; source: SourceNode; path: string; folder: boolean };

/** The entries for creating something inside the folder `group` ("" is the top level); the same three everywhere. */
function newItems(group: string, open: (d: Dialog) => void, labels: { folder: string; file: string; table: string }): MenuItem[] {
  return [
    { label: labels.folder, icon: <Icon name="folder" />, onSelect: () => open({ type: "group", parent: group }) },
    { label: labels.file, icon: <Icon name="database" />, onSelect: () => open({ type: "source", group, kind: "file" }) },
    { label: labels.table, icon: <Icon name="table" />, onSelect: () => open({ type: "source", group, kind: "table" }) },
  ];
}

/** Drag and drop of sources and folders between folders. */
type Drag = { dragging: string | null; setDragging: (path: string | null) => void; drop: (to: string) => void };
const DragContext = createContext<Drag>({ dragging: null, setDragging: () => undefined, drop: () => undefined });

/** Props that make a tree row draggable. */
function useDragSource(path: string) {
  const { setDragging } = useContext(DragContext);
  return {
    draggable: true,
    onDragStart: (e: DragEvent) => {
      e.dataTransfer.setData("text/plain", path);
      e.dataTransfer.effectAllowed = "move";
      e.stopPropagation();
      setTimeout(() => setDragging(path), 0); // changing the page inside dragstart can cancel the drag
    },
    onDragEnd: () => setDragging(null),
  };
}

const isFileDrag = (e: DragEvent) => Array.from(e.dataTransfer.types).includes("Files");

/**
 * Makes a source row or a folder row accept files dragged in from outside (the Finder, a desktop folder). Only compatible
 * targets accept: file sources take files and folders, a table source takes exactly one CSV file. The browser reveals
 * the names of dragged files only when they are dropped, so for a table source the type is checked while dragging and
 * the name when dropped.
 */
function useFileDrop(source: SourceNode, folder: string, onDialog: (d: Dialog) => void) {
  const [over, setOver] = useState(false);
  const isTable = source.kind === "table";
  const accepts = (e: DragEvent) => {
    if (!isFileDrag(e) || source.state === "broken") return false;
    if (!isTable) return true;
    const items = e.dataTransfer.items;
    if (items && items.length > 1) return false;
    const type = items?.[0]?.type ?? "";
    return type === "" || /csv|excel|comma/i.test(type);
  };
  return {
    over,
    props: {
      onDragOver: (e: DragEvent) => {
        if (!accepts(e)) return;
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = "copy";
        setOver(true);
      },
      onDragLeave: () => setOver(false),
      onDrop: async (e: DragEvent) => {
        setOver(false);
        if (!accepts(e)) return;
        e.preventDefault();
        e.stopPropagation();
        const dropped = await fromDrop(e.dataTransfer.items);
        if (dropped.picked.length === 0) return;
        if (isTable && (dropped.picked.length !== 1 || dropped.picked[0].rel.includes("/") || !/\.csv$/i.test(dropped.picked[0].rel))) {
          window.alert("A table source takes a single CSV file.");
          return;
        }
        onDialog({ type: "upload", source, folder, initial: dropped }); // the upload panel checks for conflicts and asks once
      },
    },
  };
}

/** Props that make an element a drop target for moves into the group `to` ("" is the top level). */
function useDropTarget(to: string, onOver: (over: boolean) => void) {
  const { dragging, drop } = useContext(DragContext);
  const valid = dragging !== null && canMove(dragging, to);
  return {
    valid,
    props: {
      onDragOver: (e: DragEvent) => {
        if (!valid) return;
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = "move";
        onOver(true);
      },
      onDragLeave: () => onOver(false),
      onDrop: (e: DragEvent) => {
        onOver(false);
        if (!valid) return;
        e.preventDefault();
        e.stopPropagation();
        drop(to);
      },
    },
  };
}

export function SourcesTab() {
  const tree = useTree();
  const [selectedGroup, setSelectedGroup] = useState("");
  const [dialog, setDialog] = useState<Dialog | null>(null);
  // Uploads are not a dialog: they open the docked upload panel at the bottom.
  const openDialog = (d: Dialog) => (d.type === "upload" ? openUploadPanel({ source: d.source, folder: d.folder, initial: d.initial }) : setDialog(d));
  const queryClient = useQueryClient();
  const showMenu = useMenu();
  const [dragging, setDragging] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const searching = search.trim() !== "";
  const move = useMutation({
    mutationFn: ({ path, to }: { path: string; to: string }) => moveNode(path, to),
    onSuccess: () => {
      setSelectedGroup("");
      queryClient.invalidateQueries({ queryKey: ["tree"] });
    },
    onError: (error) => window.alert(error instanceof Error ? error.message : String(error)),
  });
  const drag: Drag = { dragging, setDragging, drop: (to) => dragging && (setDragging(null), move.mutate({ path: dragging, to })) };

  return (
    <DragContext.Provider value={drag}>
      <div className="toolbar">
        <button
          className="small"
          title={`Create a folder or a source cohort in ${selectedGroup || "the top level"}`}
          aria-haspopup="menu"
          onClick={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            showMenu(e, newItems(selectedGroup, setDialog, { folder: "Folder", file: "General Source Cohort", table: "Tabular Source Cohort" }), { x: rect.left, y: rect.bottom + 2 });
          }}
        >
          <Icon name="add" size={22} /> New
        </button>
        <span className="grow faint" title="Where new folders and source cohorts go. Click a folder to choose it.">
          {selectedGroup ? `in ${selectedGroup}` : ""}
        </span>
        <button className="icon-button" title="Refresh" onClick={() => queryClient.invalidateQueries({ queryKey: ["tree"] })}>
          <Icon name="refresh" size={22} />
        </button>
      </div>
      <div className="search-row">
        <input
          type="search"
          placeholder="Search all files…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => e.key === "Escape" && setSearch("")}
          aria-label="Search all files"
        />
      </div>
      {searching && <SearchResults query={search} sources={flattenSources(tree.data ?? [])} onDialog={openDialog} />}
      <TopLevelArea
        hidden={searching}
        onContextMenu={(e) =>
          showMenu(e, [
            ...newItems("", setDialog, { folder: "New folder", file: "New general source cohort", table: "New tabular source cohort" }),
            "separator",
            { label: "Refresh", icon: <Icon name="refresh" />, onSelect: () => queryClient.invalidateQueries({ queryKey: ["tree"] }) },
          ])
        }
      >
        <ErrorLine error={tree.error} />
        {tree.isLoading && <Empty>Loading…</Empty>}
        {tree.data?.length === 0 && <Empty>No source cohorts yet. Create a folder or a source cohort to begin.</Empty>}
        <ul className="tree">
          {tree.data?.map((node) => (
            <Node key={node.path} node={node} depth={0} selected={selectedGroup} onSelect={setSelectedGroup} onDialog={openDialog} />
          ))}
        </ul>
      </TopLevelArea>
      {dialog?.type === "group" && <NewGroupDialog parent={dialog.parent} onClose={() => (setDialog(null), queryClient.invalidateQueries({ queryKey: ["tree"] }))} />}
      {dialog?.type === "source" && (
        <NewSourceDialog
          group={dialog.group}
          kind={dialog.kind}
          onClose={() => setDialog(null)}
          onCreated={(source) => {
            queryClient.invalidateQueries({ queryKey: ["tree"] });
            if (source.kind === "table") openUploadPanel({ source });
          }}
        />
      )}
      {dialog?.type === "move" && <MoveDialog path={dialog.path} onClose={() => setDialog(null)} onMoved={() => setSelectedGroup("")} />}
      {dialog?.type === "rename-group" && (
        <RenameDialog
          title="Rename folder"
          current={dialog.path.split("/").pop()!}
          hint="Sources inside keep working: datasets refer to them by id, not by name."
          run={(name) => api("POST", "/groups/rename", { json: { path: dialog.path, name } }).then(() => setSelectedGroup(""))}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog?.type === "rename-source" && (
        <RenameDialog
          title="Rename source"
          current={dialog.source.name}
          hint="The folder on disk is renamed, too. Datasets keep working: they refer to the source by id."
          run={(name) => api("POST", `/sources/${encode(dialog.source.id!)}/rename`, { json: { name } })}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog?.type === "rename-item" && (
        <RenameDialog
          title="Rename"
          current={dialog.path.split("/").pop()!}
          hint="Renamed on disk. Dataset entries follow automatically. If you change the file type, fields that only allow certain types mark the entry invalid."
          run={(name) => api("POST", `/sources/${encode(dialog.source.id!)}/items/rename`, { json: { path: dialog.path, name } })}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog?.type === "delete-item" && <DeleteItemDialog source={dialog.source} path={dialog.path} folder={dialog.folder} onClose={() => setDialog(null)} />}
      {dialog?.type === "remove" && <RemoveSourceDialog source={dialog.source} onClose={() => setDialog(null)} />}
      {dialog?.type === "delete-group" && <DeleteGroupDialog path={dialog.path} onClose={() => setDialog(null)} />}
      {dialog?.type === "setup" && <TableSetupDialog source={dialog.source} onClose={() => setDialog(null)} />}
      {dialog?.type === "issues" && <IssuesDialog source={dialog.source} onClose={() => setDialog(null)} />}
    </DragContext.Provider>
  );
}

/** The result of the global search: matching files of all sources in one flat list. */
function SearchResults({ query, sources, onDialog }: { query: string; sources: SourceNode[]; onDialog: (d: Dialog) => void }) {
  const q = useDebounced(query.trim(), 250);
  const [limit, setLimit] = useState(200);
  const showMenu = useMenu();
  const results = useQuery({
    queryKey: ["search", q, limit],
    queryFn: () => api<{ results: SearchHit[]; more: boolean }>("GET", "/search", { query: { q, limit } }),
    enabled: q !== "",
    placeholderData: (previous) => previous,
  });
  const data = results.data;
  return (
    <div className="scroll">
      <ErrorLine error={results.error} />
      {(results.isLoading || q !== query.trim()) && !data && <Empty>Searching…</Empty>}
      {data && data.results.length === 0 && q === query.trim() && <Empty>No file matches “{q}”.</Empty>}
      {data && data.results.length > 0 && <div className="tree-hint">{data.results.length}{data.more ? "+" : ""} result{data.results.length === 1 ? "" : "s"} in all sources</div>}
      <ul className="tree">
        {data?.results.map((hit) => {
          const source = sources.find((s) => s.id === hit.source);
          const folder = hit.path.includes("/") ? hit.path.slice(0, hit.path.lastIndexOf("/")) : "";
          return (
            <li key={`${hit.source}/${hit.id}`}>
              <div
                className="tree-row file result"
                title={hit.abs_path}
                onClick={() => openPath(hit.abs_path)}
                onContextMenu={(e) =>
                  showMenu(e, [
                    { label: "Open", icon: <Icon name="open" />, onSelect: () => openPath(hit.abs_path) },
                    { label: "Copy path", icon: <Icon name="copy" />, onSelect: () => void copyText(hit.abs_path) },
                    "separator",
                    { label: "Rename…", icon: <Icon name="edit" />, disabled: !source, onSelect: () => source && onDialog({ type: "rename-item", source, path: hit.path }) },
                    { label: "Delete from disk…", icon: <Icon name="delete" />, danger: true, disabled: !source, onSelect: () => source && onDialog({ type: "delete-item", source, path: hit.path, folder: false }) },
                  ])
                }
              >
                <span className="tree-icon">▫</span>
                <span className="result-text">
                  <span className="tree-name">{hit.name}</span>
                  <span className="result-path">{hit.source_path}{folder ? ` / ${folder}` : ""}</span>
                </span>
                <span className="tree-badges faint">{bytes(hit.size)}</span>
              </div>
            </li>
          );
        })}
      </ul>
      {data?.more && (
        <div className="tree-hint">
          <button className="small" onClick={() => setLimit(limit + 200)}>
            Show more
          </button>
        </div>
      )}
    </div>
  );
}

/** The scrolling tree. Dropping on its background moves into the top level. */
function TopLevelArea({ children, onContextMenu, hidden }: { children: React.ReactNode; onContextMenu: (e: React.MouseEvent) => void; hidden?: boolean }) {
  const { dragging } = useContext(DragContext);
  const [over, setOver] = useState(false);
  const target = useDropTarget("", setOver);
  const [overHint, setOverHint] = useState(false);
  const hint = useDropTarget("", setOverHint);
  return (
    <div className="scroll-wrap" hidden={hidden}>
      <div className={`scroll ${over ? "drop-over" : ""} ${dragging ? "dragging" : ""}`} {...target.props} onContextMenu={onContextMenu}>
        {children}
      </div>
      {/* Always reachable, even when the tree fills the panel and there is no empty background. */}
      {hint.valid && (
        <div className={`drop-hint ${overHint ? "over" : ""}`} {...hint.props}>
          Drop here to move it to the top level
        </div>
      )}
    </div>
  );
}

type NodeProps = {
  node: TreeNode;
  depth: number;
  selected: string;
  onSelect: (path: string) => void;
  onDialog: (dialog: Dialog) => void;
};

function Node(props: NodeProps) {
  return props.node.type === "group" ? <Group {...props} node={props.node} /> : <Source {...props} node={props.node} />;
}

function Group({ node, depth, selected, onSelect, onDialog }: NodeProps & { node: GroupNode }) {
  const [open, setOpen] = useState(true);
  const showMenu = useMenu();
  const dragSource = useDragSource(node.path);
  const [over, setOver] = useState(false);
  const target = useDropTarget(node.path, setOver);
  return (
    <li>
      <div
        {...dragSource}
        {...target.props}
        className={`tree-row ${selected === node.path ? "selected" : ""} ${over ? "drop-target" : ""}`}
        style={{ paddingLeft: 6 + depth * 14 }}
        onClick={() => (onSelect(selected === node.path ? "" : node.path), setOpen(true))}
        onContextMenu={(e) =>
          showMenu(e, [
            ...newItems(node.path, onDialog, { folder: "New folder here", file: "New general source cohort here", table: "New tabular source cohort here" }),
            "separator",
            { label: "Move to…", onSelect: () => onDialog({ type: "move", path: node.path }) },
            { label: "Rename…", icon: <Icon name="edit" />, onSelect: () => onDialog({ type: "rename-group", path: node.path }) },
            { label: "Delete from disk…", icon: <Icon name="delete" />, danger: true, onSelect: () => onDialog({ type: "delete-group", path: node.path }) },
          ])
        }
      >
        <span className="twisty" onClick={(e) => (e.stopPropagation(), setOpen(!open))}>
          {open ? "▾" : "▸"}
        </span>
        <span className="tree-icon">
          <Icon name="folder" />
        </span>
        <span className="tree-name">{node.name}</span>
      </div>
      {open && (
        <ul className="tree">
          {node.children.length === 0 && <li className="tree-hint" style={{ paddingLeft: 24 + depth * 14 }}>empty</li>}
          {node.children.map((child) => (
            <Node key={child.path} node={child} depth={depth + 1} selected={selected} onSelect={onSelect} onDialog={onDialog} />
          ))}
        </ul>
      )}
    </li>
  );
}

function Source({ node, depth, onDialog }: NodeProps & { node: SourceNode }) {
  const [open, setOpen] = useState(false);
  const job = useJob(node.id);
  const scan = useMutation({
    mutationFn: () => api("POST", `/sources/${encode(node.id!)}/scan`),
    onError: () => undefined,
  });
  const isFile = node.kind === "file";
  const broken = node.state === "broken";
  const showMenu = useMenu();
  const dragSource = useDragSource(node.path);
  const fileDrop = useFileDrop(node, "", onDialog);
  const [dropOver, setDropOver] = useState(false);
  // Dropping on a source means "into the folder this source is in".
  const sibling = useDropTarget(node.path.includes("/") ? node.path.slice(0, node.path.lastIndexOf("/")) : "", setDropOver);
  const busy = !!job && !job.done;
  const menu = (e: React.MouseEvent) =>
    showMenu(e, [
      ...(isFile || node.state === "ready" ? [{ label: isFile ? "Rescan files" : "Read the CSV again", icon: <Icon name="refresh" />, disabled: busy, onSelect: () => scan.mutate() }] : []),
      ...(node.state === "ambiguous"
        ? [] // several CSV files: remove the extra ones first (they are listed under the source)
        : [{ label: isFile ? "Upload files…" : node.csv ? "Replace the CSV file…" : "Upload the CSV file…", icon: <Icon name="upload" />, onSelect: () => onDialog({ type: "upload", source: node }) }]),
      ...(!isFile && node.state !== "no_csv" && node.state !== "ambiguous" ? [{ label: "Columns and key…", onSelect: () => onDialog({ type: "setup", source: node }) }] : []),
      ...(!isFile && (node.issues ?? 0) > 0 ? [{ label: "Show rows with problems…", onSelect: () => onDialog({ type: "issues", source: node }) }] : []),
      "separator",
      { label: "Move to…", onSelect: () => onDialog({ type: "move", path: node.path }) },
      { label: "Rename…", icon: <Icon name="edit" />, onSelect: () => onDialog({ type: "rename-source", source: node }) },
      { label: "Remove…", icon: <Icon name="delete" />, danger: true, onSelect: () => onDialog({ type: "remove", source: node }) },
    ]);

  return (
    <li>
      <div
        className={`tree-row ${dropOver ? "drop-sibling" : ""} ${fileDrop.over ? "drop-target" : ""}`}
        {...dragSource}
        {...sibling.props}
        onDragOverCapture={fileDrop.props.onDragOver}
        onDragLeave={() => (fileDrop.props.onDragLeave(), sibling.props.onDragLeave())}
        onDropCapture={fileDrop.props.onDrop}
        style={{ paddingLeft: 6 + depth * 14 }}
        onClick={() => !broken && setOpen(!open)}
        onContextMenu={broken ? (e) => showMenu(e, [{ label: "Remove…", icon: <Icon name="delete" />, danger: true, onSelect: () => onDialog({ type: "remove", source: node }) }]) : menu}
      >
        <span className="twisty">{open ? "▾" : "▸"}</span>
        <span className="tree-icon" title={isFile ? "General source cohort" : "Tabular source cohort"}>
          <Icon name={isFile ? "database" : "table"} />
        </span>
        <span className="tree-name">{node.name}</span>
        <span className="tree-badges">
          {broken && <span className="badge badge-bad" title={node.error ?? ""}>broken</span>}
          {!broken && isFile && <span className="faint">{node.items ?? 0}</span>}
          {!broken && !isFile && node.state === "ready" && <span className="faint">{node.rows ?? 0} rows</span>}
          {(node.missing ?? 0) > 0 && <span className="badge badge-bad">{node.missing} missing</span>}
          {(node.issues ?? 0) > 0 && (
            <span className="badge badge-warn" onClick={(e) => (e.stopPropagation(), onDialog({ type: "issues", source: node }))} title="Rows that cannot be used">
              {node.issues} issues
            </span>
          )}
          {node.state === "needs_setup" && <span className="badge badge-warn">set up</span>}
          {node.state === "no_csv" && <span className="badge badge-warn">no CSV</span>}
          {node.state === "ambiguous" && <span className="badge badge-warn" title={node.error ?? ""}>{node.csv_files?.length} CSV files</span>}
        </span>
      </div>
      {job && job.text && <div className={`tree-hint ${job.error ? "bad" : ""}`} style={{ paddingLeft: 30 + depth * 14 }}>{job.text}</div>}
      {scan.error ? <ErrorLine error={scan.error} /> : null}
      {open && !broken && (isFile ? <FileFolder source={node} dir="" depth={depth + 1} onDialog={onDialog} /> : <TableDetails source={node} depth={depth + 1} onDialog={onDialog} />)}
    </li>
  );
}

function TableDetails({ source, depth, onDialog }: { source: SourceNode; depth: number; onDialog: (d: Dialog) => void }) {
  const showMenu = useMenu();
  const pad = { paddingLeft: 24 + depth * 14 };
  if (source.state === "no_csv")
    return (
      <div className="tree-hint" style={pad}>
        <button className="small" onClick={() => onDialog({ type: "upload", source })}>
          Upload the CSV file
        </button>
      </div>
    );
  const csvRow = (name: string, path: string) => (
    <li key={name}>
      <div
        className="tree-row file"
        style={pad}
        title={path}
        onClick={() => openPath(path)}
        onContextMenu={(e) =>
          showMenu(e, [
            { label: "Open", icon: <Icon name="open" />, onSelect: () => openPath(path) },
            { label: "Copy path", icon: <Icon name="copy" />, onSelect: () => void copyText(path) },
            "separator",
            { label: "Rename…", icon: <Icon name="edit" />, onSelect: () => onDialog({ type: "rename-item", source, path: name }) },
            { label: "Delete from disk…", icon: <Icon name="delete" />, danger: true, onSelect: () => onDialog({ type: "delete-item", source, path: name, folder: false }) },
          ])
        }
      >
        <span className="tree-icon">▫</span>
        <span className="tree-name">{name}</span>
      </div>
    </li>
  );
  return (
    <ul className="tree">
      {source.state === "ambiguous" ? (
        <>
          {source.csv_files?.map((f) => csvRow(f.name, f.path))}
          <li className="tree-hint warn" style={pad}>
            A tabular source cohort is exactly one CSV file, and this folder has {source.csv_files?.length}. Delete or rename the ones that do not belong here.
          </li>
        </>
      ) : (
        csvRow(source.csv ?? "", source.csv_path ?? "")
      )}
      {source.state === "needs_setup" && (
        <li className="tree-hint" style={pad}>
          <button className="small primary" onClick={() => onDialog({ type: "setup", source })}>
            Set up columns
          </button>
        </li>
      )}
      {source.state === "ready" && (
        <li className="tree-hint" style={pad}>
          {source.columns?.map((c) => (
            <span key={c.name} className={`col-chip ${c.is_key ? "key" : ""}`} title={c.is_key ? "key" : c.type}>
              {c.name}
            </span>
          ))}
        </li>
      )}
    </ul>
  );
}

function FileFolder({ source, dir, depth, onDialog }: { source: SourceNode; dir: string; depth: number; onDialog: (d: Dialog) => void }) {
  const showMenu = useMenu();
  const [limit, setLimit] = useState(100);
  const listing = useQuery({
    queryKey: ["files", source.id, dir, limit],
    queryFn: () => api<FileListing>("GET", `/sources/${encode(source.id!)}/files`, { query: { dir, limit } }),
    placeholderData: (previous) => previous,
  });
  const pad = 24 + depth * 14;
  return (
    <ul className="tree">
      <ErrorLine error={listing.error} />
      {listing.data?.folders.map((folder) => (
        <Folder key={folder.path} source={source} folder={folder} depth={depth} onDialog={onDialog} />
      ))}
      {listing.data?.files.map((file) => (
        <li key={file.id}>
          <div
            className={`tree-row file ${file.status === "missing" ? "is-missing" : ""}`}
            style={{ paddingLeft: pad }}
            title={file.abs_path}
            onClick={() => file.status !== "missing" && openPath(file.abs_path)}
            onContextMenu={(e) =>
              showMenu(e, [
                { label: "Open", icon: <Icon name="open" />, disabled: file.status === "missing", onSelect: () => openPath(file.abs_path) },
                { label: "Copy path", icon: <Icon name="copy" />, onSelect: () => void copyText(file.abs_path) },
                "separator",
                { label: "Rename…", icon: <Icon name="edit" />, disabled: file.status === "missing", onSelect: () => onDialog({ type: "rename-item", source, path: file.path }) },
                { label: "Delete from disk…", icon: <Icon name="delete" />, danger: true, disabled: file.status === "missing", onSelect: () => onDialog({ type: "delete-item", source, path: file.path, folder: false }) },
                "separator",
                { label: "Upload files here…", icon: <Icon name="upload" />, onSelect: () => onDialog({ type: "upload", source, folder: file.path.includes("/") ? file.path.slice(0, file.path.lastIndexOf("/")) : "" }) },
              ])
            }
          >
            <span className="tree-icon">▫</span>
            <span className="tree-name">{file.name}</span>
            <span className="tree-badges">
              {file.status === "missing" ? <span className="badge badge-bad">missing</span> : <span className="faint">{bytes(file.size)}</span>}
            </span>
          </div>
        </li>
      ))}
      {listing.data?.more && (
        <li className="tree-hint" style={{ paddingLeft: pad }}>
          <button className="small" onClick={() => setLimit(limit + 200)}>
            Show more
          </button>
        </li>
      )}
      {listing.data && listing.data.files.length === 0 && listing.data.folders.length === 0 && (
        <li className="tree-hint" style={{ paddingLeft: pad }}>
          {dir === "" ? (
            <>
              No files.{" "}
              <button className="small" onClick={() => onDialog({ type: "upload", source })}>
                Upload
              </button>
            </>
          ) : (
            "empty"
          )}
        </li>
      )}
    </ul>
  );
}

function Folder({ source, folder, depth, onDialog }: { source: SourceNode; folder: { name: string; path: string; count: number; missing: number }; depth: number; onDialog: (d: Dialog) => void }) {
  const [open, setOpen] = useState(false);
  const showMenu = useMenu();
  const fileDrop = useFileDrop(source, folder.path, onDialog);
  return (
    <li>
      <div
        className={`tree-row ${fileDrop.over ? "drop-target" : ""}`}
        {...fileDrop.props}
        style={{ paddingLeft: 24 + depth * 14 }}
        onClick={() => setOpen(!open)}
        onContextMenu={(e) =>
          showMenu(e, [
            { label: open ? "Collapse" : "Expand", onSelect: () => setOpen(!open) },
            { label: "Upload into this folder…", icon: <Icon name="upload" />, onSelect: () => onDialog({ type: "upload", source, folder: folder.path }) },
            "separator",
            { label: "Rename…", icon: <Icon name="edit" />, onSelect: () => onDialog({ type: "rename-item", source, path: folder.path }) },
            { label: "Delete from disk…", icon: <Icon name="delete" />, danger: true, onSelect: () => onDialog({ type: "delete-item", source, path: folder.path, folder: true }) },
          ])
        }
      >
        <span className="twisty">{open ? "▾" : "▸"}</span>
        <span className="tree-icon">
          <Icon name="folder" />
        </span>
        <span className="tree-name">{folder.name}</span>
        <span className="tree-badges">
          {folder.missing > 0 && <span className="badge badge-bad">{folder.missing} missing</span>}
          <span className="faint">{folder.count}</span>
        </span>
      </div>
      {open && <FileFolder source={source} dir={folder.path} depth={depth + 1} onDialog={onDialog} />}
    </li>
  );
}
