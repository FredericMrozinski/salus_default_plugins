// The annotation tools (bottom panel): pan, polygon, eraser and their smart versions, the eraser size, the fill opacity, and undo/redo.
// The choice is shared state, so the viewer follows it.
import { api, followState, h, message, onEvent, setState } from "../shared/common.js";

const app = document.getElementById("app");
let history = { can_undo: false, can_redo: false };
let error = "";

// Simple stroke icons (24 x 24).
const ICONS = {
  pan: "M12 3v18M3 12h18M12 3l-3 3M12 3l3 3M12 21l-3-3M12 21l3-3M3 12l3-3M3 12l3 3M21 12l-3-3M21 12l-3 3",
  polygon: "M12 3.5l8 6-3 10H7l-3-10z",
  eraser: "M8.5 19.5L3.8 14.8a1.5 1.5 0 010-2.1l8.9-8.9a1.5 1.5 0 012.1 0l5.2 5.2a1.5 1.5 0 010 2.1L12 19.5zM8.5 9.5l6 6M12 19.5h8",
  smart: "M4 20c2.5 0 4-1.5 4-3.5a2.5 2.5 0 00-5 0c0 1 .5 1.5.5 2M7.5 15L17 5.5a2 2 0 013 3L10.5 18M17 15l1 2 2 1-2 1-1 2-1-2-2-1 2-1z",
  smarteraser: "M7.5 20.5L3.8 16.8a1.5 1.5 0 010-2.1l7.4-7.4a1.5 1.5 0 012.1 0l4.2 4.2a1.5 1.5 0 010 2.1L11 20.5zM7.5 11l5.5 5.5M11 20.5h8M18 2l.9 1.8 1.8.9-1.8.9L18 7.4l-.9-1.8-1.8-.9 1.8-.9z",
  smartpolygon: "M10 6.5l7 5-2.7 8.5H5.7L3 11.5zM19 2l.9 1.8 1.8.9-1.8.9L19 7.4l-.9-1.8-1.8-.9 1.8-.9z",
  undo: "M9 14L4 9l5-5M4 9h10a6 6 0 010 12h-3",
  redo: "M15 14l5-5-5-5M20 9H10a6 6 0 000 12h3",
};
const icon = (name) => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  for (const [k, v] of Object.entries({ fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round" })) svg.setAttribute(k, v);
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
};

const shared = followState((state) => {
  if (state?.image !== lastImage) refreshHistory(state?.image);
  render(state);
});
let lastImage;

onEvent((event) => {
  if (event.type === "annotations" && event.image === shared.get()?.image) {
    history = { can_undo: event.can_undo, can_redo: event.can_redo };
    render(shared.get());
  }
});

async function refreshHistory(image) {
  lastImage = image;
  history = { can_undo: false, can_redo: false };
  if (!image) return;
  try {
    const data = await api("GET", "/annotations", { query: { image } });
    history = { can_undo: data.can_undo, can_redo: data.can_redo };
    render(shared.get());
  } catch {
    // the viewer shows the problem
  }
}

async function change(changes) {
  try {
    await setState(changes);
    error = "";
  } catch (e) {
    error = message(e);
    render(shared.get());
  }
}

async function step(which) {
  const image = shared.get()?.image;
  if (!image) return;
  try {
    await api("POST", `/annotations/${which}`, { json: { image } });
  } catch (e) {
    error = message(e);
    render(shared.get());
  }
}

function toolButton(state, tool, label, key) {
  return h("button", { class: "tool" + (state.tool === tool ? " on" : ""), title: `${label} (${key})`, onclick: () => change({ tool }) }, icon(tool), label);
}

function render(state) {
  if (!state) return;
  const current = state.project?.classes.find((c) => c.id === state.class_id);
  const brushValue = h("span", { class: "value" }, `${state.brush} px`);
  const opacityValue = h("span", { class: "value" }, `${Math.round(state.opacity * 100)} %`);
  app.replaceChildren(
    h("div", { class: "group" },
      toolButton(state, "pan", "Pan", "V"),
      toolButton(state, "polygon", "Polygon", "P"),
      toolButton(state, "eraser", "Eraser", "E"),
      toolButton(state, "smart", "Smart brush", "B"),
      toolButton(state, "smarteraser", "Smart eraser", "X"),
      toolButton(state, "smartpolygon", "Smart polygon", "S")),
    h("div", { class: "sep" }),
    h("div", { class: "group", title: "Eraser size on screen" },
      h("span", { class: "label" }, "Eraser"),
      h("input", { type: "range", min: 4, max: 200, value: state.brush, oninput: (e) => (brushValue.textContent = `${e.target.value} px`), onchange: (e) => change({ brush: Number(e.target.value) }) }),
      brushValue),
    h("div", { class: "group", title: "How strongly shapes are filled" },
      h("span", { class: "label" }, "Fill"),
      h("input", { type: "range", min: 0, max: 100, value: Math.round(state.opacity * 100), oninput: (e) => (opacityValue.textContent = `${e.target.value} %`), onchange: (e) => change({ opacity: Number(e.target.value) / 100 }) }),
      opacityValue),
    h("div", { class: "sep" }),
    h("div", { class: "group" },
      h("button", { class: "tool", title: "Undo (Ctrl/Cmd+Z)", disabled: !history.can_undo || !state.image, onclick: () => step("undo") }, icon("undo")),
      h("button", { class: "tool", title: "Redo (Ctrl/Cmd+Shift+Z)", disabled: !history.can_redo || !state.image, onclick: () => step("redo") }, icon("redo"))),
    h("div", { class: "sep" }),
    h("div", { class: "current-class" }, current
      ? [h("span", { class: "label" }, "Class"), h("span", { class: "chip", style: `background:${current.color}` }), current.name]
      : h("span", { class: "faint" }, state.project ? "No class chosen: add or pick one in the class editor" : "No project open")),
    error && h("span", { class: "error" }, error),
    h("span", { class: "grow" }),
    h("span", { class: "faint", style: "font-size:11px" }, ...help(state.tool)));
}

function help(tool) {
  const pan = [" Hold ", h("kbd", {}, "Space"), " to pan."];
  if (tool === "smart") return ["Smart brush: drag to fill the colour region under the circle, stopping at edges. ", h("kbd", {}, "Shift"), "+drag erases, ", h("kbd", {}, "Esc"), " cancels.", ...pan];
  if (tool === "smarteraser") return ["Smart eraser: drag to erase the colour region under the circle, stopping at edges. ", h("kbd", {}, "Esc"), " cancels.", ...pan];
  const polygon = ["click points, double-click or click the first point to close, or drag freehand. ", h("kbd", {}, "Esc"), " cancels, ", h("kbd", {}, "⌫"), " removes the last point."];
  if (tool === "smartpolygon") return ["Smart polygon: the polygon is filled and grown out along its outline to the edges around it; ", ...polygon, ...pan];
  return ["Polygon: ", ...polygon, ...pan];
}
