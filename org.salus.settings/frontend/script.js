// The settings plugin shows what the framework tells it: the configurables of Salus and of every plugin, and the
// values in their config files. It does not read any of that itself. The framework exposes it under
// /settings-api/<frontend id> (only for this plugin); this is not part of the plugin SDK.
import { Salus, Http } from "./salus_sdk.js";

const salus = Salus.connect();
const api = `${Http.prefixOf(location.pathname)}/settings-api/${salus.frontendId}`;

async function call(method, path, body) {
  const init = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers["content-type"] = "application/json";
  }
  const response = await fetch(api + path, init);
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

const targetsEl = document.getElementById("targets");
const detailEl = document.getElementById("detail");

let targets = [];
let selected = null; // id of the target shown

const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
};

function renderTargets() {
  const buttons = targets.map((target, index) => {
    const button = el("button", { textContent: target.name, title: target.name, type: "button" });
    button.classList.toggle("selected", target.id === selected);
    button.addEventListener("click", () => select(target.id));
    return index === 0 ? [button, el("div", { className: "separator" })] : [button];
  });
  targetsEl.replaceChildren(...buttons.flat());
}

async function select(id) {
  selected = id;
  renderTargets();
  const target = targets.find((t) => t.id === id);
  const path = target.kind === "salus" ? "/config/salus" : `/config/plugin/${encodeURIComponent(id)}`;
  try {
    const config = await call("GET", path);
    if (selected === id) renderDetail(target, path, config);
  } catch (error) {
    if (selected === id) detailEl.replaceChildren(el("p", { className: "error", textContent: `Could not load the settings: ${error.message}` }));
  }
}

function renderDetail(target, path, config) {
  const heading = el("h1", { textContent: target.name });
  if (target.settings.length === 0) {
    detailEl.replaceChildren(heading, el("p", { className: "muted", textContent: "This plugin has no settings." }));
    return;
  }
  const state = { values: config.values, stored: new Set(config.stored) };
  detailEl.replaceChildren(heading, ...target.settings.map((setting) => renderSetting(setting, path, state)));
}

function renderSetting(setting, path, state) {
  const id = `setting-${setting.key}`;
  const error = el("div", { className: "error", hidden: true });
  const saved = el("span", { className: "saved", hidden: true, textContent: "Saved" });
  const reset = el("button", { type: "button", textContent: "Reset", title: "Back to the default" });
  const control = makeControl(setting, id, state.values[setting.key]);

  const refresh = () => {
    reset.hidden = !state.stored.has(setting.key);
    setControlValue(setting, control, state.values[setting.key]);
  };

  async function save(change) {
    error.hidden = true;
    try {
      const result = await call("PUT", path, { [setting.key]: change });
      state.values = result.values;
      state.stored = new Set(result.stored);
      saved.hidden = false;
      setTimeout(() => (saved.hidden = true), 1500);
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
    }
    refresh(); // shows what is really stored, also after a refused change
  }

  control.addEventListener("change", () => {
    const value = readControl(setting, control);
    if (value === undefined) return save(null); // an emptied number goes back to the default
    if (Number.isNaN(value)) return refresh();
    save(value);
  });
  reset.addEventListener("click", () => save(null));

  const hints = [];
  if (setting.min !== undefined || setting.max !== undefined) hints.push(`${setting.min ?? "…"} to ${setting.max ?? "…"}`);
  if ("default" in setting && setting.default !== "") hints.push(`default: ${setting.default}`);
  if (setting.type === "file-path") hints.push("path of a file on the server");
  if (setting.type === "directory-path") hints.push("path of a folder on the server");

  const node = el("section", { className: "setting" },
    el("div", { className: "head" }, el("label", { htmlFor: id, textContent: setting.name }), el("span", { className: "key", textContent: setting.key })),
    ...(setting.description ? [el("p", { className: "description", textContent: setting.description })] : []),
    el("div", { className: "control" }, control, reset, saved),
    ...(hints.length ? [el("div", { className: "hint", textContent: hints.join(" · ") })] : []),
    error);
  refresh();
  return node;
}

function makeControl(setting, id) {
  switch (setting.type) {
    case "bool":
      return el("input", { id, type: "checkbox" });
    case "choice":
      return el("select", { id }, ...setting.choices.map((choice) => el("option", { value: choice, textContent: choice })));
    case "int":
    case "float": {
      const input = el("input", { id, type: "number", step: setting.type === "int" ? "1" : "any" });
      if (setting.min !== undefined) input.min = setting.min;
      if (setting.max !== undefined) input.max = setting.max;
      return input;
    }
    default:
      return el("input", { id, type: "text", spellcheck: false, className: setting.type === "string" ? "" : "path" });
  }
}

function setControlValue(setting, control, value) {
  if (setting.type === "bool") control.checked = value === true;
  else control.value = value === undefined ? "" : String(value);
}

/** The value in the control: `undefined` for an empty number field, `NaN` for text that is not a number. */
function readControl(setting, control) {
  if (setting.type === "bool") return control.checked;
  if (setting.type === "int" || setting.type === "float") return control.value.trim() === "" ? undefined : Number(control.value);
  return control.value;
}

try {
  targets = await call("GET", "/configurables");
  renderTargets();
  await select(targets[0].id);
} catch (error) {
  detailEl.replaceChildren(el("p", { className: "error", textContent: `Could not load the settings: ${error.message}` }));
}
