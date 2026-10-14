import { Salus } from "../salus_sdk.js";
import { createEditor } from "./edit.js";

const salus = Salus.connect();
let file = salus.params?.file; // edit mode can move it ("save elsewhere")
const $ = (id) => document.getElementById(id);

const state = { info: null, table: null, offset: 0, size: 100, sort: null, desc: false, search: "", filters: {}, request: 0 };

function show(text, error = false) {
  const el = $("message");
  el.textContent = text;
  el.className = "message" + (error ? " error" : "");
  el.hidden = !text;
}

async function open() {
  if (!file) return show("No file was given.", true);
  $("file").textContent = file.split(/[\\/]/).pop();
  $("file").title = file;
  for (;;) {
    try {
      state.info = await salus.http.json("GET", "/table/open", { query: { file } });
    } catch (error) {
      return show(`Could not open the file: ${error.body || error.message}`, true);
    }
    if (state.info.state === "error") return show(state.info.error, true);
    if (state.info.state === "ready") break;
    show(state.info.progress ? `Loading… ${state.info.progress.toLocaleString()} rows` : "Loading…");
    await new Promise((r) => setTimeout(r, 400));
  }
  const select = $("table");
  select.replaceChildren(...state.info.tables.map((t) => new Option(`${t.name} (${t.rows.toLocaleString()})`, t.name)));
  select.hidden = state.info.tables.length < 2;
  chooseTable(state.info.tables[0].name);
}

function chooseTable(name) {
  state.table = state.info.tables.find((t) => t.name === name);
  Object.assign(state, { offset: 0, sort: null, desc: false, filters: {} });
  const table = state.table;
  $("infobox").replaceChildren(...table.columns.map((c) => {
    const span = document.createElement("span");
    span.innerHTML = `<code></code> ${c.type.toLowerCase()}`;
    span.firstChild.textContent = c.name;
    return span;
  }));
  buildHeader();
  load();
}

function buildHeader() {
  const head = $("head"), filters = $("filters");
  head.replaceChildren(...state.table.columns.map((c) => {
    const th = document.createElement("th");
    th.title = `${c.name} (${c.type.toLowerCase()}) — click to sort`;
    th.textContent = c.name;
    th.addEventListener("click", () => {
      state.desc = state.sort === c.name ? !state.desc : false;
      state.sort = c.name;
      state.offset = 0;
      markSort();
      load();
    });
    return th;
  }));
  filters.replaceChildren(...state.table.columns.map((c) => {
    const th = document.createElement("th");
    const input = document.createElement("input");
    input.placeholder = "filter";
    let timer;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        state.filters[c.name] = input.value;
        state.offset = 0;
        load();
      }, 300);
    });
    th.append(input);
    return th;
  }));
  markSort();
}

function markSort() {
  [...$("head").children].forEach((th, i) => {
    const name = state.table.columns[i].name;
    th.textContent = name;
    if (state.sort === name) {
      const arrow = document.createElement("span");
      arrow.className = "arrow";
      arrow.textContent = state.desc ? " ▼" : " ▲";
      th.append(arrow);
    }
  });
}

async function load() {
  if (editor.isEditing()) return editor.load();
  const ticket = ++state.request;
  let result;
  try {
    result = await salus.http.json("GET", "/table/rows", {
      query: {
        file, table: state.table.name, limit: state.size, offset: state.offset,
        sort: state.sort ?? "", desc: state.desc ? "1" : "0", q: state.search,
        filters: JSON.stringify(Object.fromEntries(Object.entries(state.filters).filter(([, v]) => v))),
      },
    });
  } catch (error) {
    return show(error.body || error.message, true);
  }
  if (ticket !== state.request) return; // a newer request superseded this one
  show(result.total === 0 ? "No rows match." : "");
  $("grid").hidden = $("pager").hidden = false;
  const numeric = state.table.columns.map((c) => c.type === "INTEGER" || c.type === "REAL");
  $("body").replaceChildren(...result.rows.map((row) => {
    const tr = document.createElement("tr");
    row.forEach((value, i) => {
      const td = document.createElement("td");
      if (value === null) {
        td.className = "null";
        td.textContent = "null";
      } else {
        td.textContent = String(value);
        td.title = String(value);
        if (numeric[i]) td.className = "num";
      }
      tr.append(td);
    });
    return tr;
  }));
  const from = result.total === 0 ? 0 : state.offset + 1;
  const to = Math.min(state.offset + state.size, result.total);
  $("range").textContent = `${from.toLocaleString()}–${to.toLocaleString()} of ${result.total.toLocaleString()}`;
  $("prev").disabled = state.offset === 0;
  $("next").disabled = to >= result.total;
  state.total = result.total;
}

const editor = createEditor({
  salus,
  getFile: () => file,
  setFile: (path) => { file = path; $("file").textContent = path.split(/[\\/]/).pop(); $("file").title = path; },
  show,
  pager: state_pager(),
  onLeave: () => open(), // back to the read-only view; the server notices that the file changed and reads it again
});

function state_pager() {
  // the editor pages with the same offset and size as the read-only view
  return { get offset() { return state.offset; }, set offset(v) { state.offset = v; }, get size() { return state.size; } };
}

// Only CSV and TSV can be edited.
if (file && /\.(csv|tsv)$/i.test(file)) $("editToggle").hidden = false;
$("edit").addEventListener("change", async (e) => {
  const box = e.target;
  box.disabled = true;
  const ok = box.checked ? await editor.enter() : await editor.leave();
  // If entering failed, or leaving needs a decision (the conflict question), the box shows what is really going on.
  box.checked = editor.isEditing();
  box.disabled = false;
  if (!box.checked && !ok) await open();
});

$("table").addEventListener("change", (e) => chooseTable(e.target.value));
$("prev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.size); load(); });
$("next").addEventListener("click", () => { state.offset += state.size; load(); });
$("size").addEventListener("change", (e) => { state.size = Number(e.target.value); state.offset = 0; load(); });
$("info").addEventListener("click", () => { $("infobox").hidden = !$("infobox").hidden; });
let searchTimer;
$("search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.search = e.target.value; state.offset = 0; load(); }, 300);
});
$("infobox").hidden = true;
open();
