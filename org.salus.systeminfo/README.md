# System Info

Shows the hardware of the machine the plugin's backend runs on, i.e. the machine of the Salus server, not the device the page
is opened on (an iPad shows the Mac's values). One component, `main` (center panel), with a card each for:

| Card | What it shows | Where it comes from |
|---|---|---|
| Memory (RAM) | total, available, in use (bar) | `psutil.virtual_memory()` |
| CPU | model, physical and logical cores, performance/efficiency cores on Apple Silicon, architecture | `sysctl machdep.cpu.brand_string` (macOS), `/proc/cpuinfo` (Linux), `platform.processor()`; core counts from `psutil`, core types from `sysctl hw.perflevel*` |
| GPU (one per GPU) | NVIDIA: name, VRAM total/free (bar), driver | `nvidia-smi --query-gpu=…`; without it `torch.cuda` if PyTorch can be imported |
| | Apple Silicon: chip, GPU cores, Metal, unified memory (= RAM), MPS available | `sysctl`, `system_profiler SPDisplaysDataType -json`; MPS from `torch.backends.mps` when PyTorch is installed, otherwise inferred (Apple Silicon and macOS 12.3 or later) |
| | Intel Mac: the GPUs with their VRAM | `system_profiler` |

A **Runs in** card names the container (Docker, Podman, Kubernetes, LXC, containerd, or an overlay root file system) and the
virtual machine (`systemd-detect-virt`, DMI vendor, the `hypervisor` CPU flag, WSL 2, `kern.hv_vmm_present` on macOS) the backend runs in.

Without any of these the GPU card says *No GPU found*. A tool that is missing, fails or takes longer than 10 s is skipped.
The values are read again every 5 s while the panel is shown, and with **Refresh**.

## Containers and VMs: what the backend can really use

In a container, `/proc/meminfo` and `/proc/cpuinfo` (what psutil reads) show the **host's** memory and CPUs. The caps are in the
container's cgroup, so the plugin reads them (`backend/limits.py`) and shows what the backend can use, with the machine's values
below and a note saying what caps it:

| | cgroup v2 | cgroup v1 |
|---|---|---|
| Memory limit | `memory.max` | `memory/memory.limit_in_bytes` |
| Memory in use | `memory.current` minus `inactive_file` of `memory.stat` (as `docker stats`) | `memory.usage_in_bytes` minus `total_inactive_file` |
| CPU quota | `cpu.max` (`quota period`, e.g. `150000 100000` = 1.5 cores) | `cpu.cfs_quota_us` / `cpu.cfs_period_us` |
| CPU set | the process' affinity, else `cpuset.cpus.effective` | affinity, else `cpuset.effective_cpus` / `cpuset.cpus` |

A cap may sit on any ancestor of the process' cgroup (Kubernetes caps the pod, Docker the container), so every level up to the
root is read and the tightest one counts. Available memory is the smaller of what the cgroup leaves and what the machine has free.
Usable cores are the smallest of the logical cores, the CPU set and the quota (which can be a fraction).

GPUs: a container sees only the GPUs it was given (`--gpus`), and nvidia-smi lists only those. `CUDA_VISIBLE_DEVICES` (indices
or UUIDs) narrows the list further, as CUDA reads it. A GPU's memory cannot be capped by a container; MIG and vGPU partitions
report their own size.

A VM has its own kernel, so what the OS reports already is the VM's share; the VM is only named.

## Backend

`lifetime = "panel"`: nothing is kept between requests.

```
GET /info   {host, os, python, pid,
             environment: {container: {kind, evidence} | null, virtual_machine: {kind, evidence} | null, cgroup: 1 | 2 | null},
             memory: {total, available, used_percent, machine_total, machine_available, limit: {limit, available, usage, source} | null},
             cpu: {model, architecture, physical_cores, logical_cores, performance_cores?, efficiency_cores?,
                   effective: {cores, limited_by: "quota" | "cpuset" | null, quota: {cores, source} | null, cpuset}},
             gpus: [{vendor, name, memory_total, memory_used, memory_free, unified_memory, uuid?, visible_by?, source, …}]}   sizes in bytes
```

The only dependency is `psutil`. PyTorch is *not* installed (it is large); it is used only if it is importable in the plugin's
environment.

## Setup and tests

```sh
backend/setup.sh                                                  # creates backend/.venv with psutil
cd backend && .venv/bin/python -m unittest discover -s tests      # parsing, GPU detection, cgroup v1/v2 trees, this machine
```

Restart Salus after changing the backend.

## Changes

- 0.2.0: in a container, memory and CPUs are what its cgroup allows (v1 and v2), with the machine's values below; container and VM
  are detected and shown; `CUDA_VISIBLE_DEVICES` narrows the GPU list.
- 0.1.0: first version (memory, CPU, NVIDIA / Apple Silicon / Intel Mac GPUs).
