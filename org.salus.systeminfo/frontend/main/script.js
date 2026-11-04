// Shows what the backend finds out about the machine it runs on (GET /info, see backend/sysinfo.py).
// The available memory changes, so the page reloads it every few seconds while it is shown, and on Refresh.
import { api, h, message, salus } from "../shared/common.js";

const app = document.getElementById("app");
const REFRESH_MS = 5000;
let info = null;
let error = "";
let timer = null;

function bytes(n) {
  if (n === null || n === undefined) return "unknown";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i >= 3 ? 1 : 0)} ${units[i]}`;
}

const rows = (pairs) => h("dl", {}, pairs.filter(([, v]) => v !== null && v !== undefined && v !== "").flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));

function bar(used, total) {
  const share = total ? Math.min(1, Math.max(0, used / total)) : 0;
  return h("div", { class: share > 0.9 ? "bar high" : "bar", title: `${Math.round(share * 100)} % used` }, h("div", { style: `width: ${share * 100}%` }));
}

function memoryCard(memory) {
  const used = memory.total - memory.available;
  return h("section", { class: "card" },
    h("h2", {}, "Memory (RAM)"),
    h("div", { class: "big" }, bytes(memory.total)),
    bar(used, memory.total),
    rows([["Available", bytes(memory.available)], ["In use", `${bytes(used)} (${Math.round(memory.used_percent)} %)`]]));
}

function cpuCard(cpu) {
  const kinds = cpu.performance_cores !== undefined ? `${cpu.performance_cores} performance, ${cpu.efficiency_cores ?? 0} efficiency` : null;
  return h("section", { class: "card" },
    h("h2", {}, "CPU"),
    h("div", { class: "big" }, cpu.model ?? "Unknown CPU"),
    rows([["Physical cores", cpu.physical_cores ?? "unknown"], ["Logical cores", cpu.logical_cores ?? "unknown"], ["Core types", kinds], ["Architecture", cpu.architecture]]));
}

function yesNo(value, yes = "yes", no = "no") {
  if (value === null || value === undefined) return "unknown";
  return h("span", { class: value ? "yes" : "no" }, value ? yes : no);
}

function gpuCard(gpu) {
  const memory = gpu.unified_memory
    ? [["Memory", `${bytes(gpu.memory_total)} unified (shared with the CPU)`]]
    : [["VRAM", bytes(gpu.memory_total)], ["Free", gpu.memory_free !== null ? bytes(gpu.memory_free) : null]];
  const mps = gpu.mps ? [["MPS (PyTorch)", yesNo(gpu.mps.available, "available", "not available")]] : [];
  return h("section", { class: "card" },
    h("h2", {}, gpu.index !== undefined && gpu.index !== null ? `GPU ${gpu.index}` : "GPU"),
    h("div", { class: "big" }, gpu.name),
    gpu.memory_used !== null && gpu.memory_used !== undefined && gpu.memory_total ? bar(gpu.memory_used, gpu.memory_total) : null,
    rows([
      ["Vendor", gpu.vendor],
      ...(gpu.chip && gpu.chip !== gpu.name ? [["Chip", gpu.chip]] : []),
      ["GPU cores", gpu.cores],
      ...memory,
      ["Metal", gpu.metal],
      ["Driver", gpu.driver],
      ...mps,
      ["Read with", gpu.source],
    ]),
    gpu.mps?.source ? h("p", { class: "faint", style: "margin: 8px 0 0; font-size: 12px" }, `MPS: ${gpu.mps.source}`) : null);
}

function render() {
  const header = h("header", {},
    h("h1", {}, info ? info.host : "System Info"),
    info ? h("span", { class: "faint" }, `${info.os.system} ${info.os.version || info.os.release} · Python ${info.python}`) : null,
    h("span", { class: "grow" }),
    h("button", { onclick: load, title: "Read the values again" }, "Refresh"));
  if (!info) {
    app.replaceChildren(header, h("div", { class: error ? "empty error" : "empty" }, error || "Loading…"));
    return;
  }
  const gpus = info.gpus.length
    ? info.gpus.map(gpuCard)
    : [h("section", { class: "card" }, h("h2", {}, "GPU"), h("div", { class: "faint" }, "No GPU found (no NVIDIA GPU via nvidia-smi or PyTorch, no Apple Silicon)."))];
  app.replaceChildren(...[header,
    error ? h("p", { class: "error" }, error) : null,
    h("div", { class: "cards" }, memoryCard(info.memory), cpuCard(info.cpu), ...gpus),
    h("p", { class: "faint", style: "margin-top: 14px; font-size: 12px" }, "The machine the Salus server runs on, not the device this page is shown on.")].filter(Boolean));
}

async function load() {
  try {
    info = await api("GET", "/info");
    error = "";
  } catch (e) {
    error = `Could not read the system information: ${message(e)}`;
  }
  render();
}

function startPolling() {
  clearInterval(timer);
  timer = setInterval(load, REFRESH_MS);
}

salus.onShown(() => {
  load();
  startPolling();
});
salus.onHidden(() => clearInterval(timer));
load();
startPolling();
