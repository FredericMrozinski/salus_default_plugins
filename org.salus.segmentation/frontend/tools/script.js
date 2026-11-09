// The annotation tools (bottom panel), in sections from left to right: the mode (Exact, or Smart, which turns brush,
// eraser and polygon into versions that follow the image), the tools (pan, brush, eraser, polygon, 2 x 2), the tool
// width (and, in Smart, how strict its colours are), and undo/redo at the right edge.
// The choice is shared state, so the viewer follows it.
import { api, followState, h, message, onEvent, setState, watchHideKey } from "../shared/common.js";

watchHideKey(); // H held down hides the annotations (see shared/common.js)

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

// The modes shown: Exact and Smart (the backend's Border Growth). The model modes (Swin Edges, backend/models/) are hidden
// for now; the backend still offers them.
const MODES = [
  { id: "exact", name: "Exact", icon: "architecture", about: "Brush, eraser and polygon draw exactly what you draw" },
  { id: "border", name: "Smart", icon: "stars", about: "Brush, eraser and polygon follow the image: each dab grows the painted area from its border into the colors of the inner circle, stopping at edges" },
];
// Material Symbols "architecture" and "stars_2" (weight 200, 24 units), filled.
const MODE_ICONS = {
  architecture: "M7.13,19.92L7,18.88l2.58-7.12q0.2,0.18 0.42,0.28t0.45,0.17L7.93,19.18L7.13,19.92Zm9.73,0l-0.79-0.74l-2.52-6.97q0.23-0.07 0.45-0.17t0.42-0.28L17,18.88l-0.13,1.05ZM10.23,9.77Q9.5,9.04 9.5,8q0-0.98 0.61-1.63T11.5,5.58V3h1V5.58q0.78,0.13 1.39,0.78T14.5,8q0,1.04-0.73,1.77T12,10.5T10.23,9.77Zm2.83-0.71Q13.5,8.62 13.5,8T13.06,6.94T12,6.5T10.94,6.94T10.5,8t0.44,1.06T12,9.5t1.06-0.44Z",
  stars: "M7.97,17.88L12,15.46l4.05,2.46L15,13.35l3.54-3.05L13.87,9.87L12,5.55l-1.85,4.3l-4.67,0.4l3.54,3.1L7.97,17.88ZM6.44,20l1.47-6.28L3,9.48L9.47,8.93L12,3l2.55,5.93l6.47,0.55l-4.91,4.24L17.58,20L12,16.66L6.44,20ZM17.42,7.06l0.45-1.88L16.37,3.92l1.98-0.16l0.77-1.77l0.77,1.77l1.98,0.16L20.36,5.18l0.45,1.88l-1.69-1l-1.69,1Zm-5.4,4.67Z",
};
function modeIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", MODE_ICONS[name]);
  svg.append(path);
  return svg;
}

// Smart only: how strict the colours away from the brush are (backend/borderbrush.py, `strict_profile`).
const SENSITIVITIES = [
  { id: "normal", name: "Normal", about: "Colors: as close to the inner circle's as before, also some way beyond it" },
  { id: "strict", name: "Strict", about: "Colors: only close ones in the inner circle and very close ones beyond it; less leaking, the area stays nearer to the brush" },
];

function sensitivitySwitch(state) {
  const current = state.sensitivity ?? "normal";
  return h("div", { class: "setting", role: "radiogroup", "aria-label": "Colors" },
    h("span", { class: "label" }, "Colors"),
    h("div", { class: "choice-buttons" }, SENSITIVITIES.map((s) => h("button", {
      class: "choice" + (current === s.id ? " on" : ""), role: "radio", "aria-checked": current === s.id ? "true" : "false",
      title: s.about, onclick: () => current !== s.id && change({ sensitivity: s.id }),
    }, s.name))));
}
const isSmart = (state) => !!state.mode && state.mode !== "exact";

function toolButton(state, tool, label, key) {
  const smart = isSmart(state) && tool !== "pan";
  const modeName = MODES.find((m) => m.id === state.mode)?.name ?? "";
  return h("button", { class: "tool" + (state.tool === tool ? " on" : ""), title: `${label}${smart ? ` (${modeName})` : ""} (${key})`, onclick: () => change({ tool }) },
    icon(smart ? "smart" + tool : tool), label);
}

function render(state) {
  if (!state) return;
  const widthValue = h("span", { class: "value" }, `${state.brush} px`);
  app.replaceChildren(
    // The modes as buttons, not a <select>: the system's dropdown (a native popup on macOS) can keep mouse moves from
    // reaching the viewer afterwards, so the brush circle did not show until the next click.
    h("section", { class: "section mode-section" },
      h("div", { class: "section-head" }, "Mode"),
      h("div", { class: "stack", role: "radiogroup", "aria-label": "Mode" }, MODES.map((m) => h("button", {
        class: "mode-button" + (state.mode === m.id ? " on" : ""), role: "radio", "aria-checked": state.mode === m.id ? "true" : "false",
        title: m.about, onclick: () => state.mode !== m.id && change({ mode: m.id }),
      }, modeIcon(m.icon), m.name)))),
    // the brush follows from the mode: the line between them points right
    h("div", { class: "salus-vseparator salus-arrow", "aria-hidden": "true" }, h("span")),
    h("section", { class: "section tool-section" },
      h("div", { class: "section-head" }, "Brush"),
      h("div", { class: "tool-grid" },
        toolButton(state, "pan", "Pan", "V"),
        toolButton(state, "brush", "Brush", "B"),
        toolButton(state, "eraser", "Eraser", "E"),
        toolButton(state, "polygon", "Polygon", "P"))),
    // the brush settings follow from the brush: the line between them points right
    h("div", { class: "salus-vseparator salus-arrow", style: "--salus-right: var(--salus-depth-default)", "aria-hidden": "true" }, h("span")),
    h("section", { class: "section width-section" },
      h("div", { class: "section-head" }, "Brush settings"),
      h("div", { class: "setting", title: isSmart(state) ? "Width of the inner circle, on screen" : "Brush and eraser width, on screen" },
        h("span", { class: "label" }, "Width"),
        h("input", { type: "range", min: 4, max: 200, value: state.brush, oninput: (e) => (widthValue.textContent = `${e.target.value} px`), onchange: (e) => change({ brush: Number(e.target.value) }) }),
        widthValue),
      state.mode === "border" ? sensitivitySwitch(state) : null,
      error && h("div", { class: "error" }, error)),
    h("section", { class: "section history-section" },
      h("button", { class: "tool", title: "Undo (Ctrl/Cmd+Z)", disabled: !history.can_undo || !state.image, onclick: () => step("undo") }, icon("undo")),
      h("button", { class: "tool", title: "Redo (Ctrl/Cmd+Shift+Z)", disabled: !history.can_redo || !state.image, onclick: () => step("redo") }, icon("redo"))));
}
