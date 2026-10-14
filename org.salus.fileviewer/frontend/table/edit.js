// Edit mode of the table viewer (CSV and TSV). The edits go to a private working copy on the server; "Save" writes them to the
// file. Nothing is locked: if the file changed meanwhile, the save asks what to do (save elsewhere, overwrite, discard).
// Every value is text, exactly as in the file, and the server writes it back quoted where needed, so a comma, a quote or a
// line break in a cell cannot break the file.

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function node(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== false) el.setAttribute(key, value === true ? "" : value);
  }
  el.append(...children.filter((c) => c !== null && c !== undefined));
  return el;
}

export function createEditor({ salus, getFile, setFile, show, pager, onLeave }) {
  let session = null;   // what the server says about the editing session
  let page = [];        // the rows on screen: [[row id, value of each column ...], ...]
  let total = 0;
  let pending = Promise.resolve(); // the last cell commit; a save waits for it
  let noticeTimer = 0;

  const path = (suffix) => `/table/edit/${session.id}${suffix}`;
  const message = (error) => error.body || error.message || String(error);

  // ---- the notice bar (saved, errors, the conflict question)
  function notice(text, kind = "info", ...extra) {
    clearTimeout(noticeTimer);
    const bar = $("notice");
    bar.className = `notice ${kind}`;
    bar.replaceChildren(node("span", { class: "notice-text" }, text), ...extra);
    bar.hidden = false;
    if (kind === "ok") noticeTimer = setTimeout(() => (bar.hidden = true), 3500);
  }
  const hideNotice = () => ($("notice").hidden = true);
  const action = (label, onclick, primary = false) => node("button", { class: primary ? "primary" : "", onclick }, label);

  function updateUi() {
    const dirty = session.modified > 0;
    $("dirty").hidden = !dirty;
    $("save").disabled = !dirty;
    $("discard").disabled = !dirty;
  }

  // ---- entering and leaving
  async function enter() {
    document.body.classList.add("editing");
    $("grid").hidden = true;
    $("pager").hidden = true;
    show("Loading for editing…");
    try {
      let info = await salus.http.json("POST", "/table/edit/start", { json: { file: getFile() } });
      while (info.state === "loading") {
        show(info.progress ? `Loading for editing… ${info.progress.toLocaleString()} rows` : "Loading for editing…");
        await sleep(300);
        info = await salus.http.json("GET", `/table/edit/${info.id}`);
      }
      if (info.state === "error") throw new Error(info.error);
      session = info;
    } catch (error) {
      document.body.classList.remove("editing");
      notice(`Cannot edit this file: ${message(error)}`, "error");
      return false;
    }
    hideNotice();
    pager.offset = 0;
    renderHead();
    await load();
    updateUi();
    if (session.skipped_blank) notice(`${session.skipped_blank} blank line(s) in the file are dropped when you save.`, "info");
    return true;
  }

  /** Leaving edit mode saves what changed. Returns false if that needs the user's decision first (the conflict question). */
  async function leave() {
    await pending;
    if (session.modified > 0 && !(await save())) return false;
    await close();
    return true;
  }

  async function close() {
    const id = session.id;
    session = null;
    document.body.classList.remove("editing");
    hideNotice();
    salus.http.request("DELETE", `/table/edit/${id}`).catch(() => undefined);
    await onLeave();
  }

  // ---- showing the working copy
  async function load() {
    let result;
    try {
      result = await salus.http.json("GET", path("/rows"), { query: { limit: pager.size, offset: pager.offset } });
    } catch (error) {
      return show(message(error), true);
    }
    total = result.total;
    if (result.rows.length === 0 && pager.offset > 0) {
      pager.offset = Math.max(0, Math.floor((total - 1) / pager.size) * pager.size);
      return load();
    }
    page = result.rows;
    show(session.columns.length === 0 ? "This file has no columns. Add one with “Add column”." : total === 0 ? "No rows. Add one with “Add row”." : "");
    renderBody();
    const from = total === 0 ? 0 : pager.offset + 1;
    const to = Math.min(pager.offset + pager.size, total);
    $("range").textContent = `${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()}`;
    $("prev").disabled = pager.offset === 0;
    $("next").disabled = to >= total;
    $("grid").hidden = session.columns.length === 0;
    $("pager").hidden = false;
  }

  function renderHead() {
    $("filters").replaceChildren();
    $("head").replaceChildren(
      node("th", { class: "tools" }),
      ...session.columns.map((column, index) => {
        const name = node("span", { class: `colname ${column.name === "" ? "unnamed" : ""}`, title: "Double-click to rename" }, column.name || `(column ${index + 1})`);
        name.addEventListener("dblclick", () => renameColumn(column, name));
        return node("th", { class: "edit-head" }, name,
          node("button", { class: "tiny", title: "Add a column after this one", onclick: () => addColumn(column.id) }, "+"),
          node("button", { class: "tiny", title: "Delete this column", onclick: () => deleteColumn(column) }, "×"));
      }),
    );
  }

  function renderBody() {
    $("body").replaceChildren(...page.map((row, i) => {
      const id = row[0];
      const tr = node("tr", { "data-id": id },
        node("td", { class: "tools" },
          node("span", { class: "rownum" }, String(pager.offset + i + 1)),
          node("button", { class: "tiny", title: "Insert a row below", onclick: () => addRow({ after: id }) }, "+"),
          node("button", { class: "tiny", title: "Delete this row", onclick: () => deleteRow(id) }, "×")));
      session.columns.forEach((column, c) => {
        const td = node("td", { class: "cell", title: row[c + 1] }, row[c + 1]);
        td.dataset.row = id;
        td.dataset.col = column.id;
        td.addEventListener("click", () => beginEdit(td));
        tr.append(td);
      });
      return tr;
    }));
  }

  // ---- editing a cell
  function beginEdit(td) {
    if (td.querySelector("textarea")) return;
    const original = rowValue(td);
    const box = node("textarea", { class: "cell-input", rows: "1", spellcheck: "false" });
    box.value = original;
    const fit = () => (box.rows = Math.min(8, Math.max(1, box.value.split("\n").length)));
    let finished = false;
    const finish = (commit) => {
      if (finished) return;
      finished = true;
      const value = box.value;
      td.textContent = commit ? value : original;
      td.title = commit ? value : original;
      if (commit && value !== original) commitCell(td, original, value);
    };
    box.addEventListener("input", fit);
    box.addEventListener("blur", () => finish(true));
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        finish(true);
      } else if (e.key === "Escape") {
        e.preventDefault();
        finish(false);
      } else if (e.key === "Tab") {
        e.preventDefault();
        const next = e.shiftKey ? td.previousElementSibling : td.nextElementSibling;
        finish(true);
        if (next && next.classList.contains("cell")) beginEdit(next);
      }
    });
    td.textContent = "";
    td.append(box);
    fit();
    box.focus();
    box.select();
  }

  const rowValue = (td) => page.find((r) => r[0] === Number(td.dataset.row))?.[1 + session.columns.findIndex((c) => c.id === Number(td.dataset.col))] ?? "";

  function commitCell(td, original, value) {
    const rowId = Number(td.dataset.row);
    const colId = Number(td.dataset.col);
    const row = page.find((r) => r[0] === rowId);
    const index = session.columns.findIndex((c) => c.id === colId);
    if (row) row[1 + index] = value;
    pending = pending.then(async () => {
      try {
        const reply = await salus.http.json("POST", path("/cells"), { json: { edits: [{ row: rowId, col: colId, value }] } });
        session.modified = reply.modified;
        updateUi();
      } catch (error) {
        if (row) row[1 + index] = original; // the server did not take it: show what is really there
        td.textContent = original;
        notice(`The change was not saved to the working copy: ${message(error)}`, "error");
      }
    });
  }

  // ---- rows and columns
  async function change(promise) {
    try {
      const reply = await promise;
      session.modified = reply.modified ?? session.modified;
      if (reply.columns) session.columns = reply.columns;
      return reply;
    } catch (error) {
      notice(message(error), "error");
      return null;
    }
  }

  async function addRow(where) {
    await pending;
    const reply = await change(salus.http.json("POST", path("/rows"), { json: { where } }));
    if (!reply) return;
    if (where === "end") pager.offset = Math.floor((reply.rows - 1) / pager.size) * pager.size;
    else if (where.after !== undefined && page.findIndex((r) => r[0] === where.after) === page.length - 1 && page.length >= pager.size) pager.offset += pager.size;
    await load();
    updateUi();
    const fresh = document.querySelector(`tr[data-id="${reply.ids[0]}"] td.cell`);
    if (fresh) (fresh.scrollIntoView({ block: "nearest" }), beginEdit(fresh));
  }

  async function deleteRow(id) {
    await pending;
    if (await change(salus.http.json("POST", path("/rows/delete"), { json: { ids: [id] } }))) (await load(), updateUi());
  }

  async function addColumn(after) {
    await pending;
    const reply = await change(salus.http.json("POST", path("/columns"), { json: { name: "new column", after } }));
    if (!reply) return;
    renderHead();
    await load();
    updateUi();
    const heads = [...document.querySelectorAll("#head .colname")];
    const index = session.columns.findIndex((c) => c.id === reply.column.id);
    if (heads[index]) renameColumn(reply.column, heads[index], true);
  }

  async function deleteColumn(column) {
    await pending;
    if (await change(salus.http.json("POST", path("/columns/delete"), { json: { col: column.id } }))) (renderHead(), await load(), updateUi());
  }

  function renameColumn(column, label, selectAll = false) {
    const input = node("input", { class: "rename", value: column.name, spellcheck: "false" });
    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      if (commit && input.value !== column.name) {
        await pending;
        await change(salus.http.json("POST", path("/columns/rename"), { json: { col: column.id, name: input.value } }));
        updateUi();
      }
      renderHead();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") (e.preventDefault(), finish(true));
      else if (e.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
    label.replaceWith(input);
    input.focus();
    if (selectAll || column.name) input.select();
  }

  // ---- saving
  async function post(suffix, body) {
    const response = await salus.http.post(path(suffix), { json: body });
    const isJson = (response.headers.get("content-type") || "").includes("json");
    return { ok: response.ok, status: response.status, data: isJson ? await response.json() : null, text: isJson ? "" : await response.text() };
  }

  /** @returns {Promise<boolean>} true when the working copy is in the file now */
  async function save() {
    await pending;
    if (!session) return false;
    const reply = await post("/save", {});
    return handleSaveReply(reply);
  }

  function handleSaveReply(reply) {
    if (reply.ok) {
      session.modified = 0;
      updateUi();
      if (reply.data.saved && reply.data.saved !== getFile()) setFile(reply.data.saved);
      notice(`Saved ${reply.data.name}.`, "ok");
      return true;
    }
    if (reply.status === 409 && reply.data?.conflict) {
      askConflict(reply.data.conflict);
      return false;
    }
    notice(reply.text || "Could not save.", "error");
    return false;
  }

  function askConflict(kind) {
    const text = kind === "deleted" ? "The file was deleted since you started editing." : "The file was changed by someone else since you started editing.";
    notice(`${text} Your edits are still here. What now?`, "conflict",
      action("Save elsewhere…", askElsewhere, true),
      action("Overwrite anyway", async () => handleSaveReply(await post("/save", { overwrite: true }))),
      action("Discard and reload", discard));
  }

  function askElsewhere() {
    const original = getFile();
    const input = node("input", { class: "path", value: original.replace(/(\.[^./\\]+)$/, ".edited$1"), spellcheck: "false" });
    const hint = node("div", { class: "path-hint" });
    let timer = 0;
    const check = async () => {
      try {
        const info = await salus.http.json("GET", path("/path-info"), { query: { path: input.value } });
        const notes = [];
        if (info.exists) notes.push("A file with that name exists already.");
        if (info.folder_is_source) notes.push("This folder is a tabular source cohort. A second CSV file there makes the source ambiguous until one is removed.");
        hint.textContent = notes.join(" ");
        hint.className = `path-hint ${notes.length ? "warn" : ""}`;
      } catch (error) {
        hint.textContent = message(error);
        hint.className = "path-hint warn";
      }
    };
    input.addEventListener("input", () => (clearTimeout(timer), (timer = setTimeout(check, 250))));
    const submit = async () => {
      const reply = await post("/save", { path: input.value });
      if (reply.ok) handleSaveReply(reply);
      else if (reply.data?.conflict) askConflict(reply.data.conflict);
      else (hint.textContent = reply.text || "Could not save.", (hint.className = "path-hint warn"));
    };
    input.addEventListener("keydown", (e) => e.key === "Enter" && submit());
    notice("Save a copy as (a path on the server; relative paths start in the file's folder):", "conflict",
      input, action("Save copy", submit, true), action("Back", () => askConflict("changed")), hint);
    input.focus();
    input.select();
    check();
  }

  async function discard() {
    await pending;
    try {
      session = await salus.http.json("POST", path("/discard"));
    } catch (error) {
      return notice(message(error), "error");
    }
    pager.offset = 0;
    renderHead();
    await load();
    updateUi();
    notice("Changes discarded; the file was loaded again.", "ok");
  }

  // ---- the buttons
  $("save").addEventListener("click", () => session && save());
  $("discard").addEventListener("click", () => session && discard());
  $("addrow").addEventListener("click", () => session && addRow("end"));
  $("addcol").addEventListener("click", () => session && addColumn(null));
  window.addEventListener("keydown", (e) => {
    if (session && (e.ctrlKey || e.metaKey) && e.key === "s") (e.preventDefault(), save());
  });

  return { enter, leave, load, isEditing: () => session !== null };
}
