// The export view (center): turns the annotations into pixel maps, one pyramidal TIFF per image, plus classes.json that
// says which color is which class. The class colors, their order (which class wins where shapes overlap), the background,
// the folder and the resolution are kept in the project, so the next export starts from them.
import { api, dirName, fileName, followState, h, message } from "../shared/common.js";
import { pickFolder } from "../shared/picker.js";

const app = document.getElementById("app");
let projectId = null;
let settings = null; // {background, classes: [{id, name, drawing_color, color, include}], folder, downsample}
let images = []; // [{path, shapes}]
let chosen = new Set();
let filter = "";
let overwrite = false;
let job = null; // the backend's export status while an export runs or after it ended
let showJob = false;
let error = "";
let poll = null;

followState(async (state) => {
  if (!state.project) {
    projectId = null;
    app.replaceChildren(h("div", { class: "empty" }, "Open a project in the image browser to export its annotations."));
    return;
  }
  if (state.project.id !== projectId) {
    projectId = state.project.id;
    await load();
  } else if (settings) {
    // Classes may have been added or renamed: take them in, keeping what was chosen here.
    const fresh = await api("GET", "/export/settings").catch(() => null);
    if (fresh) merge(fresh);
  }
  render();
});

async function load() {
  try {
    const [s, list, status] = await Promise.all([api("GET", "/export/settings"), api("GET", "/images"), api("GET", "/export/status")]);
    settings = s;
    images = list.images;
    chosen = new Set(images.filter((i) => i.shapes > 0).map((i) => i.path));
    job = status.state === "idle" ? null : status;
    showJob = status.state === "running";
    if (showJob) watch();
    error = "";
  } catch (e) {
    error = message(e);
  }
}

function merge(fresh) {
  const mine = new Map(settings.classes.map((c) => [c.id, c]));
  const order = settings.classes.map((c) => c.id).filter((id) => fresh.classes.some((c) => c.id === id));
  for (const c of fresh.classes) if (!order.includes(c.id)) order.push(c.id);
  settings.classes = order.map((id) => {
    const f = fresh.classes.find((c) => c.id === id);
    const m = mine.get(id);
    return m ? { ...f, color: m.color, include: m.include } : f;
  });
}

function watch() {
  clearInterval(poll);
  poll = setInterval(async () => {
    try {
      job = await api("GET", "/export/status");
    } catch {
      return;
    }
    if (job.state !== "running") {
      clearInterval(poll);
      const list = await api("GET", "/images").catch(() => null);
      if (list) images = list.images;
    }
    render();
  }, 600);
}

// --------------------------------------------------------------------------- the settings

function render() {
  if (!settings) {
    app.replaceChildren(h("div", { class: "empty" + (error ? " error" : "") }, error || "Loading…"));
    return;
  }
  if (showJob && job) return renderJob();
  const active = document.activeElement?.dataset?.key;
  app.replaceChildren(h("div", { class: "page" }, classesSection(), imagesSection(), outputSection()), footer());
  if (active) {
    const again = app.querySelector(`[data-key="${active}"]`);
    if (again) {
      again.focus();
      if (again.setSelectionRange && again.type !== "color") again.setSelectionRange(again.value.length, again.value.length);
    }
  }
}

function colorField(value, key, onchange) {
  const picker = h("input", { type: "color", value, onchange: (e) => onchange(e.target.value) });
  const text = h("input", { class: "hex", value, "data-key": key, spellcheck: false,
    onchange: (e) => {
      const v = e.target.value.trim().toLowerCase();
      if (/^#[0-9a-f]{6}$/.test(v)) onchange(v);
      else (e.target.value = value);
    } });
  return [picker, text];
}

function classesSection() {
  const move = (i, by) => {
    const list = settings.classes;
    [list[i], list[i + by]] = [list[i + by], list[i]];
    render();
  };
  const rows = settings.classes.map((c, i) => h("div", { class: "row" + (c.include ? "" : " off") },
    h("span", { class: "prio" }, String(i + 1)),
    h("input", { type: "checkbox", checked: c.include, title: "Export this class", onchange: (e) => { c.include = e.target.checked; render(); } }),
    h("span", { class: "chip", style: `background:${c.drawing_color}`, title: "Drawing color" }),
    h("span", { class: "name", title: c.name }, c.name),
    colorField(c.color, `class-${c.id}`, (v) => { c.color = v; render(); }),
    h("button", { class: "move", title: "Higher priority", disabled: i === 0, onclick: () => move(i, -1) }, "↑"),
    h("button", { class: "move", title: "Lower priority", disabled: i === settings.classes.length - 1, onclick: () => move(i, 1) }, "↓")));
  return h("section", {},
    h("h2", {}, "Classes"),
    h("p", { class: "hint" }, "The color each class gets in the pixel maps. Where shapes overlap, the class higher in the list wins."),
    h("div", { class: "box" }, rows, !rows.length && h("div", { class: "empty" }, "The project has no classes yet."),
      h("div", { class: "row" }, h("span", { class: "prio" }), h("span", { class: "name" }, h("em", {}, "Background (no annotation)")),
        colorField(settings.background, "background", (v) => { settings.background = v; render(); }))));
}

function imagesSection() {
  const needle = filter.trim().toLowerCase();
  const shown = needle ? images.filter((i) => i.path.toLowerCase().includes(needle)) : images;
  const toggle = (path) => { chosen.has(path) ? chosen.delete(path) : chosen.add(path); render(); };
  const choose = (paths) => { chosen = new Set(paths); render(); };
  return h("section", {},
    h("h2", {}, "Images"),
    h("div", { class: "toolbar" },
      h("input", { type: "search", placeholder: "Filter…", value: filter, "data-key": "filter", oninput: (e) => { filter = e.target.value; render(); } }),
      h("button", { onclick: () => choose(images.filter((i) => i.shapes > 0).map((i) => i.path)) }, "Annotated"),
      h("button", { onclick: () => choose(images.map((i) => i.path)) }, "All"),
      h("button", { onclick: () => choose([]) }, "None")),
    h("div", { class: "box images" },
      shown.map((image) => h("div", { class: "row", onclick: (e) => e.target.type !== "checkbox" && toggle(image.path) },
        h("input", { type: "checkbox", checked: chosen.has(image.path), onchange: () => toggle(image.path) }),
        h("div", { class: "name", title: image.path }, fileName(image.path), dirName(image.path) && h("div", { class: "dir" }, dirName(image.path))),
        h("span", { class: "count" }, image.shapes ? `${image.shapes} shapes` : "no shapes"))),
      !shown.length && h("div", { class: "empty" }, images.length ? "No image matches." : "The project has no images.")),
    h("p", { class: "hint" }, `${chosen.size} of ${images.length} chosen. Images without shapes give maps that are all background.`));
}

function outputSection() {
  const folder = h("input", { type: "text", class: "mono", value: settings.folder, "data-key": "folder", placeholder: "/path/to/the/pixel/maps", spellcheck: false,
    oninput: (e) => (settings.folder = e.target.value) });
  return h("section", { class: "wide" },
    h("h2", {}, "Output"),
    h("div", { class: "options" },
      h("span", {}, "Folder"),
      h("div", { class: "with-button" }, folder, h("button", { onclick: async () => {
        const picked = await pickFolder("Choose the folder for the pixel maps", settings.folder || undefined);
        if (picked) { settings.folder = picked; render(); }
      } }, "Browse…")),
      h("span", {}, "Resolution"),
      h("div", {}, h("select", { onchange: (e) => (settings.downsample = Number(e.target.value)) },
        [1, 2, 4, 8, 16, 32].map((d) => h("option", { value: d, selected: settings.downsample === d }, d === 1 ? "Full resolution (like the image)" : `1/${d} of the full resolution`)))),
      h("span", {}),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: overwrite, onchange: (e) => (overwrite = e.target.checked) }), "Replace pixel maps that exist already (otherwise they are skipped)")),
    h("p", { class: "hint" }, "Each image becomes <folder>/<image path>.tif: a tiled, pyramidal TIFF with lossless compression, so the colors stay exact. classes.json in the folder lists which color is which class."));
}

function footer() {
  const go = h("button", { class: "primary", disabled: !chosen.size, onclick: start }, `Export ${chosen.size} image${chosen.size === 1 ? "" : "s"}`);
  return h("div", { class: "footer" },
    error && h("span", { class: "error" }, error),
    h("span", { class: "grow" }),
    job && h("button", { onclick: () => { showJob = true; render(); } }, "Last export"),
    go);
}

async function start() {
  try {
    job = await api("POST", "/export/start", { json: { settings, images: [...chosen], overwrite } });
    error = "";
    showJob = true;
    watch();
  } catch (e) {
    error = message(e);
  }
  render();
}

// --------------------------------------------------------------------------- the progress

function renderJob() {
  const items = job.items;
  const done = items.filter((i) => ["done", "skipped", "failed", "cancelled"].includes(i.status)).length;
  const running = job.state === "running";
  const fraction = items.length ? items.reduce((s, i) => s + (i.status === "running" ? i.percent / 100 : ["done", "skipped", "failed", "cancelled"].includes(i.status) ? 1 : 0), 0) / items.length : 1;
  const elapsed = ((job.finished ?? Date.now() / 1000) - job.started);
  const eta = running && fraction > 0.03 ? ` · about ${duration(elapsed / fraction - elapsed)} left` : "";
  const counts = (status) => items.filter((i) => i.status === status).length;
  const state = {
    pending: () => h("span", { class: "faint" }, "Waiting"),
    running: (i) => [h("span", { class: "small-bar" }, h("div", { style: `width:${i.percent}%` })), h("span", { class: "count" }, `${Math.round(i.percent)} %`)],
    done: (i) => h("span", { class: "ok" }, i.message || "Done"),
    skipped: (i) => h("span", { class: "warn" }, i.message),
    failed: (i) => h("span", { class: "error", title: i.message }, `Failed: ${i.message}`),
    cancelled: (i) => h("span", { class: "faint" }, i.message || "Stopped"),
  };
  app.replaceChildren(
    h("div", { class: "page" }, h("section", { class: "wide" },
      h("h2", {}, running ? "Exporting…" : job.state === "cancelled" ? "Export stopped" : "Export finished"),
      h("div", { class: "big" }, running ? `${Math.round(fraction * 100)} %` : `${counts("done")} written` + (counts("skipped") ? `, ${counts("skipped")} skipped` : "") + (counts("failed") ? `, ${counts("failed")} failed` : "")),
      h("div", { class: "bar" + (running ? "" : " done") }, h("div", { style: `width:${(fraction * 100).toFixed(1)}%` })),
      h("p", { class: "hint" }, `${done} of ${items.length} images · ${duration(elapsed)}${eta} · ${job.folder}`),
      h("div", { class: "box" }, items.map((i) => h("div", { class: "row" },
        h("div", { class: "name", title: i.image }, i.file), state[i.status](i)))))),
    h("div", { class: "footer" },
      running && h("button", { onclick: () => api("POST", "/export/cancel").catch(() => undefined) }, "Stop"),
      h("span", { class: "grow" }),
      !running && h("button", { class: "primary", onclick: () => { showJob = false; render(); } }, "Back to the settings")));
}

function duration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const m = Math.floor(seconds / 60), s = seconds % 60;
  return m ? `${m} min ${String(s).padStart(2, "0")} s` : `${s} s`;
}
