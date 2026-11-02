// The annotation tools (bottom panel): pan, brush, eraser and polygon, the mode (Exact, or a smart mode that turns the
// last three into versions that follow the image), the tool width, the fill opacity, and undo/redo.
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
  brush: "M4 20c2.5 0 4-1.5 4-3.5a2.5 2.5 0 00-5 0c0 1 .5 1.5.5 2M7.5 15L17 5.5a2 2 0 013 3L10.5 18",
  // the smart versions carry a spark
  smartbrush: "M4 20c2.5 0 4-1.5 4-3.5a2.5 2.5 0 00-5 0c0 1 .5 1.5.5 2M7.5 15L17 5.5a2 2 0 013 3L10.5 18M17 15l1 2 2 1-2 1-1 2-1-2-2-1 2-1z",
  smarteraser: "M7.5 20.5L3.8 16.8a1.5 1.5 0 010-2.1l7.4-7.4a1.5 1.5 0 012.1 0l4.2 4.2a1.5 1.5 0 010 2.1L11 20.5zM7.5 11l5.5 5.5M11 20.5h8M18 2l.9 1.8 1.8.9-1.8.9L18 7.4l-.9-1.8-1.8-.9 1.8-.9z",
  smartpolygon: "M10 6.5l7 5-2.7 8.5H5.7L3 11.5zM19 2l.9 1.8 1.8.9-1.8.9L19 7.4l-.9-1.8-1.8-.9 1.8-.9z",
  spark: "M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8zM18.5 15l.8 1.9 1.9.8-1.9.8-.8 1.9-.8-1.9-1.9-.8 1.9-.8z",
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

// How brush, eraser and polygon work. "exact" draws exactly what is drawn; the others are smart modes, where the tools
// follow the image (the backend's MODES).
const MODES = [
  { id: "exact", name: "Exact", about: "Brush, eraser and polygon draw exactly what you draw" },
  { id: "color", name: "Color Similarity", about: "Brush, eraser and polygon follow the edges between colors in the image" },
];
const isSmart = (state) => !!state.mode && state.mode !== "exact";

function toolButton(state, tool, label, key) {
  const smart = isSmart(state) && tool !== "pan";
  const modeName = MODES.find((m) => m.id === state.mode)?.name;
  return h("button", { class: "tool" + (state.tool === tool ? " on" : ""), title: `${label}${smart ? ` (${modeName})` : ""} (${key})`, onclick: () => change({ tool }) },
    icon(smart ? "smart" + tool : tool), label);
}

function render(state) {
  if (!state) return;
  const current = state.project?.classes.find((c) => c.id === state.class_id);
  const widthValue = h("span", { class: "value" }, `${state.brush} px`);
  app.replaceChildren(
    h("div", { class: "tools" },
      // The modes as a row of buttons, not a <select>: the system's dropdown (a native popup on macOS) can keep mouse
      // moves from reaching the viewer afterwards, so the brush circle did not show until the next click.
      h("div", { class: "mode" + (isSmart(state) ? " on" : ""), role: "radiogroup", "aria-label": "Mode" },
        icon("spark"), h("span", {}, "Mode"),
        h("div", { class: "modes" }, MODES.map((m) => h("button", {
          class: "mode-option" + (state.mode === m.id ? " on" : ""), role: "radio", "aria-checked": state.mode === m.id ? "true" : "false",
          title: m.about, onclick: () => state.mode !== m.id && change({ mode: m.id }),
        }, m.name)))),
      h("div", { class: "group" },
        toolButton(state, "pan", "Pan", "V"),
        toolButton(state, "brush", "Brush", "B"),
        toolButton(state, "eraser", "Eraser", "E"),
        toolButton(state, "polygon", "Polygon", "P"))),
    h("div", { class: "sep" }),
    h("div", { class: "group", title: isSmart(state) ? "Width of the inner circle, on screen" : "Brush and eraser width, on screen" },
      h("span", { class: "label" }, "Width"),
      h("input", { type: "range", min: 4, max: 200, value: state.brush, oninput: (e) => (widthValue.textContent = `${e.target.value} px`), onchange: (e) => change({ brush: Number(e.target.value) }) }),
      widthValue),
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
    h("span", { class: "faint", style: "font-size:11px" }, ...help(state.tool, isSmart(state))));
}

function help(tool, smart) {
  const pan = [" Right-drag ", tool === "eraser" ? "paints" : "erases", ". Hold ", h("kbd", {}, "Ctrl"), " or ", h("kbd", {}, "Space"), " to pan."];
  const esc = [h("kbd", {}, "Esc"), " cancels."];
  const polygon = ["click points, double-click or click the first point to close, or drag freehand. ", h("kbd", {}, "⌫"), " removes the last point, ", ...esc];
  if (tool === "pan") return ["Pan: drag to move the image, scroll to zoom."];
  if (smart) {
    if (tool === "brush") return ["Smart brush: drag to fill the colour region under the circle, stopping at edges. ", h("kbd", {}, "Shift"), "+drag erases, ", ...esc, ...pan];
    if (tool === "eraser") return ["Smart eraser: drag to erase the colour region under the circle, stopping at edges. ", ...esc, ...pan];
    return ["Smart polygon: the polygon is filled and grown out along its outline to the edges around it; ", ...polygon, ...pan];
  }
  if (tool === "brush") return ["Brush: drag to paint. ", h("kbd", {}, "Shift"), "+drag erases, ", ...esc, ...pan];
  if (tool === "eraser") return ["Eraser: drag to cut the brushed area out of all shapes. ", ...esc, ...pan];
  return ["Polygon: ", ...polygon, ...pan];
}
