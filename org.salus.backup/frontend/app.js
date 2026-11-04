// The backup view: bundles on the left; the chosen bundle's folders, backups, snapshots and restore on the right.
// A bundle's password is asked when it is first needed and kept in this page only while the bundle is open; it is never
// stored, neither here nor in the backend.
import { api, bytes, duration, h, icon, message, modal } from "./common.js";
import { pickFolder } from "./picker.js";

const app = document.getElementById("app");
let settings = null; // {root, root_exists, restic: {ok, version, error}}
let list = []; // bundles in the backup root
let current = null; // name of the open bundle
let password = null; // the open bundle's password, once entered
let snapshots = null; // the open bundle's snapshots (null: not loaded)
let snapshot = null; // the snapshot being browsed
let tree = new Map(); // folder path in the snapshot -> {entries, open, loading, error}
let checked = new Set(); // paths ticked for restoring
let job = null; // the open bundle's running or last job
let notice = null; // {text, error}
let poll = null;

// --------------------------------------------------------------------------- loading

async function load() {
  try {
    settings = await api("GET", "/settings");
    if (settings.root) list = (await api("GET", "/bundles")).bundles;
  } catch (e) {
    notice = { text: message(e), error: true };
  }
  if (current && !list.some((b) => b.name === current)) close();
  render();
}

function open(name) {
  if (name === current) return;
  close();
  current = name;
  job = list.find((b) => b.name === name)?.job ?? null;
  if (job?.state === "running") watch();
  render();
}

function close() {
  current = password = snapshots = snapshot = null;
  tree = new Map();
  checked = new Set();
  job = null;
  clearInterval(poll);
}

const bundle = () => list.find((b) => b.name === current);
const path = (suffix = "") => `/bundles/${encodeURIComponent(current)}${suffix}`;

/** The open bundle's password: asks for it (and checks it) the first time it is needed. */
function unlock() {
  if (password) return Promise.resolve(password);
  return new Promise((resolve) => {
    const input = h("input", { type: "password", autocomplete: "off", placeholder: "Password" });
    const problem = h("div", { class: "error" });
    const ok = h("button", { class: "primary" }, "Unlock");
    const name = current;
    const submit = async () => {
      ok.disabled = true;
      try {
        await api("POST", `/bundles/${encodeURIComponent(name)}/unlock`, { json: { password: input.value } });
        if (current === name) password = input.value;
        dismiss();
        resolve(input.value);
        await loadSnapshots();
      } catch (e) {
        problem.textContent = message(e);
        ok.disabled = false;
        input.select();
      }
    };
    ok.addEventListener("click", submit);
    input.addEventListener("keydown", (e) => e.key === "Enter" && submit());
    const dismiss = modal(`Unlock “${name}”`, [
      h("p", { class: "hint" }, "The password is kept in this page only while the bundle is open, and never stored."),
      input, problem,
    ], [h("button", { onclick: () => (dismiss(), resolve(null)) }, "Cancel"), ok]);
    input.focus();
  });
}

async function loadSnapshots() {
  if (!password) return;
  try {
    snapshots = (await api("POST", path("/snapshots"), { json: { password } })).snapshots;
  } catch (e) {
    notice = { text: message(e), error: true };
  }
  render();
}

// --------------------------------------------------------------------------- jobs

async function startJob(route, extra = {}) {
  const pw = await unlock();
  if (!pw) return;
  try {
    job = await api("POST", path(route), { json: { password: pw, ...extra } });
    notice = null;
    watch();
  } catch (e) {
    notice = { text: message(e), error: true };
  }
  render();
}

function watch() {
  clearInterval(poll);
  const name = current;
  poll = setInterval(async () => {
    if (current !== name) return clearInterval(poll);
    try {
      job = (await api("GET", path("/job"))).job;
    } catch {
      return;
    }
    if (job?.state !== "running") {
      clearInterval(poll);
      if (job?.kind !== "restore") await loadSnapshots();
    }
    render();
  }, 700);
}

// --------------------------------------------------------------------------- the page

function render() {
  if (!settings) return app.replaceChildren(h("div", { class: "setup" }, notice ? h("div", { class: "error" }, notice.text) : "Loading…"));
  if (!settings.root) return app.replaceChildren(setupScreen());
  app.replaceChildren(side(), h("div", { class: "main" }, mainContent()));
}

function setupScreen() {
  const input = h("input", { type: "text", class: "mono", placeholder: "/path/to/backups", spellcheck: false });
  const problem = h("div", { class: "error" });
  const save = async () => {
    try {
      await api("PUT", "/settings", { json: { root: input.value } });
      await load();
    } catch (e) {
      problem.textContent = message(e);
    }
  };
  return h("div", { class: "setup" },
    h("h1", {}, "Where should backups go?"),
    h("p", { class: "hint" }, "Choose the backup folder. Every bundle becomes a folder in it. It is a setting of this plugin, so you can change it later in the Salus settings. A folder on another disk protects against the disk failing."),
    resticWarning(),
    h("div", { class: "toolbar" }, input, h("button", { onclick: async () => { const p = await pickFolder("Choose the backup folder", input.value.trim() || undefined, { create: true }); if (p) input.value = p; } }, "Browse…"),
      h("button", { class: "primary", onclick: save }, "Use this folder")),
    problem);
}

function resticWarning() {
  return settings.restic?.ok ? null : h("div", { class: "error" }, settings.restic?.error ?? "restic is missing.");
}

function side() {
  return h("div", { class: "side" },
    h("div", { class: "head" }, h("strong", {}, "Bundles"), h("button", { class: "with-icon", onclick: newBundleDialog, disabled: !settings.restic?.ok }, icon("add", 22), "New")),
    h("div", { class: "list" },
      list.map((b) => h("div", { class: "bundle" + (b.name === current ? " current" : ""), onclick: () => open(b.name) },
        h("div", {}, b.name),
        h("div", { class: "sub" }, b.job?.state === "running" ? `${label(b.job.kind)}…` : `${b.folders.length} folder${b.folders.length === 1 ? "" : "s"}${b.ok ? "" : " · repository missing"}`))),
      !list.length && h("div", { class: "empty" }, "No bundles yet.")),
    h("div", { class: "root", title: settings.root }, h("span", {}, settings.root), h("button", { class: "icon-button", title: "Refresh: look for bundles in the backup folder again", onclick: load }, icon("refresh", 22))));
}

const label = (kind) => ({ backup: "Backing up", restore: "Restoring", cleanup: "Cleaning up", check: "Checking" })[kind] ?? kind;

function mainContent() {
  const b = bundle();
  if (!b) {
    return [resticWarning(), h("div", { class: "empty" }, list.length ? "Choose a bundle on the left." : "Create a bundle: a set of folders that are backed up together, with its own password."),
      notice && h("div", { class: notice.error ? "error" : "ok" }, notice.text)];
  }
  const running = job?.state === "running";
  return [
    h("h1", {}, b.name, h("span", { class: "badge" + (password ? " open" : "") }, password ? "unlocked" : "locked"),
      h("span", { class: "grow" }),
      password ? h("button", { onclick: () => { const name = current; close(); open(name); } }, "Lock") : h("button", { onclick: unlock }, "Unlock")),
    resticWarning(),
    h("section", {},
      h("h2", {}, "Folders"),
      h("div", { class: "box" }, b.folders.map((f) => h("div", { class: "row" }, h("span", { class: "grow mono", title: f }, f),
        b.missing_folders.includes(f) && h("span", { class: "warn" }, "missing"))),
        !b.folders.length && h("div", { class: "empty" }, b.bare ? "A plain restic repository: its folders are not known here." : "No folders yet.")),
      b.excludes.length > 0 && h("p", { class: "hint" }, "Excluded: ", h("span", { class: "mono" }, b.excludes.join(", "))),
      h("div", { class: "toolbar", style: "margin-top:8px" },
        h("button", { class: "primary", disabled: running || !settings.restic?.ok, onclick: () => startJob("/backup") }, "Back up now"),
        !b.bare && h("button", { disabled: running, onclick: () => editDialog(b) }, "Edit folders…"),
        h("button", { disabled: running, onclick: cleanupDialog }, "Clean up…"),
        h("button", { disabled: running, onclick: () => startJob("/check") }, "Check integrity"),
        h("span", { class: "hint" }, `Stored in ${b.path}`))),
    job && jobPanel(),
    notice && h("div", { class: notice.error ? "error" : "ok" }, notice.text),
    snapshotsSection(),
    snapshot && browserSection(),
  ];
}

function jobPanel() {
  const p = job.progress ?? {};
  const running = job.state === "running";
  const fraction = job.state === "done" ? 1 : p.percent_done ?? 0;
  const s = job.summary ?? {};
  let detail = running && p.percent_done == null ? "Starting…" : "";
  if (running && job.kind === "backup" && p.percent_done != null) detail = `${p.files_done ?? 0} of ${p.total_files ?? "?"} files · ${bytes(p.bytes_done)} of ${bytes(p.total_bytes)}` + (p.seconds_remaining ? ` · about ${duration(p.seconds_remaining)} left` : "");
  if (running && job.kind === "restore" && p.percent_done != null) detail = `${p.files_restored ?? 0} of ${p.total_files ?? "?"} files · ${bytes(p.bytes_restored)} of ${bytes(p.total_bytes)}`;
  if (job.state === "done" && job.kind === "backup") detail = `${s.files_new ?? 0} new, ${s.files_changed ?? 0} changed, ${s.files_unmodified ?? 0} unchanged files · ${bytes(s.data_added_packed ?? s.data_added)} added (compressed) · ${duration(s.total_duration)}`;
  if (job.state === "done" && job.kind === "restore") detail = `${s.files_restored ?? 0} files restored${s.files_skipped ? `, ${s.files_skipped} kept as they were` : ""} into ${job.detail?.target}`;
  if (job.state === "done" && job.kind === "cleanup") detail = "Old snapshots were removed and their space freed.";
  if (job.state === "done" && job.kind === "check") detail = "No errors were found.";
  const finished = { backup: "Backup finished", restore: "Restore finished", cleanup: "Clean-up finished", check: "Check finished" }[job.kind];
  const title = running ? `${label(job.kind)}…` : job.state === "done" ? finished : job.state === "stopped" ? "Stopped" : "Failed";
  return h("div", { class: "job" + (job.state === "failed" ? " failed" : "") },
    h("div", { class: "toolbar" }, h("strong", {}, title), h("span", { class: "grow" }), running && h("button", { onclick: () => api("POST", path("/job/stop")).catch(() => undefined) }, "Stop")),
    (running || job.state === "done") && h("div", { class: "bar" + (running ? "" : " done") }, h("div", { style: `width:${(fraction * 100).toFixed(1)}%` })),
    detail && h("div", { class: "hint" }, detail),
    running && p.current && h("div", { class: "hint mono" }, p.current),
    job.message && h("div", { class: job.state === "failed" ? "error" : "warn" }, job.message),
    job.errors?.length > 0 && h("ul", { class: "errors" }, job.errors.map((e) => h("li", {}, e))));
}

function snapshotsSection() {
  if (!password) return h("section", {}, h("h2", {}, "Snapshots"), h("p", { class: "hint" }, "Unlock the bundle to see its snapshots and restore from them."));
  if (!snapshots) return h("section", {}, h("h2", {}, "Snapshots"), h("p", { class: "hint" }, "Loading…"));
  return h("section", {},
    h("h2", {}, `Snapshots (${snapshots.length})`),
    h("div", { class: "box", style: "max-height:260px" },
      snapshots.map((s) => h("div", { class: "row click" + (snapshot?.id === s.id ? " chosen" : ""), onclick: () => browse(s) },
        h("span", { class: "mono" }, s.short_id),
        h("span", {}, new Date(s.time).toLocaleString()),
        h("span", { class: "grow hint", title: s.paths.join("\n") }, `${s.hostname} · ${s.paths.join(", ")}`),
        s.files != null && h("span", { class: "num" }, `${s.files} files, ${bytes(s.bytes)}`),
        s.added != null && h("span", { class: "num", title: "New data this snapshot stored (compressed)" }, `+${bytes(s.added)}`))),
      !snapshots.length && h("div", { class: "empty" }, "No snapshots yet. Press “Back up now”.")));
}

// --------------------------------------------------------------------------- browsing and restoring

function browse(s) {
  snapshot = s;
  tree = new Map();
  checked = new Set();
  toggle("/");
}

async function toggle(dir) {
  const node = tree.get(dir) ?? {};
  tree.set(dir, node);
  node.open = !node.open;
  if (node.open && !node.entries && !node.loading) {
    node.loading = true;
    render();
    try {
      node.entries = (await api("POST", path("/ls"), { json: { password, snapshot: snapshot.id, path: dir } })).entries;
    } catch (e) {
      node.error = message(e);
    }
    node.loading = false;
  }
  render();
}

function treeRows(dir, depth) {
  const node = tree.get(dir);
  if (!node?.open) return [];
  const pad = `padding-left:${8 + depth * 16}px`;
  if (node.loading) return [h("div", { class: "node hint", style: pad }, "Loading…")];
  if (node.error) return [h("div", { class: "node error", style: pad }, node.error)];
  const out = [];
  for (const e of node.entries ?? []) {
    const isDir = e.type === "dir";
    out.push(h("div", { class: "node", style: pad },
      h("span", { class: "chevron", onclick: isDir ? () => toggle(e.path) : null }, isDir ? (tree.get(e.path)?.open ? "▾" : "▸") : ""),
      h("input", { type: "checkbox", checked: checked.has(e.path), onchange: (ev) => { ev.target.checked ? checked.add(e.path) : checked.delete(e.path); render(); } }),
      h("span", { onclick: isDir ? () => toggle(e.path) : null, style: isDir ? "cursor:pointer" : "" }, `${isDir ? "📁" : "📄"} ${e.name}`),
      !isDir && h("span", { class: "size num" }, bytes(e.size))));
    if (isDir) out.push(...treeRows(e.path, depth + 1));
  }
  if (!out.length) out.push(h("div", { class: "node hint", style: pad }, "Empty"));
  return out;
}

function browserSection() {
  return h("section", {},
    h("h2", {}, `Snapshot ${snapshot.short_id} · ${new Date(snapshot.time).toLocaleString()}`),
    h("div", { class: "box tree" }, treeRows("/", 0)),
    h("div", { class: "toolbar", style: "margin-top:8px" },
      h("button", { class: "primary", disabled: job?.state === "running", onclick: restoreDialog },
        checked.size ? `Restore ${checked.size} selected…` : "Restore the whole snapshot…"),
      checked.size > 0 && h("button", { onclick: () => { checked.clear(); render(); } }, "Clear selection")));
}

function choice(name, title, detail, onchange, extra) {
  return h("label", { class: "choice" }, h("input", { type: "radio", name, onchange }),
    h("span", {}, h("strong", {}, title), detail && h("span", { class: "detail" }, detail), extra));
}

function restoreDialog() {
  const items = [...checked];
  let where = null, overwrite = null;
  const target = h("input", { type: "text", class: "mono", placeholder: "/path/to/restore/into", spellcheck: false });
  const strip = h("input", { type: "checkbox", checked: true });
  const problem = h("div", { class: "error" });
  const go = h("button", { class: "primary", disabled: true }, "Restore");
  const update = () => (go.disabled = !where || overwrite === null);
  const example = items[0] ?? snapshot.paths[0] ?? "/home/me/data";
  const otherFolder = h("div", { class: "choices", style: "margin-top:6px" },
    h("div", { class: "toolbar" }, target, h("button", { type: "button", onclick: async () => { const p = await pickFolder("Restore into", target.value.trim() || undefined, { create: true }); if (p) target.value = p; } }, "Browse…")),
    h("label", { class: "toolbar" }, strip, h("span", { class: "hint" }, `Without the folders above the chosen items (${example} → <folder>/${example.split("/").pop()})`)));
  go.addEventListener("click", async () => {
    const json = { snapshot: snapshot.id, items, overwrite, target: where === "original" ? null : target.value.trim(), strip: strip.checked };
    if (where === "other" && !json.target) return void (problem.textContent = "Choose the folder to restore into.");
    close_();
    await startJob("/restore", json);
  });
  const close_ = modal(`Restore from snapshot ${snapshot.short_id}`, [
    h("p", {}, items.length ? `${items.length} selected item${items.length === 1 ? "" : "s"}: ` : "The whole snapshot: ", h("span", { class: "mono" }, (items.length ? items : snapshot.paths).slice(0, 3).join(", ")), items.length > 3 ? " …" : ""),
    h("strong", {}, "Where to?"),
    h("div", { class: "choices" },
      choice("where", "To the original locations", "Back to where the files were when they were backed up.", () => ((where = "original"), update())),
      choice("where", "Into another folder", "For example on another machine, or to compare before replacing anything.", () => ((where = "other"), update()), otherFolder)),
    h("strong", {}, "Files that exist already"),
    h("div", { class: "choices" },
      choice("overwrite", "Keep them", "Only missing files are restored.", () => ((overwrite = false), update())),
      choice("overwrite", "Replace them", "Files that differ from the snapshot are overwritten with the backed-up version.", () => ((overwrite = true), update()))),
    problem,
  ], [h("button", { onclick: () => close_() }, "Cancel"), go], { wide: true });
}

// --------------------------------------------------------------------------- bundles

function foldersEditor(initial) {
  const folders = [...initial];
  const box = h("div", { class: "box folders" });
  const draw = () => box.replaceChildren(...folders.map((f, i) => h("div", { class: "row" }, h("input", { type: "text", class: "mono", value: f, oninput: (e) => (folders[i] = e.target.value) }),
    h("button", { onclick: () => { folders.splice(i, 1); draw(); } }, "Remove"))), h("div", { class: "row" },
    h("button", { onclick: async () => { const p = await pickFolder("Add a folder to the bundle"); if (p) { folders.push(p); draw(); } } }, "+ Add folder…")));
  draw();
  return { element: box, value: () => folders.map((f) => f.trim()).filter(Boolean) };
}

function newBundleDialog() {
  const name = h("input", { type: "text", placeholder: "e.g. papers" });
  const pw = h("input", { type: "password", autocomplete: "new-password" });
  const pw2 = h("input", { type: "password", autocomplete: "new-password" });
  const folders = foldersEditor([]);
  const excludes = h("textarea", { placeholder: "One pattern per line, e.g.\n*.tmp\nnode_modules" });
  const problem = h("div", { class: "error" });
  const create = h("button", { class: "primary" }, "Create bundle");
  create.addEventListener("click", async () => {
    if (pw.value !== pw2.value) return void (problem.textContent = "The passwords do not match.");
    create.disabled = true;
    try {
      const made = await api("POST", "/bundles", { json: { name: name.value, password: pw.value, folders: folders.value(), excludes: excludes.value } });
      close_();
      await load();
      open(made.name);
      password = pw.value;
      await loadSnapshots();
    } catch (e) {
      problem.textContent = message(e);
      create.disabled = false;
    }
  });
  const close_ = modal("New bundle", [
    h("label", { class: "field" }, h("span", {}, "Name (also the bundle's folder name)"), name),
    h("label", { class: "field" }, h("span", {}, "Password (at least 8 characters)"), pw),
    h("label", { class: "field" }, h("span", {}, "Password again"), pw2),
    h("p", { class: "warn" }, "Keep the password safe: without it the backups cannot be read, and it cannot be recovered."),
    h("div", { class: "field" }, h("span", {}, "Folders"), folders.element),
    h("label", { class: "field" }, h("span", {}, "Exclude (optional)"), excludes),
    problem,
  ], [h("button", { onclick: () => close_() }, "Cancel"), create], { wide: true });
  name.focus();
}

function editDialog(b) {
  const folders = foldersEditor(b.folders);
  const excludes = h("textarea", { value: b.excludes.join("\n") });
  const problem = h("div", { class: "error" });
  const save = h("button", { class: "primary" }, "Save");
  save.addEventListener("click", async () => {
    try {
      await api("PUT", path(), { json: { folders: folders.value(), excludes: excludes.value } });
      close_();
      await load();
    } catch (e) {
      problem.textContent = message(e);
    }
  });
  const close_ = modal(`Folders of “${b.name}”`, [
    h("p", { class: "hint" }, "The next backup uses the new list. Earlier snapshots keep what they contained."),
    folders.element,
    h("label", { class: "field" }, h("span", {}, "Exclude (one pattern per line)"), excludes),
    problem,
  ], [h("button", { onclick: () => close_() }, "Cancel"), save], { wide: true });
}

function cleanupDialog() {
  const keep = h("input", { type: "number", min: 1, value: 10, style: "width:90px" });
  const go = h("button", { class: "primary" }, "Remove older snapshots");
  go.addEventListener("click", async () => {
    close_();
    await startJob("/cleanup", { keep_last: Number(keep.value) });
  });
  const close_ = modal("Clean up", [
    h("div", { class: "toolbar" }, "Keep the newest", keep, "snapshots"),
    h("p", { class: "warn" }, "Older snapshots are deleted for good, and the space only they used is freed."),
  ], [h("button", { onclick: () => close_() }, "Cancel"), go]);
}

load();
