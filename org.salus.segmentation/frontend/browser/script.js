// The dataset browser (left panel): chooses the project and lists its images. It is the plugin's entry component and
// opens the other three: the viewer (center), the tools (bottom) and the classes (right).
import { api, dirName, fileName, followState, h, message, modal, onEvent, salus, setState } from "../shared/common.js";
import { pickFolder } from "../shared/picker.js";

const app = document.getElementById("app");
let images = []; // [{path, shapes}]
let filter = "";
const statusFilter = new Set(); // ids of the statuses shown; empty shows all
let menu = null; // the open menu: "projects" | "more" | "context" (right-click on images)
// Images chosen with Ctrl/Cmd+click, Shift+click or Ctrl/Cmd+A. With two or more, the status buttons move to the bottom and
// apply to all of them. `anchor` is where a Shift+click range starts.
const selected = new Set();
let anchor = null;
let loadedFor = null; // project id the image list belongs to
let error = "";

const shared = followState(async (state) => {
  // A project newly shown here is rescanned: images may have been added while the backend kept the list.
  if (state.project && loadedFor !== state.project.id) await loadImages(true, state.project.id);
  if (!state.project) (images = []), (loadedFor = null);
  render(state);
});

onEvent((event) => {
  if (event.type === "status") {
    const paths = new Set(event.images);
    for (const image of images) if (paths.has(image.path)) image.status = event.status;
    render(shared.get());
  } else if (event.type === "annotations") {
    const image = images.find((i) => i.path === event.image);
    if (image) (image.shapes = event.shapes), render(shared.get());
  } else if (event.type === "images") {
    loadImages(false).then(() => render(shared.get())); // images removed from or put back into the project
  }
});

// The status of an image; images whose status was removed have the first (default) one.
const statusOf = (project, image) => project.statuses.find((s) => s.id === image.status) ?? project.statuses[0];

// Gives the images (paths) the status.
async function setStatus(paths, status) {
  const set = new Set(paths);
  for (const image of images) if (set.has(image.path)) image.status = status; // at once; the event confirms it
  render(shared.get());
  await guard(() => api("PUT", "/images/status", { json: { images: paths, status } }));
}

// A click on an image: plain opens it (and ends a selection), Ctrl/Cmd adds or removes it, Shift selects the range from
// the last clicked one. `shown` is the list as filtered.
function clickImage(event, path, shown) {
  const current = shared.get()?.image;
  if (event.shiftKey) {
    const from = shown.findIndex((i) => i.path === (anchor ?? current));
    const to = shown.findIndex((i) => i.path === path);
    if (!(event.ctrlKey || event.metaKey)) selected.clear();
    const [a, b] = from < 0 ? [to, to] : [Math.min(from, to), Math.max(from, to)];
    for (const image of shown.slice(a, b + 1)) selected.add(image.path);
    document.getSelection()?.removeAllRanges(); // Shift+click selects text otherwise
  } else if (event.ctrlKey || event.metaKey) {
    if (!selected.size && current && current !== path) selected.add(current); // the open image is where a selection starts
    selected.has(path) ? selected.delete(path) : selected.add(path);
    anchor = path;
  } else {
    selected.clear();
    anchor = path;
    openImage(path);
    return;
  }
  render(shared.get());
}

function selectAll() {
  for (const element of app.querySelectorAll(".list .image[data-path]")) selected.add(element.dataset.path);
  render(shared.get());
}

function clearSelection() {
  if (!selected.size) return;
  selected.clear();
  render(shared.get());
}

document.addEventListener("keydown", (event) => {
  const typing = event.target.closest?.("input, textarea, select, [contenteditable]");
  if (typing || document.querySelector(".backdrop") || !shared.get()?.project) return;
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
    event.preventDefault();
    selectAll();
  } else if (event.key === "Escape" && !menu) clearSelection();
});

async function loadImages(rescan, projectId = shared.get()?.project?.id) {
  try {
    images = (await api("GET", "/images", { query: { rescan: rescan ? "1" : undefined } })).images;
    loadedFor = projectId;
    error = "";
  } catch (e) {
    error = message(e);
  }
}

// The other components of the workspace. `reuse` focuses them if they are open already.
async function openWorkspace() {
  for (const name of ["viewer", "tools", "classes"]) {
    try {
      await salus.openComponent(name, { reuse: true });
    } catch (e) {
      console.error(`could not open ${name}`, e);
    }
  }
}

function render(state) {
  if (!state) return;
  const project = state.project;
  const projectMenu = menu === "projects" && h("div", { class: "menu projects", role: "listbox", onkeydown: menuKeys },
    state.projects.length
      ? state.projects.map((p) => h("button", {
        class: "item" + (project?.id === p.id ? " on" : ""), role: "option", disabled: p.missing, title: p.save,
        onclick: () => {
          closeMenu();
          if (project?.id !== p.id) guard(() => api("POST", `/projects/${p.id}/use`));
        },
      }, h("span", { class: "check" }, project?.id === p.id ? "✓" : ""),
        h("span", { class: "label" }, h("span", { class: "title" }, p.name), h("span", { class: "sub" }, p.missing ? "Save folder missing" : p.save))))
      : h("div", { class: "none" }, "No projects yet. Create one with +, or open one with the folder button."));
  const moreMenu = menu === "more" && h("div", { class: "menu more", role: "menu", onkeydown: menuKeys },
    h("button", { class: "item", role: "menuitem", title: "Where the project's images and annotations are", onclick: () => (closeMenu(), infoDialog(project)) },
      h("span", { class: "label" }, h("span", { class: "title" }, "Project information…"))),
    h("button", { class: "item", role: "menuitem", title: "Import annotations exported from QuPath (GeoJSON)", onclick: () => (closeMenu(), importDialog(state)) },
      h("span", { class: "label" }, h("span", { class: "title" }, "Import from QuPath…"))),
    h("button", { class: "item", role: "menuitem", title: "Put images removed from the project back", onclick: () => (closeMenu(), removedDialog()) },
      h("span", { class: "label" }, h("span", { class: "title" }, "Removed images…"))));
  // The right-click menu of the images in the list (the selection, or the image clicked).
  const contextMenu = menu === "context" && h("div", { class: "menu context", role: "menu", onkeydown: menuKeys, style: `left:${context.x}px;top:${context.y}px` },
    h("button", { class: "item", role: "menuitem", onclick: () => (closeMenu(), removeDialog(context.paths)) },
      h("span", { class: "label" }, h("span", { class: "title" }, context.paths.length === 1 ? "Remove from project…" : `Remove ${context.paths.length} images from project…`))));

  const head = h("div", { class: "project" },
    h("div", { class: "line" },
      h("div", { class: "anchor grow" },
        h("button", {
          class: "picker" + (menu === "projects" ? " open" : ""), "aria-haspopup": "listbox", "aria-expanded": menu === "projects" ? "true" : "false",
          title: project ? `${project.name}\n${project.save}` : "Choose a project",
          onclick: () => toggleMenu("projects"),
          onkeydown: (e) => e.key === "ArrowDown" && (e.preventDefault(), menu !== "projects" && toggleMenu("projects")),
        }, h("span", { class: "name" + (project ? "" : " faint") }, project ? project.name : state.projects.length ? "Choose a project…" : "No projects yet"), icon("expand")),
        projectMenu),
      h("button", { class: "icon-button", title: "New annotation project", onclick: newProjectDialog }, icon("add")),
      h("button", { class: "icon-button", title: "Open a project from its save folder", onclick: openProjectDialog }, icon("folder"))),
    project && h("div", { class: "line" },
      h("button", { class: "grow", title: "Export the annotations as pixel maps (pyramidal TIFF)", onclick: () => salus.openComponent("export", { reuse: true }).catch((e) => console.error(e)) }, "Export"),
      h("button", { class: "grow", title: "Edit the statuses images can have", onclick: () => statusesDialog(project) }, "Statuses"),
      h("div", { class: "anchor" },
        h("button", { class: "icon-button" + (menu === "more" ? " open" : ""), title: "More", "aria-haspopup": "menu", onclick: () => toggleMenu("more") }, icon("more")),
        moreMenu)),
    project?.root_missing && h("div", { class: "warn" }, "The image folder does not exist (any more)."));

  if (!project) {
    app.replaceChildren(head, h("div", { class: "welcome" },
      h("p", { class: "faint" }, "An annotation project lists the images of a folder and keeps their annotations in a separate save folder. Create one, or open one from its save folder."),
      h("button", { class: "primary", onclick: newProjectDialog }, "New project…"),
      error && h("div", { class: "error" }, error)));
    focusMenu();
    return;
  }

  for (const id of statusFilter) if (!project.statuses.some((s) => s.id === id)) statusFilter.delete(id); // removed statuses
  const needle = filter.trim().toLowerCase();
  const shown = images.filter((i) => (!needle || i.path.toLowerCase().includes(needle)) && (!statusFilter.size || statusFilter.has(statusOf(project, i).id)));
  const search = h("input", { type: "search", placeholder: "Filter images…", value: filter, oninput: (e) => { filter = e.target.value; render(shared.get()); } });
  // Only images in the list can be selected: those the filters hide leave the selection.
  const visible = new Set(shown.map((i) => i.path));
  for (const path of selected) if (!visible.has(path)) selected.delete(path);
  // The bar at the bottom takes over the status buttons for a selection (one image counts if it is not the open one).
  const multi = selected.size > 1 || (selected.size === 1 && !selected.has(state.image));
  const list = h("div", { class: "list" },
    shown.map((image) => {
      const status = statusOf(project, image);
      const current = image.path === state.image;
      return h("div", {
        class: "image" + (current ? " current" : "") + (selected.has(image.path) ? " selected" : ""),
        title: image.path,
        "data-path": image.path,
        onclick: (e) => clickImage(e, image.path, shown),
        oncontextmenu: (e) => openContextMenu(e, image.path, state),
      },
        h("div", { class: "text" },
          h("div", { class: "name" }, fileName(image.path)),
          dirName(image.path) && h("div", { class: "dir" }, dirName(image.path)),
          current && !multi && h("div", { class: "statuses" }, statusButtons(project, [image]))),
        h("span", { class: "dot", style: `background:${status.color}`, title: `${status.name} · ${image.shapes} shape${image.shapes === 1 ? "" : "s"}` }));
    }),
    !shown.length && h("div", { class: "empty" }, images.length ? "No image matches." : `No ${project.extensions.join(" / ")} files in the image folder.`),
    error && h("div", { class: "empty error" }, error));
  const tally = project.statuses.map((s) => ({ ...s, count: images.filter((i) => statusOf(project, i).id === s.id).length }));
  const counts = tally.map((s) => `${s.count} ${s.name.toLowerCase()}`);
  // One segment per status, as wide as its share of the images.
  const bar = images.length > 0 && h("div", { class: "status-bar" }, tally.filter((s) => s.count).map((s) =>
    h("span", { style: `flex:${s.count};background:${s.color}`, title: `${s.name}: ${s.count} (${Math.round((100 * s.count) / images.length)} %)` })));

  // Status filter: none chosen shows every image; otherwise the images with one of the chosen statuses.
  const chips = h("div", { class: "filters" }, tally.map((s) => h("button", {
    class: "chip" + (statusFilter.has(s.id) ? " on" : ""), style: `--status:${s.color}`,
    title: statusFilter.has(s.id) ? `Stop showing only “${s.name}”` : `Show the images that are “${s.name}”`,
    onclick: () => { statusFilter.has(s.id) ? statusFilter.delete(s.id) : statusFilter.add(s.id); render(shared.get()); },
  }, h("span", { class: "swatch" }), s.name, h("span", { class: "count" }, String(s.count)))),
  statusFilter.size > 0 && h("button", { class: "chip clear", title: "Show all statuses", onclick: () => { statusFilter.clear(); render(shared.get()); } }, "All"));
  const filtered = shown.length !== images.length;

  const focused = document.activeElement === app.querySelector(".search input");
  const scroll = app.querySelector(".list")?.scrollTop ?? 0;
  app.replaceChildren(head,
    h("div", { class: "search" },
      h("div", { class: "line" }, search, h("button", { title: "Look for new or removed images", onclick: async () => { await loadImages(true); render(shared.get()); } }, "↻")),
      chips),
    list,
    // app.replaceChildren, unlike h(), would show `false` as text
    multi ? h("div", { class: "selection" },
      h("div", { class: "line" }, h("strong", {}, `${selected.size} image${selected.size === 1 ? "" : "s"} selected`), h("span", { class: "grow" }),
        h("button", { class: "clear", title: "End the selection (Esc)", onclick: clearSelection }, "Clear")),
      h("div", { class: "statuses" }, statusButtons(project, images.filter((i) => selected.has(i.path))))) : "",
    h("div", { class: "footer" }, bar, `${images.length} images: ${counts.join(", ")}`, filtered && h("span", { class: "shown" }, ` · ${shown.length} shown`)),
    contextMenu || "");
  if (contextMenu) { // keep it inside the panel
    const box = contextMenu.getBoundingClientRect();
    contextMenu.style.left = `${Math.max(4, Math.min(context.x, innerWidth - box.width - 4))}px`;
    contextMenu.style.top = `${Math.max(4, Math.min(context.y, innerHeight - box.height - 4))}px`;
  }
  list.scrollTop = scroll;
  if (focused) {
    search.focus();
    search.setSelectionRange(search.value.length, search.value.length);
  }
  focusMenu();
}

// One button per status for the images: the status they all have is filled; a click gives it to every one of them.
function statusButtons(project, chosen) {
  const ids = new Set(chosen.map((i) => statusOf(project, i).id));
  const one = chosen.length === 1;
  return project.statuses.map((s) => {
    const all = ids.size === 1 && ids.has(s.id);
    const count = chosen.filter((i) => statusOf(project, i).id === s.id).length;
    return h("button", {
      class: "status" + (all ? " on" : count ? " some" : ""),
      style: `--status:${s.color}`,
      title: all ? `Status: ${s.name}` : one ? `Set the status to ${s.name}` : `Set the status of all ${chosen.length} to ${s.name} (${count} have it)`,
      onclick: (e) => (e.stopPropagation(), !all && setStatus(chosen.map((i) => i.path), s.id)),
    }, s.name);
  });
}

// --------------------------------------------------------------------------- the project picker and the "more" menu

// Material Symbols (weight 200, 960 units), the set the Dataset Workbench uses; filled with the text color.
const ICONS = {
  add: "M460-460H240v-40h220v-220h40v220h220v40H500v220h-40v-220Z",
  folder: "M184.62-200q-27.62 0-46.12-18.5Q120-237 120-264.62v-430.76q0-27.62 18.5-46.12Q157-760 184.62-760h199.23l80 80h311.53q27.62 0 46.12 18.5Q840-643 840-615.38v350.76q0 27.62-18.5 46.12Q803-200 775.38-200H184.62Zm0-40h590.76q10.77 0 17.7-6.92 6.92-6.93 6.92-17.7v-350.76q0-10.77-6.92-17.7-6.93-6.92-17.7-6.92H447.77l-80-80H184.62q-10.77 0-17.7 6.92-6.92 6.93-6.92 17.7v430.76q0 10.77 6.92 17.7 6.93 6.92 17.7 6.92ZM160-240v-480 480Z",
  more: "M190-480a50 50 0 1 0 100 0a50 50 0 1 0-100 0Zm240 0a50 50 0 1 0 100 0a50 50 0 1 0-100 0Zm240 0a50 50 0 1 0 100 0a50 50 0 1 0-100 0Z",
  expand: "M480-371.69 267.69-584l28.31-28.31 184 184 184-184L692.31-584 480-371.69Z",
};
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 -960 960 960");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

let wantFocus = false; // move the focus into the menu after the next render

function toggleMenu(which) {
  menu = menu === which ? null : which;
  wantFocus = !!menu;
  render(shared.get());
}

function closeMenu() {
  if (!menu) return;
  const which = menu;
  menu = null;
  render(shared.get());
  if (which !== "context") app.querySelector(which === "projects" ? ".picker" : ".anchor .icon-button[aria-haspopup=menu]")?.focus();
}

// Right-click on an image: the menu acts on the selection if the image is in it, otherwise on the image alone (which
// becomes the selection).
let context = null; // {x, y, paths}
function openContextMenu(event, path, state) {
  event.preventDefault();
  if (!selected.has(path)) {
    selected.clear();
    if (path !== state.image) selected.add(path);
    anchor = path;
  }
  const paths = selected.size ? [...selected] : [path];
  context = { x: event.clientX, y: event.clientY, paths };
  menu = "context";
  wantFocus = true;
  render(shared.get());
}

function focusMenu() {
  if (!wantFocus) return;
  wantFocus = false;
  const items = [...app.querySelectorAll(".menu .item:not(:disabled)")];
  (items.find((b) => b.classList.contains("on")) ?? items[0])?.focus();
}

// Arrow keys move through the items, Esc closes.
function menuKeys(event) {
  const items = [...event.currentTarget.querySelectorAll(".item:not(:disabled)")];
  const at = items.indexOf(document.activeElement);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    items[(at + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length]?.focus();
  } else if (event.key === "Escape") closeMenu();
}

// A click anywhere outside the open menu (and its button) closes it.
document.addEventListener("pointerdown", (event) => {
  if (menu && !event.target.closest(".anchor, .menu")) closeMenu();
});
document.addEventListener("keydown", (event) => event.key === "Escape" && closeMenu());

async function openImage(path) {
  await guard(() => setState({ image: path }));
  openWorkspace();
}

async function guard(action) {
  try {
    return await action();
  } catch (e) {
    error = message(e);
    render(shared.get());
    return undefined;
  }
}

function folderField(label, input, hint) {
  return h("label", { class: "field" }, h("span", {}, label),
    h("div", { class: "with-button" }, input, h("button", { type: "button", onclick: async () => {
      const chosen = await pickFolder(`Choose the ${label.toLowerCase()}`, input.value.trim() || undefined);
      if (chosen) input.value = chosen;
    } }, "Browse…")), hint && h("span", {}, hint));
}

function newProjectDialog() {
  const name = h("input", { type: "text", placeholder: "e.g. Tumor segmentation" });
  const root = h("input", { type: "text", class: "mono", placeholder: "/path/to/the/images", spellcheck: false });
  const extensions = h("input", { type: "text", class: "mono", value: ".svs", spellcheck: false });
  const save = h("input", { type: "text", class: "mono", placeholder: "/path/to/the/annotations", spellcheck: false });
  const problem = h("div", { class: "error" });
  const create = h("button", { class: "primary" }, "Create");
  create.addEventListener("click", async () => {
    create.disabled = true;
    try {
      await api("POST", "/projects", { json: { name: name.value, root: root.value.trim(), extensions: extensions.value, save: save.value.trim() } });
      close();
      openWorkspace();
    } catch (e) {
      problem.textContent = message(e);
      create.disabled = false;
    }
  });
  const close = modal("New annotation project", [
    h("label", { class: "field" }, h("span", {}, "Name"), name),
    folderField("Image folder", root, "All images below this folder (including subfolders) belong to the project. They are only read."),
    h("label", { class: "field" }, h("span", {}, "File types"), extensions, h("span", {}, "Separated by commas, e.g. .svs, .tif")),
    folderField("Save folder", save, "The project file and all annotations are written here. Choose an empty or new folder."),
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), create]);
  name.focus();
}

function openProjectDialog() {
  const save = h("input", { type: "text", class: "mono", placeholder: "/path/to/the/save/folder", spellcheck: false });
  const problem = h("div", { class: "error" });
  const open = h("button", { class: "primary" }, "Open");
  open.addEventListener("click", async () => {
    try {
      await api("POST", "/projects/open", { json: { save: save.value.trim() } });
      close();
      openWorkspace();
    } catch (e) {
      problem.textContent = message(e);
    }
  });
  const close = modal("Open a project", [folderField("Save folder", save, "The folder that contains the project's project.json."), problem],
    [h("button", { onclick: () => close() }, "Cancel"), open]);
}

// --------------------------------------------------------------------------- removing images from the project

// Removing only takes images out of the project: the files and their annotations stay, and "Removed images…" in the ⋯
// menu puts them back.
function removeDialog(paths) {
  const problem = h("div", { class: "error" });
  const go = h("button", { class: "primary" }, "Remove from project");
  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      await api("POST", "/images/remove", { json: { images: paths } });
      close();
      for (const path of paths) selected.delete(path);
      render(shared.get());
    } catch (e) {
      problem.textContent = message(e);
      go.disabled = false;
    }
  });
  const what = paths.length === 1 ? `“${fileName(paths[0])}”` : `${paths.length} images`;
  const close = modal(paths.length === 1 ? "Remove the image from the project?" : `Remove ${paths.length} images from the project?`, [
    h("p", {}, `${what} will no longer be listed, imported into or exported. Nothing is deleted: the image file${paths.length === 1 ? " stays" : "s stay"} in the image folder, and ${paths.length === 1 ? "its annotations stay" : "their annotations stay"} in the save folder.`),
    paths.length > 1 && h("ul", { class: "files" }, paths.map((p) => h("li", {}, p))),
    h("p", { class: "faint" }, "Put images back with ⋯ › Removed images…"),
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), go]);
}

async function removedDialog() {
  let removed;
  try {
    removed = (await api("GET", "/images/removed")).images;
  } catch (e) {
    error = message(e);
    return render(shared.get());
  }
  const chosen = new Set();
  let needle = "";
  const problem = h("div", { class: "error" });
  const back = h("button", { class: "primary", disabled: true }, "Put back");
  // "Select all" acts on the images the filter shows; it is half checked when only some of them are chosen.
  const allBox = h("input", { type: "checkbox", onchange: (e) => {
    for (const row of rows) if (!row.el.hidden) (e.target.checked ? chosen.add(row.path) : chosen.delete(row.path));
    update();
  } });
  const allLabel = h("span", {});
  const update = () => {
    for (const row of rows) row.box.checked = chosen.has(row.path);
    const visible = rows.filter((r) => !r.el.hidden);
    const on = visible.filter((r) => chosen.has(r.path)).length;
    allBox.checked = visible.length > 0 && on === visible.length;
    allBox.indeterminate = on > 0 && on < visible.length;
    allBox.disabled = !visible.length;
    allLabel.textContent = needle ? `Select all ${visible.length} shown` : `Select all ${rows.length}`;
    back.disabled = !chosen.size;
    back.textContent = chosen.size > 1 ? `Put back ${chosen.size} images` : "Put back";
  };
  back.addEventListener("click", async () => {
    back.disabled = true;
    try {
      await api("POST", "/images/restore", { json: { images: [...chosen] } });
      close();
    } catch (e) {
      problem.textContent = message(e);
      update();
    }
  });
  const rows = removed.map((path) => {
    const box = h("input", { type: "checkbox", onchange: (e) => (e.target.checked ? chosen.add(path) : chosen.delete(path), update()) });
    return { path, box, el: h("label", { class: "check-row" }, box, path) };
  });
  const search = removed.length > 8 && h("input", { type: "search", placeholder: "Filter…", oninput: (e) => {
    needle = e.target.value.trim().toLowerCase();
    for (const row of rows) row.el.hidden = !!needle && !row.path.toLowerCase().includes(needle);
    update();
  } });
  update();
  // h() flattens one level of arrays only, so the parts are spread in, not nested.
  const close = modal("Removed images", [
    ...(removed.length
      ? [h("p", { class: "faint" }, "These images are in the image folder but not in the project. Put back the ones that should be listed again; their annotations are still there."),
        search,
        h("div", { class: "removed" },
          h("label", { class: "check-row all" }, allBox, allLabel),
          rows.map((r) => r.el))]
      : [h("p", { class: "faint" }, "No images have been removed from this project.")]),
    problem,
  ], [h("button", { onclick: () => close() }, removed.length ? "Cancel" : "Close"), removed.length > 0 && back]);
}

// --------------------------------------------------------------------------- project information

function infoDialog(project) {
  const row = (label, value, copyable) => h("div", { class: "info-row" },
    h("span", { class: "info-label" }, label),
    h("div", { class: "info-value" }, h("span", { class: copyable ? "mono" : "" }, value),
      copyable && h("button", { class: "copy", title: "Copy", onclick: (e) => {
        navigator.clipboard?.writeText(value).then(() => (e.target.textContent = "Copied"), () => undefined);
      } }, "Copy")));
  const close = modal(project.name, [
    h("div", { class: "info" },
      row("Image folder", project.root, true),
      row("Save folder", project.save, true),
      row("File types", project.extensions.join(", ")),
      row("Images", String(images.length)),
      row("Classes", String(project.classes.length))),
    project.root_missing && h("div", { class: "warn" }, "The image folder does not exist (any more)."),
    h("p", { class: "faint" }, "The images are only read. The project file and the annotations are written to the save folder."),
  ], [h("button", { class: "primary", onclick: () => close() }, "Close")]);
}

// --------------------------------------------------------------------------- editing the statuses

// The whole list is edited here and saved at once: names, colors, order (the first is what images start with), new and
// removed statuses. Images whose status is removed get the first one.
function statusesDialog(project) {
  let rows = project.statuses.map((s) => ({ ...s }));
  const list = h("div", { class: "status-rows" });
  const problem = h("div", { class: "error" });
  const move = (i, by) => {
    [rows[i], rows[i + by]] = [rows[i + by], rows[i]];
    draw();
  };
  const draw = () => list.replaceChildren(...rows.map((row, i) => h("div", { class: "status-row" },
    h("input", { type: "color", value: row.color, title: "Color", oninput: (e) => (row.color = e.target.value) }),
    h("input", { type: "text", value: row.name, placeholder: "Name", oninput: (e) => (row.name = e.target.value) }),
    h("button", { title: "Move up", disabled: i === 0, onclick: () => move(i, -1) }, "↑"),
    h("button", { title: "Move down", disabled: i === rows.length - 1, onclick: () => move(i, 1) }, "↓"),
    h("button", { title: "Remove", disabled: rows.length === 1, onclick: () => ((rows = rows.filter((r) => r !== row)), draw()) }, "×"))));
  draw();
  const add = h("button", { onclick: () => {
    rows.push({ name: "", color: "#4363d8" });
    draw();
    list.querySelector(".status-row:last-child input[type=text]").focus();
  } }, "+ Add status");
  const save = h("button", { class: "primary" }, "Save");
  save.addEventListener("click", async () => {
    try {
      await api("PUT", "/statuses", { json: { statuses: rows } });
      close();
    } catch (e) {
      problem.textContent = message(e);
    }
  });
  const close = modal("Statuses", [
    h("p", { class: "faint" }, "Every image has one of these statuses; the first is what images start with. Images whose status is removed get the first one."),
    list, h("div", {}, add), problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), save]);
}

// --------------------------------------------------------------------------- importing QuPath annotations

// Step 1 chooses the source: one GeoJSON file for the open image, or a folder whose files are matched to images by name.
// Step 2 shows what was found and maps every QuPath class to a project class (or leaves it out). The QuPath files are only read.
function importDialog(state) {
  let kind = state.image ? "file" : "folder";
  const path = h("input", { type: "text", class: "mono", placeholder: "/path/to/qupath/export", spellcheck: false });
  const problem = h("div", { class: "error" });
  const kinds = h("div", { class: "choices" });
  const drawKinds = () => kinds.replaceChildren(
    choice("kind", kind === "file", !state.image, () => ((kind = "file"), drawKinds()),
      "One file for the open image", state.image ? fileName(state.image) : "Open an image first"),
    choice("kind", kind === "folder", false, () => ((kind = "folder"), drawKinds()),
      "A folder of QuPath exports", "Each file is matched to the image with the same name, e.g. slide1.geojson to slide1.svs (subfolders count)."));
  drawKinds();
  const next = h("button", { class: "primary" }, "Next");
  next.addEventListener("click", async () => {
    next.disabled = true;
    const source = { path: path.value.trim(), image: kind === "file" ? state.image : undefined };
    try {
      const scan = await api("POST", "/import/scan", { json: source });
      close();
      mappingDialog(source, scan);
    } catch (e) {
      problem.textContent = message(e);
      next.disabled = false;
    }
  });
  const browse = h("button", { type: "button", onclick: async () => {
    const chosen = kind === "file"
      ? await pickFolder("Choose the QuPath GeoJSON file", path.value.trim() || undefined, { files: [".geojson", ".json"] })
      : await pickFolder("Choose the folder with the QuPath exports", path.value.trim() || undefined);
    if (chosen) path.value = chosen;
  } }, "Browse…");
  const close = modal("Import from QuPath", [
    h("p", { class: "faint" }, "Annotations exported from QuPath (File › Export objects as GeoJSON). The QuPath files are only read; the imported shapes become annotations of this project."),
    kinds,
    h("label", { class: "field" }, h("span", {}, "File or folder"), h("div", { class: "with-button" }, path, browse)),
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), next]);
}

function choice(name, checked, disabled, onchange, title, detail) {
  return h("label", { class: "choice" + (disabled ? " disabled" : "") },
    h("input", { type: "radio", name, checked, disabled, onchange }),
    h("span", {}, h("strong", {}, title), detail && h("span", { class: "detail" }, detail)));
}

function mappingDialog(source, scan) {
  const classes = shared.get()?.project?.classes ?? [];
  const mapping = {};
  const existing = scan.files.filter((f) => f.existing > 0);
  let mode = existing.length ? null : "merge";
  const problem = h("div", { class: "error" });
  const go = h("button", { class: "primary" }, "Import");
  const update = () => (go.disabled = !mode || !Object.values(mapping).some((v) => v !== null));

  const rows = scan.classes.map(({ name, count }) => {
    const same = classes.find((c) => c.name.toLowerCase() === name.toLowerCase());
    mapping[name] = same ? same.id : null;
    const select = h("select", { onchange: (e) => ((mapping[name] = e.target.value ? Number(e.target.value) : null), update()) },
      h("option", { value: "" }, "Don't import"),
      classes.map((c) => h("option", { value: c.id, selected: same?.id === c.id }, c.name)));
    return h("div", { class: "map-row" },
      h("div", { class: "map-name" }, name ? name : h("em", { class: "faint" }, "Unclassified"), h("span", { class: "faint" }, ` · ${count} shape${count === 1 ? "" : "s"}`)),
      h("div", { class: "map-target" }, h("span", { class: "faint" }, "→"), select));
  });

  const modeChoices = existing.length && h("div", { class: "choices" },
    h("div", {}, h("strong", {}, `${existing.length} of the images already ${existing.length === 1 ? "has" : "have"} annotations. What should happen to them?`)),
    choice("mode", false, false, () => ((mode = "merge"), update()), "Add the imported shapes", "The existing annotations stay."),
    choice("mode", false, false, () => ((mode = "replace"), update()), "Replace the existing annotations", "Images that get imported shapes lose their current annotations (undo brings them back)."));

  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      const result = await api("POST", "/import/apply", { json: { ...source, mapping, mode } });
      close();
      modal("Import finished", h("p", {}, `${result.shapes} shape${result.shapes === 1 ? "" : "s"} imported into ${result.files} image${result.files === 1 ? "" : "s"}.`),
        [h("button", { class: "primary", onclick: (e) => e.target.closest(".backdrop").remove() }, "OK")]);
    } catch (e) {
      problem.textContent = message(e);
      update();
    }
  });

  const shapes = scan.files.reduce((n, f) => n + f.shapes, 0);
  const close = modal("Match the QuPath classes", [
    h("div", {}, `${scan.files.length} file${scan.files.length === 1 ? "" : "s"} with ${shapes} annotation${shapes === 1 ? "" : "s"} for ${scan.files.length} image${scan.files.length === 1 ? "" : "s"}.`,
      scan.skipped ? h("span", { class: "faint" }, ` ${scan.skipped} other objects (detections, points, lines) are left out.`) : ""),
    scan.files.length > 1 && h("details", {}, h("summary", {}, "Files and images"),
      h("ul", { class: "files" }, scan.files.map((f) => h("li", {}, `${fileName(f.file)} → ${f.image}`, h("span", { class: "faint" }, ` (${f.shapes})`))))),
    scan.left_out.length > 0 && h("details", {}, h("summary", { class: "warn" }, `${scan.left_out.length} file${scan.left_out.length === 1 ? " is" : "s are"} left out`),
      h("ul", { class: "files" }, scan.left_out.map((f) => h("li", {}, fileName(f.file), h("span", { class: "faint" }, ` (${f.reason})`))))),
    scan.classes.length
      ? h("div", { class: "mapping" }, h("div", { class: "map-head" }, "QuPath class → class in this project"), rows)
      : h("p", { class: "warn" }, "The files contain no annotations that can be imported."),
    h("p", { class: "faint" }, "Classes set to “Don't import” are left out. A class that is missing here can be added in the class editor; then start the import again."),
    modeChoices,
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), go]);
  update();
}

openWorkspace();
