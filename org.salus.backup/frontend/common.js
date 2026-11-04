// Small helpers: the Salus connection, building DOM elements, HTTP and dialogs.
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

export function modal(title, content, buttons, { wide = false } = {}) {
  const backdrop = h("div", { class: "backdrop" },
    h("div", { class: "dialog" + (wide ? " wide" : "") }, h("header", {}, title), h("div", { class: "content" }, content), h("footer", {}, buttons)));
  document.body.append(backdrop);
  return () => backdrop.remove();
}

export function bytes(n) {
  if (n === null || n === undefined) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) (n /= 1024), i++;
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

export function duration(seconds) {
  seconds = Math.max(0, Math.round(seconds ?? 0));
  const hours = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  return hours ? `${hours} h ${String(m).padStart(2, "0")} min` : m ? `${m} min ${String(s).padStart(2, "0")} s` : `${s} s`;
}

// The workbench's Material Symbols icons (viewBox 0 -960 960 960), drawn in the text color.
const ICONS = {
  add: "M460-460H240v-40h220v-220h40v220h220v40H500v220h-40v-220Z",
  refresh: "M483.08-200q-117.25 0-198.63-81.34-81.37-81.34-81.37-198.54 0-117.2 81.37-198.66Q365.83-760 483.08-760q71.3 0 133.54 33.88 62.23 33.89 100.3 94.58V-760h40v209.23H547.69v-40h148q-31.23-59.85-87.88-94.54Q551.15-720 483.08-720q-100 0-170 70t-70 170q0 100 70 170t170 70q77 0 139-44t87-116h42.46Q725.08-310.15 651-255.08 576.92-200 483.08-200Z",
};

export function icon(name, size = 20) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [k, v] of Object.entries({ viewBox: "0 -960 960 960", width: size, height: size, fill: "currentColor", class: "icon", "aria-hidden": "true" })) svg.setAttribute(k, v);
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}
