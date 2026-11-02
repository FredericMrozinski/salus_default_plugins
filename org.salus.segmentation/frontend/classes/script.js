// The class editor (right panel): the segmentation classes of the project, each with its number of shapes in the open
// image and a button that hides its shapes in the viewer. Click a class to draw with it. The arrow in front of a class
// lists its shapes in the open image, each with a small picture of its outline and its name (generic, "Tumor 3", until
// it is renamed); clicking one shows it in the middle of the viewer.
// The fill opacity of the shapes is set at the bottom.
// Name and drawing color can be changed; classes are never deleted. Colors of exported pixel maps are chosen in the export view.
import { api, followState, h, message, modal, onEvent, setState } from "../shared/common.js";

const app = document.getElementById("app");
let editing = null; // id of the class being renamed
let error = "";
let shapes = null; // the shapes of the open image: [{id, class_id, name, polygons, bbox}]; null without one
let shapesFor = null; // the image `shapes` belong to
let loadToken = 0;
const expanded = new Set(); // ids of the classes whose shapes are listed
let naming = null; // id of the shape being renamed
let picked = null; // id of the shape last shown in the viewer

const shared = followState((state) => {
  if (state?.image !== shapesFor) loadShapes(state?.image);
  render(state);
});

onEvent((event) => {
  if (event.type === "annotations" && event.image === shapesFor) loadShapes(shapesFor);
  if (event.type === "focus" && event.image === shapesFor && picked !== (event.id ?? null)) {
    picked = event.id ?? null; // let go in the viewer (a click beside the shape, or drawing)
    render(shared.get());
  }
});

async function loadShapes(image) {
  const token = ++loadToken;
  if (image !== shapesFor) (shapes = null), (picked = naming = null);
  shapesFor = image ?? null;
  if (!image) return;
  try {
    const data = await api("GET", "/annotations", { query: { image } });
    if (token !== loadToken) return;
    shapes = data.features.map(toShape).filter(Boolean);
    render(shared.get());
  } catch {
    // the viewer shows the problem
  }
}

function toShape(feature) {
  const g = feature.geometry;
  const polygons = g?.type === "Polygon" ? [g.coordinates] : g?.type === "MultiPolygon" ? g.coordinates : null;
  if (!polygons) return null;
  const bbox = [Infinity, Infinity, -Infinity, -Infinity];
  for (const polygon of polygons) for (const [x, y] of polygon[0] ?? []) {
    bbox[0] = Math.min(bbox[0], x), bbox[1] = Math.min(bbox[1], y), bbox[2] = Math.max(bbox[2], x), bbox[3] = Math.max(bbox[3], y);
  }
  return { id: feature.id, class_id: feature.properties?.class_id, name: feature.properties?.name || "", polygons, bbox };
}

// A small picture of a shape's outline, filled with the class color. Long rings are thinned out; the picture is tiny.
function thumbnail(shape, color) {
  const [x0, y0, x1, y1] = shape.bbox;
  const size = Math.max(x1 - x0, y1 - y0, 1);
  const pad = size * 0.08;
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  const d = shape.polygons.flatMap((polygon) => polygon.map((ring) => {
    const step = Math.max(1, Math.floor(ring.length / 120));
    const points = ring.filter((_, i) => i % step === 0);
    return "M" + points.map(([x, y]) => `${+(x - cx).toFixed(1)} ${+(y - cy).toFixed(1)}`).join("L") + "Z";
  })).join("");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "thumb");
  svg.setAttribute("viewBox", `${-size / 2 - pad} ${-size / 2 - pad} ${size + 2 * pad} ${size + 2 * pad}`);
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", d);
  path.setAttribute("fill", color);
  path.setAttribute("fill-opacity", "0.55");
  path.setAttribute("fill-rule", "evenodd");
  path.setAttribute("stroke", color);
  path.setAttribute("stroke-width", "1.2");
  path.setAttribute("vector-effect", "non-scaling-stroke");
  svg.append(path);
  return svg;
}

// Shows the shape in the middle of the viewer (the viewer zooms to it and outlines it).
function pick(shape) {
  picked = shape.id;
  render(shared.get());
  api("POST", "/focus", { json: { image: shapesFor, bbox: shape.bbox, id: shape.id } }).catch(show);
}

async function renameShape(shape, value) {
  naming = null;
  if (value.trim() === shape.name) return render(shared.get());
  try {
    await api("POST", "/annotations/rename", { json: { image: shapesFor, id: shape.id, name: value } });
    shape.name = value.trim(); // at once; the reload confirms it
    error = "";
  } catch (e) {
    error = message(e);
  }
  render(shared.get());
}

// --------------------------------------------------------------------------- the right-click menu of a shape

let menu = null; // {x, y, shape, label} while the menu is open

function closeMenu() {
  if (!menu) return;
  menu = null;
  render(shared.get());
}

document.addEventListener("pointerdown", (e) => menu && !e.target.closest(".menu") && closeMenu());
document.addEventListener("keydown", (e) => e.key === "Escape" && closeMenu());

// Deleting one annotation is confirmed with a button (undo brings it back, too).
function deleteDialog(shape, label) {
  const image = shapesFor;
  const problem = h("div", { class: "error" });
  const go = h("button", { class: "primary danger" }, "Delete");
  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      await api("POST", "/annotations/delete", { json: { image, id: shape.id } });
      close();
      if (picked === shape.id) {
        picked = null;
        api("POST", "/focus", { json: { image, id: null } }).catch(() => undefined);
      }
    } catch (e) {
      problem.textContent = message(e);
      go.disabled = false;
    }
  });
  const close = modal(`Delete “${label}”?`, [
    h("p", {}, `The annotation “${label}” is deleted from this image. Ctrl/Cmd+Z in the viewer brings it back while the plugin runs.`),
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), go]);
  go.focus();
}

// Material Symbols "delete" (weight 200), the trash can of the Dataset Workbench.
const TRASH = "M304.62-160q-26.85 0-45.74-18.88Q240-197.77 240-224.62V-720h-40v-40h160v-30.77h240V-760h160v40h-40v495.38q0 27.62-18.5 46.12Q683-160 655.38-160H304.62ZM680-720H280v495.38q0 10.77 6.92 17.7 6.93 6.92 17.7 6.92h350.76q9.24 0 16.93-7.69 7.69-7.69 7.69-16.93V-720ZM392.31-280h40v-360h-40v360Zm135.38 0h40v-360h-40v360ZM280-720v520-520Z";
function trash() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 -960 960 960");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", TRASH);
  svg.append(path);
  return svg;
}

const CHEVRON ="M480-371.69 267.69-584l28.31-28.31 184 184 184-184L692.31-584 480-371.69Z"; // Material "expand_more"
function chevron() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 -960 960 960");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", CHEVRON);
  svg.append(path);
  return svg;
}

// Material Symbols "visibility" and "visibility_off" (weight 200, 24 units), filled with the text color.
const EYE = {
  shown: "M14.59,14.09q1.06-1.07 1.06-2.59T14.59,8.91T12,7.85T9.41,8.91T8.35,11.5t1.07,2.59T12,15.15t2.59-1.07Zm-4.5-0.67Q9.3,12.63 9.3,11.5t0.79-1.91T12,8.8t1.91,0.79q0.79,0.79 0.79,1.91t-0.79,1.91T12,14.2t-1.91-0.79ZM6.08,16.23q-2.67-1.77-4-4.73q1.33-2.96 4-4.73T12,5t5.92,1.77t4,4.73q-1.33,2.96-4,4.73T12,18T6.08,16.23ZM12,11.5Zm5.19,4.01q2.36-1.49 3.61-4.01q-1.25-2.53-3.61-4.01T12,6Q9.18,6 6.81,7.49T3.2,11.5q1.25,2.52 3.61,4.01T12,17t5.19-1.49Z",
  hidden: "M15.45,12.65l-0.8-0.8q0.22-1.31-0.74-2.27T11.65,8.85l-0.8-0.8q0.25-0.1 0.53-0.16T12,7.85q1.53,0 2.59,1.06t1.06,2.59q0,0.35-0.05,0.64t-0.16,0.51Zm3.16,3.07l-0.76-0.67q0.95-0.72 1.69-1.59T20.8,11.5q-1.25-2.53-3.59-4.01T12,6q-0.72,0-1.42,0.1T9.2,6.4L8.42,5.62q0.87-0.33 1.77-0.47T12,5q3.26,0 5.96,1.79t3.97,4.71q-0.54,1.21-1.36,2.27t-1.96,1.95Zm1.12,5.42l-3.89-3.88q-0.66,0.29-1.65,0.52T12,18q-3.28,0-5.96-1.79T2.08,11.5q0.58-1.33 1.54-2.48T5.65,7.08L2.86,4.28L3.57,3.57L20.43,20.43l-0.71,0.71ZM6.36,7.78Q5.5,8.4 4.6,9.39T3.2,11.5q1.25,2.52 3.59,4.01T12,17q0.87,0 1.74-0.17t1.32-0.34l-1.63-1.64q-0.24,0.13-0.66,0.22T12,15.15q-1.53,0-2.59-1.06T8.35,11.5q0-0.33 0.09-0.75t0.22-0.69L6.36,7.78Zm7.17,2.94Zm-2.81,1.41Z",
};
function eye(hidden) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", EYE[hidden ? "hidden" : "shown"]);
  svg.append(path);
  return svg;
}

function toggleHidden(state, c) {
  const hidden = state.hidden ?? [];
  setState({ hidden: hidden.includes(c.id) ? hidden.filter((i) => i !== c.id) : [...hidden, c.id] }).catch(show);
}

function render(state) {
  if (!state) return;
  const project = state.project;
  if (!project) {
    app.replaceChildren(h("div", { class: "empty" }, "Open a project in the image browser to see its classes."));
    return;
  }
  const rows = project.classes.map((c) => {
    const nameEl = editing === c.id
      ? h("input", { class: "rename", value: c.name, onclick: (e) => e.stopPropagation(),
        onkeydown: (e) => { if (e.key === "Enter") e.target.blur(); if (e.key === "Escape") { editing = null; render(shared.get()); } },
        onblur: (e) => rename(c, e.target.value) })
      : h("span", { class: "name", title: c.name, ondblclick: () => startRename(c) }, c.name);
    const hidden = (state.hidden ?? []).includes(c.id);
    const own = shapes?.filter((s) => s.class_id === c.id) ?? [];
    const count = own.length;
    const open = expanded.has(c.id) && count > 0;
    const row = h("div", { class: "cls" + (state.class_id === c.id ? " current" : "") + (hidden ? " hidden" : ""), onclick: () => setState({ class_id: c.id }).catch(show) },
      h("button", {
        class: "expand" + (open ? " open" : ""), disabled: !count, "aria-expanded": open ? "true" : "false",
        title: !shapes ? "Open an image to list its shapes" : count ? (open ? "Hide the shapes" : `List the ${count} shape${count === 1 ? "" : "s"} of ${c.name}`) : `No shapes of ${c.name} in this image`,
        onclick: (e) => { e.stopPropagation(); expanded.has(c.id) ? expanded.delete(c.id) : expanded.add(c.id); render(shared.get()); },
      }, chevron()),
      h("input", { type: "color", value: c.color, title: "Drawing color", onclick: (e) => e.stopPropagation(), onchange: (e) => update(c, { color: e.target.value }) }),
      nameEl,
      h("button", { class: "edit", title: "Rename", onclick: (e) => (e.stopPropagation(), startRename(c)) }, "✎"),
      shapes && h("span", { class: "count" + (count ? " has" : ""), title: `${count} shape${count === 1 ? "" : "s"} of ${c.name} in this image` }, String(count)),
      h("button", {
        class: "eye" + (hidden ? " off" : ""),
        title: hidden ? `Show ${c.name}` : `Hide ${c.name} (hidden shapes are not erased; drawing with the class shows it again)`,
        onclick: (e) => (e.stopPropagation(), toggleHidden(state, c)),
      }, eye(hidden)));
    if (!open) return row;
    // The shapes of the class: generic names number them in the order they were drawn.
    const items = own.map((s, i) => {
      const generic = `${c.name} ${i + 1}`;
      const label = naming === s.id
        ? h("input", { class: "rename", value: s.name, placeholder: generic, onclick: (e) => e.stopPropagation(),
          onkeydown: (e) => { if (e.key === "Enter") e.target.blur(); if (e.key === "Escape") { naming = null; render(shared.get()); } },
          onblur: (e) => naming === s.id && renameShape(s, e.target.value) })
        : h("span", { class: "name" + (s.name ? "" : " generic"), title: (s.name || generic) + " · double-click to rename", ondblclick: (e) => { e.stopPropagation(); naming = s.id; render(shared.get()); } }, s.name || generic);
      return h("div", {
        class: "shape" + (picked === s.id ? " picked" : "") + (menu?.shape === s ? " menu-open" : ""), title: "Show in the viewer", onclick: () => pick(s),
        oncontextmenu: (e) => { e.preventDefault(); menu = { x: e.clientX, y: e.clientY, shape: s, label: s.name || generic }; render(shared.get()); },
      },
        thumbnail(s, c.color), label,
        h("button", { class: "edit", title: "Rename (an empty name gives back the generic one)", onclick: (e) => { e.stopPropagation(); naming = s.id; render(shared.get()); } }, "✎"));
    });
    return [row, h("div", { class: "shapes" + (hidden ? " hidden" : "") }, items)];
  });
  const opacityValue = h("span", { class: "value" }, `${Math.round(state.opacity * 100)} %`);
  const scroll = app.querySelector(".list")?.scrollTop ?? 0;
  if (menu && !shapes?.includes(menu.shape)) menu = null; // the shapes were reloaded
  const contextMenu = menu && h("div", { class: "menu", role: "menu", style: `left:${menu.x}px;top:${menu.y}px`, onkeydown: (e) => e.key === "Escape" && closeMenu() },
    h("button", { class: "item danger", role: "menuitem", onclick: () => { const { shape, label } = menu; closeMenu(); deleteDialog(shape, label); } },
      trash(), "Delete annotation…"));
  const list = h("div", { class: "list" }, rows.flat(), !rows.length && h("div", { class: "empty" }, "No classes yet. Add one to start drawing."));
  app.replaceChildren(
    h("div", { class: "head" }, h("strong", {}, "Classes"), h("button", { onclick: () => addDialog(project) }, "+ Add")),
    list,
    error && h("div", { class: "empty error" }, error),
    h("div", { class: "foot", title: "How strongly shapes are filled" },
      h("span", { class: "label" }, "Fill"),
      h("input", { type: "range", min: 0, max: 100, value: Math.round(state.opacity * 100),
        oninput: (e) => (opacityValue.textContent = `${e.target.value} %`),
        onchange: (e) => setState({ opacity: Number(e.target.value) / 100 }).catch(show) }),
      opacityValue),
    contextMenu || "");
  if (contextMenu) { // keep it inside the panel
    const box = contextMenu.getBoundingClientRect();
    contextMenu.style.left = `${Math.max(4, Math.min(menu.x, innerWidth - box.width - 4))}px`;
    contextMenu.style.top = `${Math.max(4, Math.min(menu.y, innerHeight - box.height - 4))}px`;
    contextMenu.querySelector("button")?.focus();
  }
  list.scrollTop = scroll;
  const input = app.querySelector("input.rename");
  if (input) (input.focus(), input.select());
}

function show(e) {
  error = message(e);
  render(shared.get());
}

function startRename(c) {
  editing = c.id;
  render(shared.get());
}

async function rename(c, value) {
  editing = null;
  if (value.trim() && value.trim() !== c.name) await update(c, { name: value });
  else render(shared.get());
}

async function update(c, changes) {
  try {
    await api("PATCH", `/classes/${c.id}`, { json: changes });
    error = "";
  } catch (e) {
    show(e);
  }
}

// Spread-out default colors for new classes.
const PALETTE = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4", "#46f0f0", "#f032e6", "#bcf60c", "#fabebe", "#008080", "#9a6324"];

function addDialog(project) {
  const name = h("input", { type: "text", placeholder: "e.g. Tumor" });
  const color = h("input", { type: "color", value: PALETTE[project.classes.length % PALETTE.length] });
  const problem = h("div", { class: "error" });
  const add = h("button", { class: "primary" }, "Add class");
  add.addEventListener("click", async () => {
    try {
      const created = await api("POST", "/classes", { json: { name: name.value, color: color.value } });
      close();
      await setState({ class_id: created.id });
    } catch (e) {
      problem.textContent = message(e);
    }
  });
  name.addEventListener("keydown", (e) => e.key === "Enter" && add.click());
  const close = modal("New class", [
    h("label", { class: "field" }, h("span", {}, "Name"), name),
    h("label", { class: "field" }, h("span", {}, "Drawing color"), color),
    h("div", { class: "faint" }, "The drawing color is how the class looks while annotating. Which color it gets in exported pixel maps is chosen when exporting."),
    h("div", { class: "note" }, "Classes cannot be deleted."),
    problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), add]);
  name.focus();
}
