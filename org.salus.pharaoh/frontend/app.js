import { Salus } from "./salus_sdk.js";

const salus = Salus.connect();
const root = document.getElementById("app");

// --------------------------------------------------------------------------- small helpers

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key in el && key !== "list") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined && child !== false) el.append(child);
  return el;
}

function formatBytes(n) {
  if (n === null || n === undefined) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) (n /= 1024), i++;
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function formatDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const h_ = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  return h_ ? `${h_} h ${String(m).padStart(2, "0")} min` : m ? `${m} min ${String(s).padStart(2, "0")} s` : `${s} s`;
}

const api = (method, path, options) => salus.http.json(method, path, options);
const message = (error) => (typeof error?.body === "string" && error.body ? error.body : error?.message ?? String(error));

// --------------------------------------------------------------------------- state

const STEPS = ["Files", "Transformations", "Saving", "Progress"];
let server = null; // the last /state
let step = 0;
let selected = new Set();
let notice = null; // {text, error}
let dialog = null; // a DOM element
let timer = null;

async function refresh() {
  try {
    server = await api("GET", "/state");
  } catch (error) {
    notice = { text: `Cannot reach the plugin backend: ${message(error)}`, error: true };
  }
  const known = new Set(server?.items.map((i) => i.id) ?? []);
  for (const id of selected) if (!known.has(id)) selected.delete(id);
}

function wantsPolling() {
  if (!server) return true;
  if (server.run.state === "running") return true;
  return step === 0 && server.items.some((i) => i.inspecting);
}

function schedulePoll() {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    if (wantsPolling()) {
      await refresh();
      renderLive();
    }
    schedulePoll();
  }, server?.run.state === "running" ? 500 : 800);
}

function go(to) {
  step = to;
  notice = null;
  render();
}

// --------------------------------------------------------------------------- the frame

let page = null; // the body of the current step
let footer = null;
let live = null; // re-renders the parts of a step that follow the server

function render() {
  live = null;
  const body = [renderSteps()];
  if (!server) {
    root.replaceChildren(...body, h("div", { class: "page" }, h("p", { class: "faint" }, "Loading…")));
    return;
  }
  const view = [renderFiles, renderTransform, renderSaving, renderProgress][step]();
  page = h("div", { class: "page" }, notice && h("div", { class: "notice" + (notice.error ? " error" : "") }, notice.text), view.page);
  footer = h("div", { class: "footer" }, view.footer);
  live = view.live ?? null;
  root.replaceChildren(...body, page, footer, ...(dialog ? [dialog] : []));
}

function renderLive() {
  if (live) live();
  else if (!server) render();
}

function renderSteps() {
  return h("div", { class: "steps" }, STEPS.map((name, i) =>
    h("div", { class: "step" + (i === step ? " current" : i < step ? " done" : "") }, h("span", { class: "num" }, i < step ? "✓" : String(i + 1)), name)));
}

function openDialog(el) {
  dialog = el;
  render();
}

function closeDialog() {
  dialog = null;
  render();
}

async function guard(action) {
  try {
    await action();
    return true;
  } catch (error) {
    notice = { text: message(error), error: true };
    render();
    return false;
  }
}

// --------------------------------------------------------------------------- step 1: the files

function renderFiles() {
  const tbody = h("tbody");
  const summary = h("span", { class: "faint" });
  const removeButton = h("button", { onclick: removeSelected }, "Remove selected");
  const clearButton = h("button", { onclick: clearAll }, "Remove all");
  const nextButton = h("button", { class: "primary", onclick: () => go(1) }, "Next");
  const tableWrap = h("div", { class: "table-wrap" });

  async function removeSelected() {
    if (await guard(() => api("POST", "/items/remove", { json: { ids: [...selected] } }))) {
      selected.clear();
      await refresh();
      render();
    }
  }

  async function clearAll() {
    if (await guard(() => api("POST", "/items/clear"))) {
      selected.clear();
      await refresh();
      render();
    }
  }

  function update() {
    const items = server.items;
    const readable = items.filter((i) => !i.error);
    tbody.replaceChildren(...items.map((item) => {
      const slash = item.rel.lastIndexOf("/");
      const info = item.info;
      return h("tr", {
        class: selected.has(item.id) ? "selected" : "",
        onclick: (e) => {
          if (e.target.tagName === "INPUT") return;
          toggle(item.id);
        },
      },
        h("td", {}, h("input", { type: "checkbox", checked: selected.has(item.id), onchange: () => toggle(item.id) })),
        h("td", { class: "name", title: item.path }, slash >= 0 && h("span", { class: "dir" }, item.rel.slice(0, slash + 1)), item.rel.slice(slash + 1)),
        h("td", { class: "num" }, formatBytes(item.size)),
        h("td", { class: "num" }, info ? `${info.width.toLocaleString()} × ${info.height.toLocaleString()}` : ""),
        h("td", { class: "num" }, info ? String(info.levels) : ""),
        h("td", { class: item.error ? "error" : "faint", title: item.error ?? "" },
          item.error ? "Cannot be read" : item.inspecting ? "Reading…" : info.levels > 1 ? "Has a pyramid" : "No pyramid"));
    }));
    tableWrap.replaceChildren(items.length ? h("table", {},
      h("thead", {}, h("tr", {},
        h("th", {}, h("input", { type: "checkbox", checked: items.length > 0 && selected.size === items.length, onchange: (e) => (selected = e.target.checked ? new Set(items.map((i) => i.id)) : new Set(), update()) })),
        h("th", {}, "File"), h("th", { class: "num" }, "Size"), h("th", { class: "num" }, "Dimensions"), h("th", { class: "num" }, "Levels"), h("th", {}, "State"))),
      tbody) : h("div", { class: "empty" }, "No files yet. Press + to add a file or a folder of slides."));
    const total = items.reduce((sum, i) => sum + i.size, 0);
    summary.textContent = items.length ? `${items.length} file${items.length === 1 ? "" : "s"}, ${formatBytes(total)}${items.length > readable.length ? `, ${items.length - readable.length} cannot be read` : ""}` : "";
    removeButton.disabled = selected.size === 0;
    removeButton.textContent = selected.size ? `Remove ${selected.size} selected` : "Remove selected";
    clearButton.disabled = items.length === 0;
    nextButton.disabled = readable.length === 0 || items.some((i) => i.inspecting);
  }

  function toggle(id) {
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
    update();
  }

  update();
  return {
    live: update,
    page: [
      h("h2", {}, "Which slides?"),
      h("p", { class: "lead" }, "Add the whole slide images to work on. A folder adds every slide in it, including subfolders. Nothing is changed in this step."),
      h("div", { class: "toolbar" },
        h("button", { class: "plus", title: "Add a file or a folder", onclick: () => openAddDialog() }, "+"),
        removeButton, clearButton, h("span", { class: "grow" }), summary),
      tableWrap,
    ],
    footer: [h("span", { class: "grow" }), nextButton],
  };
}

function openAddDialog() {
  // The server's folders as a tree that loads a folder when it is opened. Checked rows are what gets added.
  const nodes = new Map(); // path -> {loaded, loading, open, folders: [name], files: [{name, size}], error}
  const checked = new Set();
  const jump = h("input", { type: "text", placeholder: "Jump to a path, e.g. /data/slides", spellcheck: false });
  const tree = h("div", { class: "browser tree" });
  const error = h("div", { class: "error" });
  const addButton = h("button", { class: "primary", onclick: addChecked }, "Add");
  let rootPath = "/";

  const join = (dir, name) => `${dir.replace(/\/$/, "")}/${name}`;

  async function load(path) {
    const node = nodes.get(path) ?? {};
    nodes.set(path, node);
    if (node.loaded || node.loading) return node;
    node.loading = true;
    draw();
    try {
      const listing = await api("GET", "/fs", { query: { path } });
      Object.assign(node, { folders: listing.folders.map((f) => f.name), files: listing.files, error: null, loaded: true });
    } catch (e) {
      Object.assign(node, { folders: [], files: [], error: message(e), loaded: true });
    }
    node.loading = false;
    draw();
    return node;
  }

  async function toggle(path) {
    const node = nodes.get(path) ?? {};
    nodes.set(path, node);
    node.open = !node.open;
    if (node.open) await load(path);
    draw();
  }

  /** Opens every folder on the way to a path. */
  async function reveal(path) {
    const parts = path.split("/").filter(Boolean);
    let current = "/";
    for (const part of [null, ...parts]) {
      if (part !== null) current = join(current, part);
      const node = nodes.get(current) ?? {};
      nodes.set(current, node);
      await load(current);
      if (node.error) break;
      node.open = true;
    }
    draw();
  }

  function row(depth, path, name, { folder, size, node }) {
    const open = folder && node?.open;
    return h("div", { class: "entry" + (folder ? "" : " file"), "data-path": path, style: `padding-left:${8 + depth * 18}px` },
      h("span", { class: "chevron", onclick: folder ? () => toggle(path) : null }, folder ? (open ? "▾" : "▸") : ""),
      h("input", { type: "checkbox", checked: checked.has(path), onchange: (e) => (e.target.checked ? checked.add(path) : checked.delete(path), updateButton()) }),
      h("span", { class: "label", title: path, onclick: folder ? () => toggle(path) : () => { checked.has(path) ? checked.delete(path) : checked.add(path); draw(); } },
        h("span", { class: "icon" }, folder ? "📁" : "▫"), name),
      size !== undefined && h("span", { class: "size" }, formatBytes(size)));
  }

  function rows(path, depth) {
    const node = nodes.get(path);
    if (!node?.open) return [];
    const out = [];
    if (node.loading && !node.loaded) out.push(h("div", { class: "entry faint", style: `padding-left:${8 + depth * 18}px` }, "Loading…"));
    if (node.error) out.push(h("div", { class: "entry error", style: `padding-left:${8 + depth * 18}px` }, node.error));
    for (const name of node.folders ?? []) {
      const child = join(path, name);
      out.push(row(depth, child, name, { folder: true, node: nodes.get(child) }), ...rows(child, depth + 1));
    }
    for (const file of node.files ?? []) out.push(row(depth, join(path, file.name), file.name, { folder: false, size: file.size }));
    if (node.loaded && !node.error && !node.folders.length && !node.files.length) out.push(h("div", { class: "entry faint", style: `padding-left:${8 + depth * 18}px` }, "Empty"));
    return out;
  }

  function draw() {
    const root_ = nodes.get(rootPath);
    tree.replaceChildren(row(0, rootPath, "/ (server)", { folder: true, node: root_ }), ...rows(rootPath, 1));
    updateButton();
  }

  function show(path) {
    const el = [...tree.children].find((child) => child.dataset.path === path);
    if (el) tree.scrollTop = Math.max(0, el.offsetTop - tree.offsetTop - 60);
  }

  function updateButton() {
    addButton.disabled = checked.size === 0;
    addButton.textContent = checked.size ? `Add ${checked.size} selected` : "Add";
  }

  async function addChecked() {
    addButton.disabled = true;
    let added = 0, listed = 0;
    for (const path of checked) {
      try {
        const result = await api("POST", "/items", { json: { path } });
        added += result.added;
        listed += result.already_listed;
      } catch (e) {
        error.textContent = `${path}: ${message(e)}`;
        updateButton();
        await refresh();
        return;
      }
    }
    dialog = null;
    notice = added === 0 ? { text: "Everything selected was in the list already.", error: false } : null;
    await refresh();
    render();
  }

  jump.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    const path = jump.value.trim().replace(/^~(?=\/|$)/, home ?? "~");
    if (!path.startsWith("/")) return void (error.textContent = "Enter the full path (it starts with /).");
    error.textContent = "";
    await reveal(path);
    checked.add(path);
    draw();
    show(path);
  });

  let home = null;
  const el = h("div", { class: "backdrop" }, h("div", { class: "dialog wide" },
    h("header", {}, "Add files or folders"),
    h("div", { class: "content" },
      jump, tree,
      h("div", { class: "faint" }, "Open folders with the arrows. Tick a folder to add every slide in it (including subfolders), or tick single slide files."),
      error),
    h("footer", {}, h("button", { onclick: closeDialog }, "Cancel"), addButton)));
  openDialog(el);
  (async () => {
    await load(rootPath);
    nodes.get(rootPath).open = true;
    try {
      home = (await api("GET", "/fs", { query: { path: "~" } })).path;
    } catch {
      home = null;
    }
    const last = server.items.length ? parentOf(server.items[server.items.length - 1].path) : home;
    await reveal(last ?? "/");
    if (last) show(last);
  })();
}

const parentOf = (path) => path.slice(0, Math.max(1, path.lastIndexOf("/")));

// --------------------------------------------------------------------------- step 2: the transformations

function renderTransform() {
  const t = { ...server.transform };
  const qualityRow = [
    h("label", { class: "field" }, "Quality"),
    h("div", {}, h("input", { type: "number", min: 1, max: 100, value: t.quality, oninput: (e) => (t.quality = Number(e.target.value)) }),
      " ", h("span", { class: "faint" }, "1 – 100. Lower is smaller; 70–85 is usually hard to tell from the original.")),
  ];
  const qualityLabel = qualityRow[0];
  const qualityInput = qualityRow[1];
  const compression = h("select", { onchange: (e) => (t.compression = e.target.value, sync()) },
    [["jpeg", "JPEG (lossy, smallest slides)"], ["webp", "WebP (lossy)"], ["deflate", "Deflate (lossless)"], ["zstd", "Zstandard (lossless)"], ["lzw", "LZW (lossless)"], ["none", "None (uncompressed)"]]
      .map(([value, text]) => h("option", { value, selected: value === t.compression }, text)));
  const hint = h("div", { class: "hint" });

  function sync() {
    const lossy = t.compression === "jpeg" || t.compression === "webp";
    qualityLabel.hidden = qualityInput.hidden = !lossy;
    hint.textContent = lossy
      ? "Every file is encoded again. Re-encoding an already lossy slide loses a little more detail; to only add levels, choose a high quality (90+) or a lossless compression."
      : "Lossless: the pixels stay exactly as they are, but files are usually larger than with JPEG.";
  }
  sync();

  async function next() {
    if (!Number.isFinite(t.quality)) t.quality = 80;
    if (await guard(() => api("PUT", "/plan", { json: { transform: t } }))) {
      await refresh();
      go(2);
    }
  }

  return {
    page: [
      h("h2", {}, "What should happen to the slides?"),
      h("p", { class: "lead" }, "Every slide is read once and written as a tiled BigTIFF. The original files are only changed if you choose that in the next step."),
      h("div", { class: "form" },
        h("label", { class: "field" }, "Pyramid"),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: t.pyramid, onchange: (e) => (t.pyramid = e.target.checked) }), "Build all missing resolution levels"),
        h("div", { class: "hint" }, "Levels half the size of the previous one, down to a single tile. Viewers zoom out much faster with them. Existing levels are rebuilt from the full resolution."),
        h("label", { class: "field" }, "Compression"), h("div", {}, compression),
        hint,
        qualityLabel, qualityInput,
        h("label", { class: "field" }, "Tile size"),
        h("select", { onchange: (e) => (t.tile_size = Number(e.target.value)) }, [128, 256, 512, 1024].map((n) => h("option", { value: n, selected: n === t.tile_size }, `${n} × ${n} px`))),
        h("label", { class: "field" }, "BigTIFF"),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: t.bigtiff, onchange: (e) => (t.bigtiff = e.target.checked) }), "Allow files larger than 4 GB"),
        h("div", { class: "hint" }, "Keep this on for whole slide images; slides do not need to be that small to exceed 4 GB."),
        h("label", { class: "field" }, "Skip"),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: t.skip_pyramidal, onchange: (e) => (t.skip_pyramidal = e.target.checked) }), "Slides that already have a pyramid"),
        h("div", { class: "hint" }, "Handy when you only want to repair the slides that miss levels. Do not use it to compress everything."),
      ),
    ],
    footer: [h("button", { onclick: () => go(0) }, "Back"), h("span", { class: "grow" }), h("button", { class: "primary", onclick: next }, "Next")],
  };
}

// --------------------------------------------------------------------------- step 3: where to save

function renderSaving() {
  const o = { ...server.output };
  const previewBox = h("div", { class: "table-wrap" });
  const modeBox = h("div", { class: "radios" });
  let previewTimer = null;

  const text = (key, props = {}) => h("input", { type: "text", value: o[key], spellcheck: false, ...props, oninput: (e) => (o[key] = e.target.value, queuePreview()) });

  function option(mode, title, description, extra) {
    return h("label", { class: "radio" + (o.mode === mode ? " on" : "") },
      h("input", { type: "radio", name: "mode", checked: o.mode === mode, onchange: () => (o.mode = mode, build(), queuePreview()) }),
      h("div", { class: "body" }, h("strong", {}, title), h("span", { class: "faint" }, description), o.mode === mode && extra));
  }

  function build() {
    modeBox.replaceChildren(
      option("overwrite", "Replace the original files", "Each file is overwritten once its new version is complete. The originals are gone afterwards.",
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: o.keep_if_larger, onchange: (e) => (o.keep_if_larger = e.target.checked) }), "Keep the original when the new file would not be smaller")),
      option("beside", "Save next to the originals", "The new file goes into the same folder, with a prefix and/or suffix added to its name.",
        h("div", { class: "row" }, "Prefix", text("prefix", { size: 12 }), "Suffix", text("suffix", { size: 12 }), h("span", { class: "faint" }, "slide.svs → ", h("code", {}, `${o.prefix}slide${o.suffix}.svs`)))),
      option("folder", "Save in a separate folder", "All new files go into one folder. Slides added as a folder keep their subfolders.",
        h("div", { class: "row" }, text("folder", { class: "wide", placeholder: "/path/to/the/output/folder" }))),
    );
    const extra = o.mode === "overwrite" ? null : h("label", { class: "check" }, h("input", { type: "checkbox", checked: o.replace_existing, onchange: (e) => (o.replace_existing = e.target.checked, queuePreview()) }), "Replace files that already exist at the new location (otherwise they are skipped)");
    modeBox.append(...(extra ? [extra] : []));
  }

  function queuePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(showPreview, 400);
  }

  async function showPreview() {
    try {
      await api("PUT", "/plan", { json: { output: o } });
      const { rows } = await api("GET", "/preview");
      const writes = rows.filter((r) => r.action === "write").length;
      previewBox.replaceChildren(h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Source"), h("th", {}, "Result"))),
        h("tbody", {}, rows.slice(0, 300).map((r) => h("tr", {},
          h("td", { class: "wrap" }, r.source),
          h("td", { class: "wrap " + (r.action === "skip" ? "warn" : "") }, r.action === "skip" ? `Skipped: ${r.note}` : o.mode === "overwrite" ? "Replaced" : r.target, r.action !== "skip" && r.note && h("span", { class: "faint" }, ` (${r.note})`)))))),
        rows.length > 300 && h("div", { class: "empty" }, `…and ${rows.length - 300} more`));
      summary.textContent = `${writes} of ${rows.length} files will be written`;
      apply.disabled = writes === 0;
      notice = null;
    } catch (e) {
      previewBox.replaceChildren(h("div", { class: "empty warn" }, message(e)));
      summary.textContent = "";
      apply.disabled = true;
    }
  }

  const summary = h("span", { class: "faint" });
  const apply = h("button", { class: "primary", onclick: confirmAndRun }, "Apply");

  async function confirmAndRun() {
    if (o.mode === "overwrite") {
      const el = h("div", { class: "backdrop" }, h("div", { class: "dialog" },
        h("header", {}, "Replace the original files?"),
        h("div", { class: "content" }, h("p", {}, "The slides are overwritten with their new versions and the originals cannot be restored. Make sure you have a copy if the originals matter.")),
        h("footer", {}, h("button", { onclick: closeDialog }, "Cancel"), h("button", { class: "primary danger", onclick: () => (dialog = null, run()) }, "Replace the originals"))));
      openDialog(el);
    } else run();
  }

  async function run() {
    if (await guard(async () => {
      await api("PUT", "/plan", { json: { output: o } });
      await api("POST", "/run");
    })) {
      await refresh();
      go(3);
    }
  }

  build();
  queuePreview();
  return {
    page: [
      h("h2", {}, "Where should the results go?"),
      modeBox,
      h("div", { class: "toolbar" }, h("strong", {}, "Preview"), summary),
      previewBox,
    ],
    footer: [h("button", { onclick: async () => { await api("PUT", "/plan", { json: { output: o } }).catch(() => undefined); await refresh(); go(1); } }, "Back"), h("span", { class: "grow" }), apply],
  };
}

// --------------------------------------------------------------------------- step 4: the run

function renderProgress() {
  const bar = h("div", { class: "bar" }, h("div"));
  const headline = h("div", { class: "big" });
  const cards = h("div", { class: "cards" });
  const current = h("div", { class: "faint" });
  const tbody = h("tbody");
  const tableWrap = h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "File"), h("th", { class: "num" }, "Before"), h("th", { class: "num" }, "After"), h("th", {}, "State"))), tbody));
  const stop = h("button", { class: "danger", onclick: () => api("POST", "/run/abort").then(refresh).then(update) }, "Stop");
  const again = h("button", { onclick: async () => { if (await guard(() => api("POST", "/run/reset"))) { await refresh(); go(2); } } }, "Back to the settings");
  const newRun = h("button", { class: "primary", onclick: async () => { if (await guard(async () => { await api("POST", "/run/reset"); await api("POST", "/items/clear"); })) { selected.clear(); await refresh(); go(0); } } }, "Start over with new files");

  function update() {
    const run = server.run;
    const items = server.items;
    const running = run.state === "running";
    const totalSize = items.filter((i) => i.status !== "skipped").reduce((s, i) => s + i.size, 0) || 1;
    const doneSize = items.reduce((s, i) => s + (i.status === "done" ? i.size : i.status === "running" ? i.size * i.percent / 100 : 0), 0);
    const fraction = run.state === "finished" ? 1 : Math.min(1, doneSize / totalSize);
    bar.firstChild.style.width = `${(fraction * 100).toFixed(1)}%`;
    bar.classList.toggle("done", run.state === "finished");
    const counts = { done: 0, skipped: 0, failed: 0, aborted: 0 };
    for (const i of items) if (i.status in counts) counts[i.status]++;
    const elapsed = ((run.finished ?? Date.now() / 1000) - run.started) || 0;
    headline.textContent = running ? `${Math.round(fraction * 100)} %` : run.state === "finished" ? "Done" : run.state === "aborted" ? "Stopped" : "";
    const eta = running && fraction > 0.02 ? ` · about ${formatDuration(elapsed / fraction - elapsed)} left` : "";
    const active = items.find((i) => i.id === run.current);
    current.textContent = running ? (active ? `Working on ${active.rel} (${Math.round(active.percent)} %)${eta}` : "Starting…") : `Took ${formatDuration(elapsed)}`;
    const before = items.filter((i) => i.status === "done").reduce((s, i) => s + i.size, 0);
    const after = items.filter((i) => i.status === "done").reduce((s, i) => s + (i.out_size ?? 0), 0);
    cards.replaceChildren(
      card("Written", counts.done), counts.skipped ? card("Skipped", counts.skipped) : null,
      counts.failed ? card("Failed", counts.failed, "error") : null, counts.aborted ? card("Not done", counts.aborted) : null,
      counts.done ? card("Size", `${formatBytes(before)} → ${formatBytes(after)}`) : null,
      counts.done && before ? card("Saved", `${Math.round((1 - after / before) * 100)} %`) : null,
      card("Elapsed", formatDuration(elapsed)));
    stop.hidden = !running;
    again.hidden = newRun.hidden = running;
    tbody.replaceChildren(...items.map((i) => {
      const state = {
        pending: h("span", { class: "faint" }, "Waiting"),
        running: h("span", {}, h("span", { class: "bar small" }, h("div", { style: `width:${i.percent}%` })), ` ${Math.round(i.percent)} %`),
        done: h("span", { class: "ok" }, "Done" + (i.out_path && i.out_path !== i.path ? ` → ${i.out_path.split("/").pop()}` : "")),
        skipped: h("span", { class: "warn", title: i.message }, `Skipped: ${i.message}`),
        failed: h("span", { class: "error", title: i.message }, `Failed: ${i.message}`),
        aborted: h("span", { class: "faint" }, i.message || "Stopped"),
      }[i.status];
      return h("tr", {}, h("td", { class: "name", title: i.path }, i.rel), h("td", { class: "num" }, formatBytes(i.size)), h("td", { class: "num" }, i.status === "done" ? formatBytes(i.out_size) : ""), h("td", {}, state));
    }));
  }

  const card = (label, value, cls = "") => h("div", { class: "card" }, h("div", { class: "label" }, label), h("div", { class: cls }, String(value)));

  update();
  return {
    live: update,
    page: [h("h2", {}, "Progress"), headline, bar, current, cards, tableWrap],
    footer: [stop, again, h("span", { class: "grow" }), newRun],
  };
}

// --------------------------------------------------------------------------- start

(async () => {
  await refresh();
  if (server && server.run.state !== "idle") step = 3; // a run is going on (or just ended): show it
  else if (server && server.items.length) step = 0;
  render();
  schedulePoll();
})();
