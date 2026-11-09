// The class editor (right panel): the segmentation classes of the project, each with its number of shapes in the open
// image and a button that hides its shapes in the viewer. Click a class to draw with it. The arrow in front of a class
// lists its shapes in the open image, each with a small picture of its outline and its name (generic, "Tumor 3", until
// it is renamed); clicking one shows it in the middle of the viewer.
// At the bottom: "Hide annotations", held down to see the tissue under the shapes (also the H key). How strongly the
// shapes are filled is a setting of the plugin (Settings, "Annotation fill").
// Each shape can have comments, so that users can discuss it: the row shows the first 50 characters of the latest one,
// and picking a shape that has comments opens the whole conversation with a box to write in (Ctrl/Cmd+Enter sends); the
// speech bubble opens and closes it (the way to start a conversation). The arrow beside "Comment" resolves the
// conversation: it stays, but picking the shape no longer opens it (the bubble still does; a new comment reopens it).
// The author is the system user running the backend; one's own comments can be edited, or deleted after typing "delete".
// Right-clicking a shape offers to give it another class or to delete it.
// Name and drawing color can be changed; classes are never deleted. Colors of exported pixel maps are chosen in the export view.
import { api, followState, h, message, modal, onEvent, setHideHeld, setState, watchHideKey } from "../shared/common.js";

const app = document.getElementById("app");
let editing = null; // id of the class being renamed
let error = "";
let shapes = null; // the shapes of the open image: [{id, class_id, name, polygons, bbox}]; null without one
let shapesFor = null; // the image `shapes` belong to
let loadToken = 0;
const expanded = new Set(); // ids of the classes whose shapes are listed
let naming = null; // id of the shape being renamed
let picked = null; // id of the shape last shown in the viewer
let comments = {}; // shape id -> its entries, oldest first: comments {id, author, time, text, edited?} and resolve and
                   // reopen entries {id, kind: "resolved" | "reopened", author, time}
let resolved = {}; // shape id -> its resolve entry, for the resolved conversations
const isEvent = (c) => c.kind === "resolved" || c.kind === "reopened";
const said = (id) => (comments[id] ?? []).filter((c) => !isEvent(c)); // the comments alone
let opened = null; // id of the shape whose conversation was opened with its speech bubble
let closed = null; // id of the picked shape whose conversation was closed with its speech bubble
let sendMenu = null; // id of the shape whose "Comment" arrow menu is open
let me = ""; // the user comments are written as
const drafts = new Map(); // shape id -> the comment being written
let composeFocus = null; // id of the shape whose comment box gets the focus at the next render
let sending = false;
let editingComment = null; // {id, text} of the own comment being edited

const shared = followState((state) => {
  if (state?.image !== shapesFor) loadShapes(state?.image);
  render(state);
});

onEvent((event) => {
  if (event.type === "annotations" && event.image === shapesFor) loadShapes(shapesFor);
  if (event.type === "comments" && event.image === shapesFor) loadComments(shapesFor);
  if (event.type === "focus" && event.image === shapesFor && picked !== (event.id ?? null)) {
    picked = event.id ?? null; // let go in the viewer (a click beside the shape, or drawing)
    render(shared.get());
  }
});

async function loadShapes(image) {
  const token = ++loadToken;
  if (image !== shapesFor) (shapes = null), (picked = naming = null), (comments = {}), (resolved = {}), (opened = closed = sendMenu = null), drafts.clear();
  shapesFor = image ?? null;
  if (!image) return;
  loadComments(image);
  try {
    const data = await api("GET", "/annotations", { query: { image } });
    if (token !== loadToken) return;
    shapes = data.features.map(toShape).filter(Boolean);
    render(shared.get());
  } catch {
    // the viewer shows the problem
  }
}

async function loadComments(image) {
  try {
    const data = await api("GET", "/comments", { query: { image } });
    if (image !== shapesFor) return;
    comments = data.shapes ?? {};
    resolved = data.resolved ?? {};
    me = data.me ?? "";
    render(shared.get());
  } catch {
    // the viewer shows the problem
  }
}

// Sends the comment being written; `resolve` then resolves the conversation (true), or opens it again (false).
async function sendComment(shape, resolve) {
  const text = (drafts.get(shape.id) ?? "").trim();
  sendMenu = null;
  if ((!text && resolve === undefined) || sending) return render(shared.get());
  sending = true;
  const image = shapesFor;
  try {
    if (text) {
      const comment = await api("POST", "/comments", { json: { image, shape: shape.id, text } });
      if (image === shapesFor) (comments[shape.id] ??= []).push(comment), delete resolved[shape.id]; // at once; the event reloads them
      drafts.delete(shape.id);
    }
    if (resolve !== undefined && (resolve || resolved[shape.id])) {
      const { entry } = await api("POST", "/comments/resolve", { json: { image, shape: shape.id, resolved: resolve } });
      if (image === shapesFor && entry) {
        (comments[shape.id] ??= []).push(entry);
        resolve ? (resolved[shape.id] = entry) : delete resolved[shape.id];
      }
    }
    if (resolve) (opened = null), (closed = shape.id); // resolved: put the conversation away
    else composeFocus = shape.id;
    error = "";
  } catch (e) {
    error = message(e);
  }
  sending = false;
  render(shared.get());
}

const when = (time) => {
  const date = new Date(time);
  return isNaN(date) ? "" : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};
const preview = (text, length = 50) => {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > length ? flat.slice(0, length).trimEnd() + "…" : flat;
};

// Whether a shape's conversation is shown: picking opens it if it has comments and is not resolved; the bubble overrides.
const threadOpen = (s) => opened === s.id || (picked === s.id && closed !== s.id && said(s.id).length > 0 && !resolved[s.id]);

function toggleThread(s) {
  if (threadOpen(s)) (opened = null), (closed = s.id);
  else {
    if (picked !== s.id) pick(s);
    (opened = s.id), (closed = null), (composeFocus = s.id);
  }
  render(shared.get());
}

// The conversation on a shape and the box to write in.
function thread(shape) {
  const entries = comments[shape.id] ?? [];
  const list = said(shape.id);
  const done = resolved[shape.id];
  const box = h("textarea", {
    class: "compose", rows: 2, placeholder: list.length ? "Reply…" : "Write a comment…", "data-shape": shape.id, value: drafts.get(shape.id) ?? "",
    oninput: (e) => { drafts.set(shape.id, e.target.value); send.disabled = !e.target.value.trim(); more.disabled = !list.length && !done && send.disabled; },
    onkeydown: (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendComment(shape); } if (e.key === "Escape") e.target.blur(); },
  });
  const hasText = () => !!(drafts.get(shape.id) ?? "").trim();
  const send = h("button", { class: "primary", disabled: !hasText(), title: "Ctrl/Cmd+Enter", onclick: () => sendComment(shape) }, "Comment");
  const more = h("button", { class: "primary more", title: "More", "aria-haspopup": "menu", "aria-expanded": sendMenu === shape.id ? "true" : "false",
    disabled: !list.length && !done && !hasText(), onclick: () => { sendMenu = sendMenu === shape.id ? null : shape.id; render(shared.get()); } }, chevron());
  const options = sendMenu === shape.id && h("div", { class: "menu send-menu", role: "menu" },
    done
      ? h("button", { class: "item", role: "menuitem", onclick: () => sendComment(shape, false) }, reopen(), h("span", { class: "label" }, hasText() ? "Comment and reopen" : "Reopen"))
      : h("button", { class: "item", role: "menuitem", disabled: !list.length && !hasText(), onclick: () => sendComment(shape, true) },
        check(), h("span", { class: "label" }, hasText() ? "Comment and resolve" : "Resolve")));
  return h("div", { class: "thread", onclick: (e) => e.stopPropagation() },
    entries.map((c) => {
      if (isEvent(c)) { // resolving and reopening, among the comments as they happened
        return h("div", { class: "comment event " + c.kind },
          h("div", { class: "meta" }, c.kind === "resolved" ? check() : reopen(), h("strong", {}, c.author || "?"),
            h("span", {}, c.kind === "resolved" ? "resolved this" : "reopened this"), h("span", { class: "faint", title: when(c.time) }, when(c.time))));
      }
      const mine = c.author === me;
      const editing = mine && editingComment?.id === c.id;
      return h("div", { class: "comment" + (mine ? " mine" : "") },
        h("div", { class: "meta" }, h("strong", {}, c.author || "?"),
          h("span", { class: "faint", title: c.edited ? `edited ${when(c.edited)}` : "" }, when(c.time) + (c.edited ? " · edited" : "")),
          mine && !editing && h("span", { class: "own" },
            h("button", { class: "mini", title: "Edit your comment", onclick: () => { editingComment = { id: c.id, text: c.text }; composeFocus = shape.id; render(shared.get()); } }, pen()),
            h("button", { class: "mini danger", title: "Delete your comment…", onclick: () => deleteCommentDialog(shape, c) }, trash()))),
        editing ? editBox(shape, c) : h("div", { class: "text" }, c.text));
    }),
    h("div", { class: "composer" }, box,
      h("div", { class: "row" }, me && h("span", { class: "faint" }, `as ${me}`), h("span", { class: "split" }, send, more, options))));
}

// Editing one's own comment in place: Ctrl/Cmd+Enter saves, Escape gives up.
function editBox(shape, c) {
  const box = h("textarea", { class: "compose edit-comment", rows: 3, value: editingComment.text, "data-shape": shape.id,
    oninput: (e) => { editingComment.text = e.target.value; save.disabled = !e.target.value.trim(); },
    onkeydown: (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveComment(shape, c); }
      if (e.key === "Escape") { e.stopPropagation(); editingComment = null; render(shared.get()); }
    } });
  const save = h("button", { class: "primary", title: "Ctrl/Cmd+Enter", onclick: () => saveComment(shape, c) }, "Save");
  return h("div", { class: "composer" }, box,
    h("div", { class: "row" }, h("button", { onclick: () => { editingComment = null; render(shared.get()); } }, "Cancel"), save));
}

async function saveComment(shape, c) {
  const text = editingComment?.text.trim();
  if (!text) return;
  try {
    const updated = await api("PATCH", "/comments", { json: { image: shapesFor, shape: shape.id, id: c.id, text } });
    Object.assign(c, updated); // at once; the event reloads them
    editingComment = null;
    error = "";
  } catch (e) {
    error = message(e);
  }
  render(shared.get());
}

// Comments have no undo, so deleting one is confirmed by typing "delete".
function deleteCommentDialog(shape, c) {
  const image = shapesFor;
  const word = h("input", { type: "text", placeholder: "delete", autocomplete: "off", spellcheck: false });
  const problem = h("div", { class: "error" });
  const go = h("button", { class: "primary danger", disabled: true }, "Delete comment");
  word.addEventListener("input", () => (go.disabled = word.value.trim().toLowerCase() !== "delete"));
  word.addEventListener("keydown", (e) => e.key === "Enter" && !go.disabled && go.click());
  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      await api("POST", "/comments/delete", { json: { image, shape: shape.id, id: c.id } });
      if (image === shapesFor) comments[shape.id] = (comments[shape.id] ?? []).filter((x) => x.id !== c.id);
      close();
      render(shared.get());
    } catch (e) {
      problem.textContent = message(e);
      go.disabled = false;
    }
  });
  const close = modal("Delete your comment?", [
    h("blockquote", { class: "quote" }, preview(c.text, 200)),
    h("p", {}, "The comment is deleted for everyone and cannot be brought back. Type ", h("b", {}, "delete"), " to confirm."),
    word, problem,
  ], [h("button", { onclick: () => close() }, "Cancel"), go]);
  word.focus();
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
  if (picked !== shape.id) opened = closed = sendMenu = null;
  picked = shape.id;
  render(shared.get());
  loadComments(shapesFor); // other users' backends do not tell this one about their comments
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
document.addEventListener("pointerdown", (e) => {
  if (sendMenu && !e.target.closest(".send-menu, .split .more")) (sendMenu = null), render(shared.get());
});
document.addEventListener("keydown", (e) => e.key === "Escape" && (sendMenu ? ((sendMenu = null), render(shared.get())) : closeMenu()));

// The menu: "Change class" (which lists the classes in the menu itself; the panel is too narrow for a submenu beside
// it) and "Delete annotation…".
function menuItems(project) {
  const { shape, label } = menu;
  if (menu.view === "class") {
    return [
      h("div", { class: "menu-title" }, h("button", { class: "item back", title: "Back", onclick: () => { menu.view = null; render(shared.get()); } }, chevron()),
        h("span", {}, `Class of “${label}”`)),
      ...project.classes.map((c) => h("button", {
        class: "item" + (c.id === shape.class_id ? " current" : ""), role: "menuitemradio", "aria-checked": c.id === shape.class_id ? "true" : "false",
        disabled: c.id === shape.class_id, title: c.id === shape.class_id ? "Its class now" : `Make it ${c.name}`,
        onclick: () => { closeMenu(); changeClass(shape, c); },
      }, h("span", { class: "chip", style: `background:${c.color}` }), h("span", { class: "label" }, c.name), c.id === shape.class_id && h("span", { class: "tick" }, "✓"))),
    ];
  }
  return [
    h("button", { class: "item", role: "menuitem", disabled: project.classes.length < 2, title: project.classes.length < 2 ? "Add another class first" : "Give it another class",
      onclick: () => { menu.view = "class"; render(shared.get()); } },
      swap(), h("span", { class: "label" }, "Change class"), h("span", { class: "more" }, chevron())),
    h("button", { class: "item danger", role: "menuitem", onclick: () => { closeMenu(); deleteDialog(shape, label); } }, trash(), "Delete annotation…"),
  ];
}

// Arrow keys move between the items of the menu.
function menuKeys(e) {
  if (e.key === "Escape") return closeMenu();
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  e.preventDefault();
  const items = [...e.currentTarget.querySelectorAll("button.item:not(:disabled)")];
  const at = items.indexOf(document.activeElement);
  items[(at + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length]?.focus();
}

// The shape keeps its outline and name; it is not merged with shapes of the new class. Ctrl/Cmd+Z undoes it.
async function changeClass(shape, c) {
  try {
    await api("POST", "/annotations/class", { json: { image: shapesFor, id: shape.id, class_id: c.id } });
    shape.class_id = c.id; // at once; the reload confirms it
    expanded.add(c.id); // so that it can be seen where it went
    error = "";
  } catch (e) {
    error = message(e);
  }
  render(shared.get());
}

// Material Symbols "chat_bubble" (weight 200, 24 units): comments.
function bubble() {
  return icon24("M3,20.08V4.62Q3,3.92 3.46,3.46T4.62,3H19.38q0.69,0 1.15,0.46T21,4.62V15.38q0,0.69-0.46,1.15T19.38,17H6.08L3,20.08ZM5.65,16H19.38q0.23,0 0.42-0.19T20,15.38V4.62q0-0.23-0.19-0.42T19.38,4H4.62Q4.38,4 4.19,4.19T4,4.62V17.64L5.65,16ZM4,16V4.62Q4,4.38 4,4.19T4,4Q4,4 4,4.19T4,4.62V15.38q0,0.23 0,0.42T4,16Z", "bubble");
}
// Material Symbols "edit" (weight 200, 24 units): editing a comment, renaming a class or a shape.
function pen() {
  return icon24("M5,19H6.1L16.8,8.3L15.7,7.2L5,17.9V19ZM4,20V17.48L17.18,4.29q0.15-0.14 0.34-0.21T17.91,4T18.3,4.06t0.35,0.23l1.07,1.07q0.17,0.16 0.23,0.35T20,6.1q0,0.2-0.07,0.39T19.71,6.83L6.52,20H4ZM19.02,6.09L17.91,4.98l1.11,1.11ZM16.24,7.76L15.7,7.2l1.1,1.1L16.24,7.76Z", "pen");
}

// Material Symbols "check" (weight 200, 24 units): resolving.
function check() {
  return icon24("M9.55,17.31L4.58,12.34l0.71-0.71l4.26,4.26l9.16-9.16l0.71,0.71L9.55,17.31Z", "check");
}
// Material Symbols "rotate_left" (weight 200, 24 units): reopening.
function reopen() {
  return icon24("M11,21q-1.08-0.16-2.06-0.57T7.09,19.37L7.8,18.65q0.72,0.53 1.54,0.85T11,19.95V21Zm2,0V19.99q2.6-0.41 4.3-2.38T19,13.05q0-2.92-2.04-4.96T12,6.05H11.22l2.1,2.1L12.62,8.86L9.31,5.55l3.31-3.31l0.71,0.72L11.23,5.05H12q1.66,0 3.12,0.63t2.54,1.71t1.71,2.54T20,13.05q0,3.04-2,5.3T13,21ZM5.68,17.96Q5.02,17.08 4.6,16.09T4.05,14.05H5.06q0.14,0.87 0.48,1.67T6.4,17.25L5.68,17.96ZM4.05,12.05Q4.2,10.93 4.62,9.94T5.68,8.14L6.4,8.85Q5.88,9.57 5.54,10.38T5.06,12.05H4.05Z", "reopen");
}
function icon24(d, name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("class", "icon " + name);
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", d);
  svg.append(path);
  return svg;
}

// Material Symbols "swap_horiz" (weight 200).
const SWAP = "M290-220 160-350l130-130 28.31 28.31L236.62-370H560v40H236.62l81.69 81.69L290-220Zm380-260-28.31-28.31L723.38-590H400v-40h323.38l-81.69-81.69L670-740l130 130-130 130Z";
function swap() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 -960 960 960");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", SWAP);
  svg.append(path);
  return svg;
}

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
      h("button", { class: "edit", title: "Rename", onclick: (e) => (e.stopPropagation(), startRename(c)) }, pen()),
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
      const talk = said(s.id);
      const last = talk[talk.length - 1];
      const row = h("div", {
        class: "shape" + (picked === s.id ? " picked" : "") + (menu?.shape === s ? " menu-open" : ""), title: "Show in the viewer", onclick: () => pick(s),
        oncontextmenu: (e) => { e.preventDefault(); menu = { x: e.clientX, y: e.clientY, shape: s, label: s.name || generic }; render(shared.get()); },
      },
        thumbnail(s, c.color),
        h("div", { class: "text-col" }, label,
          last && !threadOpen(s) && (resolved[s.id]
            ? h("span", { class: "said resolved", title: `${last.author}: ${last.text}` }, check(), `Resolved by ${resolved[s.id].author}`)
            : h("span", { class: "said", title: `${last.author}: ${last.text}` }, h("b", {}, last.author + ": "), preview(last.text)))),
        h("button", { class: "edit", title: "Rename (an empty name gives back the generic one)", onclick: (e) => { e.stopPropagation(); naming = s.id; render(shared.get()); } }, pen()),
        h("button", {
          class: "talk" + (talk.length ? " has" : "") + (resolved[s.id] ? " resolved" : "") + (threadOpen(s) ? " open" : ""),
          title: threadOpen(s) ? "Hide the conversation" : talk.length ? `${talk.length} comment${talk.length === 1 ? "" : "s"}${resolved[s.id] ? " (resolved)" : ""}; show the conversation` : "Comment on this shape",
          onclick: (e) => { e.stopPropagation(); toggleThread(s); },
        }, bubble(), talk.length ? String(talk.length) : ""));
      return threadOpen(s) ? [row, thread(s)] : row;
    });
    return [row, h("div", { class: "shapes" + (hidden ? " hidden" : "") }, items.flat())];
  });
  const scroll = app.querySelector(".list")?.scrollTop ?? 0;
  const typing = document.activeElement?.matches?.("textarea.compose")
    ? { id: document.activeElement.dataset.shape, edit: document.activeElement.classList.contains("edit-comment"),
      start: document.activeElement.selectionStart, end: document.activeElement.selectionEnd } : null;
  if (menu && !shapes?.includes(menu.shape)) menu = null; // the shapes were reloaded
  const contextMenu = menu && h("div", { class: "menu", role: "menu", style: `left:${menu.x}px;top:${menu.y}px`, onkeydown: menuKeys }, menuItems(project));
  const list = h("div", { class: "list" }, rows.flat(), !rows.length && h("div", { class: "empty" }, "No classes yet. Add one to start drawing."));
  app.replaceChildren(
    h("div", { class: "head" }, h("strong", {}, "Classes"), h("button", { onclick: () => addDialog(project) }, "+ Add")),
    list,
    error && h("div", { class: "empty error" }, error),
    h("div", { class: "foot" }, hideButton(), hideHint && h("span", { class: "hint" }, hideHint)),
    contextMenu || "");
  if (contextMenu) { // keep it inside the panel
    const box = contextMenu.getBoundingClientRect();
    contextMenu.style.left = `${Math.max(4, Math.min(menu.x, innerWidth - box.width - 4))}px`;
    contextMenu.style.top = `${Math.max(4, Math.min(menu.y, innerHeight - box.height - 4))}px`;
    contextMenu.querySelector("button.item:not(:disabled):not(.back)")?.focus();
  }
  list.scrollTop = scroll;
  const input = app.querySelector("input.rename");
  if (input) (input.focus(), input.select());
  else {
    const id = typing?.id ?? composeFocus;
    const boxes = id ? [...app.querySelectorAll("textarea.compose")].filter((t) => t.dataset.shape === id) : [];
    const box = boxes.find((t) => t.classList.contains("edit-comment") === !!(typing ? typing.edit : editingComment)) ?? boxes[0];
    if (box) {
      box.focus();
      if (typing) box.setSelectionRange(typing.start, typing.end);
      else (box.setSelectionRange(box.value.length, box.value.length), box.scrollIntoView({ block: "nearest" }));
    }
  }
  composeFocus = null;
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


// --------------------------------------------------------------------------- hold to hide

const HOLD_MS = 300; // a press shorter than this is a click: the annotations would only flicker, so say how it works
let pressedAt = null;
let hideHint = "";
let hintTimer = null;
watchHideKey();

function hideButton() {
  return h("button", {
    class: "hold" + (pressedAt !== null ? " on" : ""),
    title: "Hides the annotations, outlines included, while you hold it down (or hold H)",
    onpointerdown: (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      pressedAt = performance.now();
      event.currentTarget.classList.add("on");
      setHideHeld(true);
      // released anywhere (the panel may redraw meanwhile): listen on the window
      window.addEventListener("pointerup", releaseHide, { once: true });
      window.addEventListener("pointercancel", releaseHide, { once: true });
    },
  }, "Hide annotations");
}

function releaseHide() {
  if (pressedAt === null) return;
  const held = performance.now() - pressedAt;
  pressedAt = null;
  setHideHeld(false);
  window.removeEventListener("pointerup", releaseHide);
  window.removeEventListener("pointercancel", releaseHide);
  clearTimeout(hintTimer);
  hideHint = held < HOLD_MS ? "Hold the button down to keep the annotations hidden." : "";
  render(shared.get());
  if (hideHint) hintTimer = setTimeout(() => { hideHint = ""; render(shared.get()); }, 3000);
}
