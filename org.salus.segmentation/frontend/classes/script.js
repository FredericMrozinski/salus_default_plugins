// The class editor (right panel): the segmentation classes of the project. Click a class to draw with it.
// Name and drawing color can be changed; classes are never deleted. Colors of exported pixel maps are chosen in the export view.
import { api, followState, h, message, modal, setState } from "../shared/common.js";

const app = document.getElementById("app");
let editing = null; // id of the class being renamed
let error = "";

const shared = followState(render);

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
    return h("div", { class: "cls" + (state.class_id === c.id ? " current" : ""), onclick: () => setState({ class_id: c.id }).catch(show) },
      h("input", { type: "color", value: c.color, title: "Drawing color", onclick: (e) => e.stopPropagation(), onchange: (e) => update(c, { color: e.target.value }) }),
      nameEl,
      h("button", { class: "edit", title: "Rename", onclick: (e) => (e.stopPropagation(), startRename(c)) }, "✎"));
  });
  app.replaceChildren(
    h("div", { class: "head" }, h("strong", {}, "Classes"), h("button", { onclick: () => addDialog(project) }, "+ Add")),
    h("div", { class: "list" }, rows, !rows.length && h("div", { class: "empty" }, "No classes yet. Add one to start drawing.")),
    error && h("div", { class: "empty error" }, error));
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
