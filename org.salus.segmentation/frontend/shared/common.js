// Shared by the four components: the Salus connection, small DOM helpers, the shared state and its events.
import { Salus } from "./salus_sdk.js";

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

export function modal(title, content, buttons) {
  const backdrop = h("div", { class: "backdrop" },
    h("div", { class: "dialog" }, h("header", {}, title), h("div", { class: "content" }, content), h("footer", {}, buttons)));
  document.body.append(backdrop);
  return () => backdrop.remove();
}

export const fileName = (path) => path.split("/").pop();
export const dirName = (path) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");
