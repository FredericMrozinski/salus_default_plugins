// Shared by the four components: the Salus connection, small DOM helpers, the shared state and its events.
import { Payload, Salus } from "./salus_sdk.js";

export const salus = Salus.connect();

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style") el.setAttribute("style", value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key in el && key !== "list") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined && child !== false) el.append(child);
  return el;
}

export const api = (method, path, options) => salus.http.json(method, path, options);
export const message = (error) => (typeof error?.body === "string" && error.body ? error.body : error?.message ?? String(error));

// The backend broadcasts every change of the shared state ({type: "state" | "annotations", ...}) on this channel.
const listeners = new Set();
salus.channel("events", (msg) => {
  const event = msg.payload.asJson();
  for (const listener of listeners) {
    try {
      listener(event);
    } catch (error) {
      console.error(error);
    }
  }
});

export function onEvent(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The shared state ({projects, project, image, class_id, tool, brush, opacity}); calls `render(state)` now and after every change. */
export function followState(render) {
  let current = null;
  const apply = (state) => {
    current = state;
    render(state);
  };
  onEvent((event) => event.type === "state" && apply(event.state));
  const load = () => api("GET", "/state").then(apply).catch((error) => console.error(error));
  load();
  salus.onShown(load); // messages may have been missed (a reconnect, say): look again when shown
  return { get: () => current, reload: load };
}

export const setState = (changes) => api("PUT", "/state", { json: changes });

// Hold to hide (backend/plugin.py): while "Hide annotations" or the H key is held down, the viewer hides the shapes. The
// panel tells the backend when it is pressed and released, and every panel hears {type: "hide", on} on "events".
let hideHeld = false;

export function setHideHeld(on) {
  if (on === hideHeld) return;
  hideHeld = on;
  try {
    salus.send("hide", Payload.json({ on }));
  } catch (error) {
    console.warn("hide:", error);
  }
}

/** Holding H (without a modifier, not while typing) hides the annotations; releasing it, or leaving the page, shows them. */
export function watchHideKey() {
  const typing = (event) => event.target.closest?.("input, textarea, select, [contenteditable]");
  window.addEventListener("keydown", (event) => {
    if (event.key.toLowerCase() === "h" && !event.repeat && !event.metaKey && !event.ctrlKey && !event.altKey && !typing(event)) setHideHeld(true);
  });
  window.addEventListener("keyup", (event) => event.key.toLowerCase() === "h" && setHideHeld(false));
  window.addEventListener("blur", () => setHideHeld(false));
}

export function modal(title, content, buttons) {
  const backdrop = h("div", { class: "backdrop" },
    h("div", { class: "dialog" }, h("header", {}, title), h("div", { class: "content" }, content), h("footer", {}, buttons)));
  document.body.append(backdrop);
  return () => backdrop.remove();
}

// Space held pans the viewer. In a panel whose button still has the keyboard focus after a click (Undo, a tool, a
// class ...), Space would press that button instead, so it does nothing on buttons here (dialogs excepted).
for (const type of ["keydown", "keyup", "keypress"]) {
  window.addEventListener(type, (event) => {
    if (event.key === " " && event.target.closest?.("button, [role=radio], [role=menuitem]") && !document.querySelector(".backdrop")) event.preventDefault();
  }, true);
}

/** A random id (UUID form). crypto.randomUUID only exists in secure contexts (HTTPS or localhost), so Salus served over
 *  plain HTTP on a network address (dx serve --addr 0.0.0.0) has none; crypto.getRandomValues works everywhere. */
export function newId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const hex = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export const fileName = (path) => path.split("/").pop();
export const dirName = (path) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");
