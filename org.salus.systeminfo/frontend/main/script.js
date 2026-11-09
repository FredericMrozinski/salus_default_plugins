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

// The big numbers are what the backend can use; where a container caps it, the machine's values follow below.
const capped = (text) => h("div", { class: "capped" }, text);
const cores = (n) => (Number.isInteger(n) ? String(n) : n.toFixed(2).replace(/0+$/, ""));

function memoryCard(memory) {
  const used = memory.total - memory.available;
  const limit = memory.limit;
  return h("section", { class: "card salus-card" },
    h("h2", { class: "salus-title" }, "Memory (RAM)"),
    h("div", { class: "big" }, bytes(memory.total)),
    limit && limit.limit < memory.machine_total
      ? capped(limit.kind === "spawner" ? "Capped by JupyterHub's memory limit (MEM_LIMIT)" : `Capped by the container (${limit.source} memory limit)`)
      : null,
    bar(used, memory.total),
    rows([
      ["Available", bytes(memory.available)],
      ["In use", `${bytes(used)} (${Math.round(memory.used_percent)} %)`],
      ...(limit ? [["Machine", `${bytes(memory.machine_total)} total, ${bytes(memory.machine_available)} available`]] : []),
    ]));
}

function cpuCard(cpu) {
  const kinds = cpu.performance_cores !== undefined ? `${cpu.performance_cores} performance, ${cpu.efficiency_cores ?? 0} efficiency` : null;
  const effective = cpu.effective ?? {};
  const limitedBy = {
    quota: `the container's CPU quota (${effective.quota?.source})`,
    cpuset: "the CPUs it may run on (cpuset)",
    spawner: "JupyterHub's CPU limit (CPU_LIMIT)",
  }[effective.limited_by];
  return h("section", { class: "card salus-card" },
    h("h2", { class: "salus-title" }, "CPU"),
    h("div", { class: "big" }, cpu.model ?? "Unknown CPU"),
    limitedBy ? capped(`Usable: ${cores(effective.cores)} of ${cpu.logical_cores} cores, capped by ${limitedBy}`) : null,
    rows([
      ...(limitedBy ? [["Usable cores", cores(effective.cores)]] : []),
      [limitedBy ? "Machine: physical" : "Physical cores", cpu.physical_cores ?? "unknown"],
      [limitedBy ? "Machine: logical" : "Logical cores", cpu.logical_cores ?? "unknown"],
      ["Core types", kinds],
      ...(effective.cpuset !== null && effective.cpuset !== undefined && effective.limited_by !== "cpuset" && effective.cpuset < cpu.logical_cores ? [["CPU set", `${effective.cpuset} cores`]] : []),
      ...(effective.quota && effective.limited_by !== "quota" ? [["CPU quota", `${cores(effective.quota.cores)} cores`]] : []),
      ["Architecture", cpu.architecture],
    ]));
}

function environmentCard(environment) {
  if (!environment) return null;
  const { container, virtual_machine: vm, cgroup } = environment;
  const where = container && vm ? `${container.kind} container in a ${vm.kind} VM` : container ? `${container.kind} container` : vm ? `${vm.kind} virtual machine` : "Directly on the machine";
  return h("section", { class: "card salus-card" },
    h("h2", { class: "salus-title" }, "Runs in"),
    h("div", { class: "big" }, where),
    rows([
      ["Container", container ? `${container.kind} (${container.evidence})` : "none found"],
      ["Virtual machine", vm ? `${vm.kind} (${vm.evidence})` : "none found"],
      ["cgroup", cgroup ? `v${cgroup}` : null],
    ]),
    vm ? h("p", { class: "faint note" }, "In a VM, memory and CPUs are the VM's share already.") : null,
    container ? h("p", { class: "faint note" }, "A container gets whole GPUs: their memory is not capped (a MIG or vGPU partition shows its own size).") : null);
}

function yesNo(value, yes = "yes", no = "no") {
  if (value === null || value === undefined) return "unknown";
  return h("span", { class: value ? "yes" : "no" }, value ? yes : no);
}

function gpuCard(gpu) {
  const memory = gpu.unified_memory
    ? [["Memory", `${bytes(gpu.memory_total)} unified (shared with the CPU)`]]
    : [[gpu.mig ? "VRAM of the slice" : "VRAM", bytes(gpu.memory_total)],
       ["Free", gpu.memory_free !== null && gpu.memory_free !== undefined ? bytes(gpu.memory_free) : "not reported (needs NVML)"]];
  const mps = gpu.mps ? [["MPS (PyTorch)", yesNo(gpu.mps.available, "available", "not available")]] : [];
  return h("section", { class: "card salus-card" },
    h("h2", { class: "salus-title" }, gpu.index !== undefined && gpu.index !== null ? `GPU ${gpu.index}` : "GPU"),
    h("div", { class: "big" }, gpu.name),
    gpu.memory_used !== null && gpu.memory_used !== undefined && gpu.memory_total ? bar(gpu.memory_used, gpu.memory_total) : null,
    rows([
      ["Vendor", gpu.vendor],
      ...(gpu.chip && gpu.chip !== gpu.name ? [["Chip", gpu.chip]] : []),
      ["GPU cores", gpu.cores],
      ["Kind", gpu.mig ? "MIG slice of a larger GPU" : null],
      ["Compute capability", gpu.compute_capability],
      ["Multiprocessors", gpu.multiprocessors],
      ...memory,
      ["Metal", gpu.metal],
      ["Driver", gpu.driver],
      ...mps,
      ["Read with", gpu.source],
    ]),
    gpu.visible_by ? h("p", { class: "faint note" }, "Only the GPUs CUDA_VISIBLE_DEVICES leaves visible are shown.") : null,
    gpu.mps?.source ? h("p", { class: "faint note" }, `MPS: ${gpu.mps.source}`) : null);
}

function render() {
  const header = h("header", {},
    h("h1", {}, info ? info.host : "System Info"),
    info ? h("span", { class: "faint" }, `${info.os.system} ${info.os.version || info.os.release} · Python ${info.python}`) : null,
    h("span", { class: "grow" }),
    h("button", { onclick: showDiagnostics, title: "The raw inputs (cgroup files, limits, GPU libraries), to send along when a value looks wrong" }, "Diagnostics"),
    h("button", { onclick: load, title: "Read the values again" }, "Refresh"));
  if (!info) {
    app.replaceChildren(header, h("div", { class: error ? "empty error" : "empty" }, error || "Loading…"));
    return;
  }
  const gpus = info.gpus.length
    ? info.gpus.map(gpuCard)
    : [h("section", { class: "card salus-card" }, h("h2", { class: "salus-title" }, "GPU"), h("div", { class: "faint" }, "No GPU found."),
        info.gpu_notes?.length ? h("ul", { class: "faint notes" }, info.gpu_notes.map((note) => h("li", {}, note))) : null)];
  app.replaceChildren(...[header,
    error ? h("p", { class: "error" }, error) : null,
    h("div", { class: "cards" }, memoryCard(info.memory), cpuCard(info.cpu), ...gpus, environmentCard(info.environment)),
    h("p", { class: "faint", style: "margin-top: 14px; font-size: 12px" }, "What the Salus server's plugin backend can use (in a container: its share), not the device this page is shown on.")].filter(Boolean));
}

async function showDiagnostics() {
  const text = h("pre", { class: "diagnostics mono" }, "Collecting…");
  const copy = h("button", { onclick: async () => {
    try {
      await navigator.clipboard.writeText(text.textContent);
      copy.textContent = "Copied";
    } catch {
      getSelection().selectAllChildren(text); // no clipboard access (e.g. plain http): select it for Cmd/Ctrl+C
      copy.textContent = "Selected: press Cmd/Ctrl+C";
    }
  } }, "Copy");
  const close = () => backdrop.remove();
  const backdrop = h("div", { class: "backdrop", onclick: (event) => event.target === backdrop && close() },
    h("div", { class: "dialog" },
      h("header", {}, "Diagnostics"),
      h("div", { class: "content" }, text),
      h("footer", {}, copy, h("button", { onclick: close }, "Close"))));
  document.body.append(backdrop);
  try {
    const response = await salus.http.get("/diagnostics");
    text.textContent = response.ok ? await response.text() : `Could not collect the diagnostics: ${response.status} ${await response.text()}`;
  } catch (e) {
    text.textContent = `Could not collect the diagnostics: ${message(e)}`;
  }
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
