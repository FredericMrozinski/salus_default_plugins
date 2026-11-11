import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyboardEvent, MouseEvent, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useDebounced } from "../shared/hooks";
import { api, encode, initialNav, navigate, onEvent, onNav, setState } from "../shared/salus";
import type { DatasetDetail, EntryPage, EntryRow, EntryState, Nav, Stats } from "../shared/types";
import { Icon } from "../shared/icons";
import { useAutomationApply } from "../shared/automationApply";
import { useMenu } from "../shared/menu";
import { Badge, DeleteDialog, Empty, ErrorLine } from "../shared/ui";

const ROW = 46;
const PAGE = 50;

export function App() {
  const [dataset, setDataset] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);

  useEffect(() => {
    const apply = (nav: Nav) => {
      if (nav.type === "open_dataset") (setDataset(nav.dataset), setSelected(null));
      else if (nav.type === "show_entry") (setDataset(nav.dataset), setSelected(nav.entry));
      else if (nav.type === "edit_schema" || nav.type === "edit_automation" || nav.type === "export_dataset") setDataset(nav.dataset);
    };
    const initial = initialNav();
    if (initial) apply(initial);
    else api<{ dataset: string | null; entry: number | null }>("GET", "/state").then((s) => (setDataset(s.dataset), setSelected(s.entry))).catch(() => undefined);
    const stopNav = onNav(apply);
    // another workbench folder: what was open belongs to the old one
    const stopEvents = onEvent((e) => e.type === "config_changed" && (setDataset(null), setSelected(null)));
    return () => (stopNav(), stopEvents());
  }, []);

  if (!dataset) return <Empty>Choose a dataset in the Workbench to see its entries.</Empty>;
  return <Viewer key={dataset} dataset={dataset} selected={selected} onSelect={setSelected} />;
}

function Viewer({ dataset, selected, onSelect }: { dataset: string; selected: number | null; onSelect: (id: number | null) => void }) {
  const queryClient = useQueryClient();
  const detail = useQuery({ queryKey: ["dataset", dataset], queryFn: () => api<DatasetDetail>("GET", `/datasets/${encode(dataset)}`) });
  const [filter, setFilter] = useState<EntryState | "">("");
  const [search, setSearch] = useState("");
  const q = useDebounced(search, 250);

  // Opening a dataset re-checks its references in the background; the results arrive as events.
  useEffect(() => {
    api("POST", `/datasets/${encode(dataset)}/check`).catch(() => undefined);
  }, [dataset]);

  const open = (entry: number | null) => {
    onSelect(entry);
    setState(dataset, entry).catch(() => undefined);
    if (entry !== null) navigate("entry-editor", { type: "show_entry", dataset, entry }).catch(() => undefined);
  };

  const add = useMutation({
    mutationFn: () => api<{ id: number }>("POST", `/datasets/${encode(dataset)}/entries`),
    onSuccess: (entry) => {
      queryClient.invalidateQueries({ queryKey: ["entries", dataset] });
      setFilter("");
      setSearch("");
      open(entry.id);
    },
  });

  const schema = detail.data?.schema ?? [];
  const automated = Object.keys(detail.data?.automation?.rules ?? {}).length > 0;

  // Several entries can be picked: Ctrl/Cmd+click adds or removes one, Shift+click a range, Ctrl/Cmd+A all that the list
  // shows (with the search and the state filter), Esc none. A plain click picks one and opens it. The right-click menu
  // acts on the picked entries.
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [anchor, setAnchor] = useState<number | null>(null);
  const [deleting, setDeleting] = useState<number[] | null>(null);
  const showMenu = useMenu();
  useEffect(() => setPicked(new Set()), [filter, q]);
  const listedIds = () =>
    queryClient.fetchQuery({
      queryKey: ["entry-ids", dataset, filter, q],
      queryFn: () => api<{ ids: number[] }>("GET", `/datasets/${encode(dataset)}/entry-ids`, { query: { state: filter, q } }).then((r) => r.ids),
      staleTime: 2000,
    });
  const clickRow = async (event: MouseEvent, id: number) => {
    if (event.metaKey || event.ctrlKey) {
      setPicked((all) => {
        const next = new Set(all);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
      setAnchor(id);
    } else if (event.shiftKey && anchor !== null) {
      const ids = await listedIds();
      const [a, b] = [ids.indexOf(anchor), ids.indexOf(id)].sort((x, y) => x - y);
      if (a >= 0) setPicked(new Set(ids.slice(a, b + 1)));
    } else {
      setPicked(new Set([id]));
      setAnchor(id);
      open(id);
    }
  };
  const listKeys = async (event: KeyboardEvent) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "a") {
      event.preventDefault();
      setPicked(new Set(await listedIds()));
    } else if (event.key === "Escape") setPicked(new Set());
  };
  const rowMenu = (event: MouseEvent, id: number) => {
    const ids = picked.has(id) ? [...picked] : [id];
    if (!picked.has(id)) (setPicked(new Set([id])), setAnchor(id));
    showMenu(event, [
      {
        label: ids.length === 1 ? "Delete entry…" : `Delete ${ids.length} entries…`,
        icon: <Icon name="delete" />,
        danger: true,
        onSelect: () => setDeleting(ids),
      },
      ...(automated
        ? (["separator", { label: "Run automation now", icon: <Icon name="bolt" />, onSelect: () => runAutomation.start((mode) => api("POST", `/datasets/${encode(dataset)}/automation/run`, { json: { mode } })) }] as const)
        : []),
    ]);
  };
  const remove = useMutation({
    mutationFn: (ids: number[]) => api<{ deleted: number }>("POST", `/datasets/${encode(dataset)}/entries/delete`, { json: { ids } }),
    onSuccess: (_, ids) => {
      setDeleting(null);
      setPicked(new Set());
      if (selected !== null && ids.includes(selected)) onSelect(null);
      queryClient.invalidateQueries({ queryKey: ["entries", dataset] });
      queryClient.invalidateQueries({ queryKey: ["entry-ids", dataset] });
      queryClient.invalidateQueries({ queryKey: ["stats", dataset] });
    },
  });
  // Run automation now: asks first if existing values would change (shared/automationApply.tsx).
  const runAutomation = useAutomationApply(dataset);

  return (
    <div className="app">
      {/* Header, stacked: the dataset's name, then a 2×2 grid: Automation, Edit schema / Export, + Entry. */}
      <div className="toolbar viewer-head">
        <h2 title={detail.data?.name}>{detail.data?.name ?? dataset}</h2>
        <div className="viewer-head-grid">
          <button
            className={`small ${automated ? "automation-on" : ""}`}
            disabled={!detail.data || schema.length === 0}
            title={schema.length === 0 ? "Define the schema first" : automated ? "Automation is on: entries are created and filled on every refresh" : "Create and fill entries automatically"}
            onClick={() => navigate("entry-editor", { type: "edit_automation", dataset })}
          >
            <Icon name="bolt" size={16} />
            Automation
          </button>
          <button className="small" title="Change the fields of every entry" onClick={() => navigate("entry-editor", { type: "edit_schema", dataset })}>
            <Icon name="pen" size={16} />
            Edit schema
          </button>
        <button className="small" disabled={!detail.data} title="Export the dataset as JSON, optionally with its source files" onClick={() => navigate("entry-editor", { type: "export_dataset", dataset })}>
          <Icon name="export" size={16} />
          Export
        </button>
        <button className="small primary" disabled={add.isPending || schema.length === 0} title={schema.length === 0 ? "Define the schema first" : "Add an entry"} onClick={() => add.mutate()}>
          <Icon name="add" size={22} /> Entry
        </button>
        </div>
      </div>
      <div className="toolbar">
        <input className="grow" placeholder="Search entries…" value={search} onChange={(e) => setSearch(e.target.value)} />
      </div>
      <ErrorLine error={detail.error ?? add.error ?? runAutomation.error} />
      {detail.data && schema.length === 0 && <Empty>This dataset has no fields yet. Use “Edit schema”.</Empty>}
      <EntryList
        dataset={dataset}
        slots={schema.map((s) => String(s.id))}
        filter={filter}
        q={q}
        selected={selected}
        picked={picked}
        onRowClick={clickRow}
        onRowMenu={rowMenu}
        onKeyDown={listKeys}
      />
      <StatsFooter dataset={dataset} filter={filter} onFilter={setFilter} />
      {runAutomation.dialog}
      {deleting && (
        <DeleteDialog
          title={deleting.length === 1 ? `Delete entry #${deleting[0]}` : `Delete ${deleting.length} entries`}
          pending={remove.isPending}
          error={remove.error}
          onConfirm={() => remove.mutate(deleting)}
          onClose={() => (setDeleting(null), remove.reset())}
        >
          <div>
            {deleting.length === 1 ? "The entry with all its values is" : `The ${deleting.length} entries with all their values are`} deleted from the
            dataset. The files and rows they refer to stay untouched. This cannot be undone.
            {automated && " With the automation on, the next refresh (or “Run automation now”) creates entries again for source items that no entry has."}
          </div>
        </DeleteDialog>
      )}
    </div>
  );
}

function EntryList({ dataset, slots, filter, q, selected, picked, onRowClick, onRowMenu, onKeyDown }: {
  dataset: string;
  slots: string[];
  filter: string;
  q: string;
  selected: number | null;
  picked: Set<number>;
  onRowClick: (event: MouseEvent, id: number) => void;
  onRowMenu: (event: MouseEvent, id: number) => void;
  onKeyDown: (event: KeyboardEvent) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(400);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setHeight(el.clientHeight));
    observer.observe(el);
    setHeight(el.clientHeight);
    return () => observer.disconnect();
  }, []);

  // Pages around the visible rows are fetched; page 0 always, it carries the total.
  const [total, setTotal] = useState(0);
  const first = Math.max(0, Math.floor(scrollTop / ROW) - 4);
  const last = Math.min(Math.max(total, 1), Math.ceil((scrollTop + height) / ROW) + 4);
  const pages = new Set<number>([0]);
  for (let i = first; i < last; i += PAGE) pages.add(Math.floor(i / PAGE));
  pages.add(Math.floor((last - 1) / PAGE));
  const pageList = [...pages].filter((p) => p >= 0).sort((a, b) => a - b);
  const results = useQueries({
    queries: pageList.map((page) => ({
      queryKey: ["entries", dataset, filter, q, page],
      queryFn: () => api<EntryPage>("GET", `/datasets/${encode(dataset)}/entries`, { query: { limit: PAGE, offset: page * PAGE, state: filter, q } }),
      placeholderData: (previous: EntryPage | undefined) => previous,
    })),
  });
  const rows = new Map<number, EntryRow>();
  results.forEach((r, i) => r.data?.entries.forEach((e, j) => rows.set(pageList[i] * PAGE + j, e)));
  const knownTotal = results[0]?.data?.total ?? 0;
  useEffect(() => setTotal(knownTotal), [knownTotal]);
  const error = results.find((r) => r.error)?.error;

  const visible = [];
  for (let i = first; i < last && i < knownTotal; i++) visible.push(i);

  return (
    <div className="scroll entry-scroll" ref={box} tabIndex={0} onKeyDown={onKeyDown} onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}>
      <ErrorLine error={error} />
      {knownTotal === 0 && results[0]?.isFetched && <Empty>{q || filter ? "No entry matches." : "No entries yet. Add one with “+ Entry”."}</Empty>}
      <div style={{ height: knownTotal * ROW, position: "relative" }}>
        {visible.map((i) => {
          const row = rows.get(i);
          return (
            <div
              key={i}
              className={`entry-row ${row && (picked.has(row.id) || (picked.size === 0 && row.id === selected)) ? "selected" : ""}`}
              style={{ top: i * ROW, height: ROW }}
              onMouseDown={(e) => e.shiftKey && e.preventDefault() /* no text selection on Shift+click */}
              onClick={(e) => row && onRowClick(e, row.id)}
              onContextMenu={(e) => row && onRowMenu(e, row.id)}
            >
              {row ? <EntryLine row={row} slots={slots} /> : <span className="faint">…</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function EntryLine({ row, slots }: { row: EntryRow; slots: string[] }) {
  const texts = slots.map((s) => row.cells[s]).filter((c) => c && c.text);
  return (
    <>
      <span className="entry-id">#{row.id}</span>
      <span className="entry-summary">
        {texts.length ? texts.slice(0, 3).map((c) => c.text).join(" · ") : <span className="faint">(empty)</span>}
      </span>
      {row.state !== "valid" && <Badge state={row.state} />}
    </>
  );
}

function StatsFooter({ dataset, filter, onFilter }: { dataset: string; filter: string; onFilter: (s: EntryState | "") => void }) {
  const stats = useQuery({ queryKey: ["stats", dataset], queryFn: () => api<Stats>("GET", `/datasets/${encode(dataset)}/stats`) });
  const s = stats.data;
  if (!s) return <div className="footer faint">{stats.error ? "Statistics unavailable" : "…"}</div>;
  const chip = (state: EntryState, label: string) => (
    <button className={`chip chip-${state} ${filter === state ? "on" : ""}`} onClick={() => onFilter(filter === state ? "" : state)} disabled={s.states[state] === 0 && filter !== state}>
      {s.states[state]} {label}
    </button>
  );
  return (
    <div className="footer">
      <div className="stats-line">
        <b>{s.entries}</b> {s.entries === 1 ? "entry" : "entries"}
        {chip("valid", "valid")}
        {chip("incomplete", "incomplete")}
        {chip("corrupted", "corrupted")}
      </div>
    </div>
  );
}
