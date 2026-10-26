import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useDebounced } from "../shared/hooks";
import { api, encode, initialNav, navigate, onEvent, onNav, setState } from "../shared/salus";
import type { DatasetDetail, EntryPage, EntryRow, EntryState, Nav, Stats } from "../shared/types";
import { Badge, Empty, ErrorLine } from "../shared/ui";

const ROW = 46;
const PAGE = 50;

export function App() {
  const [dataset, setDataset] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);

  useEffect(() => {
    const apply = (nav: Nav) => {
      if (nav.type === "open_dataset") (setDataset(nav.dataset), setSelected(null));
      else if (nav.type === "show_entry") (setDataset(nav.dataset), setSelected(nav.entry));
      else if (nav.type === "edit_schema") setDataset(nav.dataset);
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
  return (
    <div className="app">
      <div className="toolbar">
        <h2 title={detail.data?.name}>{detail.data?.name ?? dataset}</h2>
        <button className="small" title="Change the fields of every entry" onClick={() => navigate("entry-editor", { type: "edit_schema", dataset })}>
          Edit schema
        </button>
        <button className="small primary" disabled={add.isPending || schema.length === 0} title={schema.length === 0 ? "Define the schema first" : "Add an entry"} onClick={() => add.mutate()}>
          + Entry
        </button>
      </div>
      <div className="toolbar">
        <input className="grow" placeholder="Search entries…" value={search} onChange={(e) => setSearch(e.target.value)} />
        <select value={filter} onChange={(e) => setFilter(e.target.value as EntryState | "")}>
          <option value="">All</option>
          <option value="valid">Valid</option>
          <option value="incomplete">Incomplete</option>
          <option value="corrupted">Corrupted</option>
        </select>
      </div>
      <ErrorLine error={detail.error ?? add.error} />
      {detail.data && schema.length === 0 && <Empty>This dataset has no fields yet. Use “Edit schema”.</Empty>}
      <EntryList dataset={dataset} slots={schema.map((s) => String(s.id))} filter={filter} q={q} selected={selected} onSelect={open} />
      <StatsFooter dataset={dataset} filter={filter} onFilter={setFilter} />
    </div>
  );
}

function EntryList({ dataset, slots, filter, q, selected, onSelect }: { dataset: string; slots: string[]; filter: string; q: string; selected: number | null; onSelect: (id: number) => void }) {
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
    <div className="scroll entry-scroll" ref={box} onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}>
      <ErrorLine error={error} />
      {knownTotal === 0 && results[0]?.isFetched && <Empty>{q || filter ? "No entry matches." : "No entries yet. Add one with “+ Entry”."}</Empty>}
      <div style={{ height: knownTotal * ROW, position: "relative" }}>
        {visible.map((i) => {
          const row = rows.get(i);
          return (
            <div key={i} className={`entry-row ${row && row.id === selected ? "selected" : ""}`} style={{ top: i * ROW, height: ROW }} onClick={() => row && onSelect(row.id)}>
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
  const [open, setOpen] = useState(false);
  const s = stats.data;
  if (!s) return <div className="footer faint">{stats.error ? "Statistics unavailable" : "…"}</div>;
  const chip = (state: EntryState, label: string) => (
    <button className={`chip chip-${state} ${filter === state ? "on" : ""}`} onClick={() => onFilter(filter === state ? "" : state)} disabled={s.states[state] === 0 && filter !== state}>
      {s.states[state]} {label}
    </button>
  );
  const problems = s.items.source_missing + s.items.source_changed + s.items.invalid;
  return (
    <div className="footer">
      <div className="stats-line">
        <b>{s.entries}</b> {s.entries === 1 ? "entry" : "entries"}
        {chip("valid", "valid")}
        {chip("incomplete", "incomplete")}
        {chip("corrupted", "corrupted")}
        <span className="grow" />
        <button className="small" onClick={() => setOpen(!open)}>
          {open ? "Hide details" : "Details"}
        </button>
      </div>
      {open && (
        <div className="stats-detail">
          {s.slots.map((slot) => (
            <div key={slot.slot} className="slot-stat">
              <span className="slot-name" title={slot.name}>
                {slot.label}
                {slot.required && " *"}
              </span>
              <progress value={slot.filled} max={Math.max(s.entries, 1)} />
              <span className="faint">
                {slot.filled}/{s.entries}
              </span>
              {slot.states.source_missing > 0 && <Badge state="source_missing">{slot.states.source_missing} missing</Badge>}
              {slot.states.source_changed > 0 && <Badge state="source_changed">{slot.states.source_changed} changed</Badge>}
              {slot.states.invalid > 0 && <Badge state="invalid">{slot.states.invalid} invalid</Badge>}
            </div>
          ))}
          <div className="faint">{problems === 0 ? "No problems with the items." : `${problems} item(s) need attention.`}</div>
        </div>
      )}
    </div>
  );
}
