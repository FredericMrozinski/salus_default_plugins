// The Salus connection and small DOM helpers (the same as in the other plugins).
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
