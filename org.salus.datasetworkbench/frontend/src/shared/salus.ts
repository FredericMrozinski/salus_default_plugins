import { Payload, Salus } from "./salus_sdk.js";
import type { Nav, WorkbenchEvent } from "./types";

// Everything here runs when the page loads, before React renders: messages from other components are
// not buffered, so channels must be registered immediately.
export const salus = Salus.connect();
export { Payload };

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

/** Calls a backend route; throws ApiError with the backend's message for non-2xx answers. */
export async function api<T = unknown>(
  method: string,
  path: string,
  options: { query?: Query; json?: unknown; body?: Blob; signal?: AbortSignal } = {},
): Promise<T> {
  const response = await salus.http.request(method, path, {
    query: options.query,
    json: options.json,
    body: options.body,
    headers: options.body ? { "content-type": "application/octet-stream" } : undefined,
    signal: options.signal,
  });
  if (!response.ok) {
    const text = (await response.text().catch(() => "")) || response.statusText;
    throw new ApiError(response.status, text);
  }
  if (response.status === 204) return undefined as T;
  const type = response.headers.get("content-type") ?? "";
  return (type.includes("json") ? await response.json() : await response.text()) as T;
}

export const encode = encodeURIComponent;

// ---- events from the backend (state changes of the catalog)

const eventListeners = new Set<(event: WorkbenchEvent) => void>();
salus.channel("events", (message) => {
  const event = message.payload.asJson() as WorkbenchEvent;
  for (const listener of [...eventListeners]) listener(event);
});

export function onEvent(listener: (event: WorkbenchEvent) => void): () => void {
  eventListeners.add(listener);
  return () => eventListeners.delete(listener);
}

// ---- navigation commands between components (routed by Salus inside the browser)

const navListeners = new Set<(nav: Nav) => void>();
const NAV_CHANNEL = /^peer:\/\/(\d+)\/nav$/;
salus.onUnclaimed((message) => {
  if (!NAV_CHANNEL.test(message.channel)) return;
  const nav = message.payload.asJson() as Nav;
  for (const listener of [...navListeners]) listener(nav);
});

export function onNav(listener: (nav: Nav) => void): () => void {
  navListeners.add(listener);
  return () => navListeners.delete(listener);
}

/** The navigation this component was opened with (its params), if any. */
export function initialNav(): Nav | null {
  const params = salus.params as Nav | null;
  return params && typeof params === "object" && "type" in params ? params : null;
}

/** Opens (or focuses) a component of this plugin and tells it where to go. */
export async function navigate(component: string, nav: Nav): Promise<void> {
  const peer = await salus.openComponent(component, { reuse: true, params: nav });
  peer.send("nav", Payload.json(nav));
}

export async function setState(dataset: string | null, entry: number | null): Promise<void> {
  await api("PUT", "/state", { json: { dataset, entry } });
}
