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

Without any of these the GPU card says *No GPU found*. A tool that is missing, fails or takes longer than 10 s is skipped.
The values are read again every 5 s while the panel is shown, and with **Refresh**.

## Backend

`lifetime = "panel"`: nothing is kept between requests.

```
GET /info   {host, os, python, pid, memory: {total, available, used_percent},
             cpu: {model, architecture, physical_cores, logical_cores, performance_cores?, efficiency_cores?},
             gpus: [{vendor, name, memory_total, memory_used, memory_free, unified_memory, source, …}]}   sizes in bytes
```

The only dependency is `psutil`. PyTorch is *not* installed (it is large); it is used only if it is importable in the plugin's
environment.

## Setup and tests

```sh
backend/setup.sh                                                  # creates backend/.venv with psutil
cd backend && .venv/bin/python -m unittest discover -s tests      # parsing, GPU detection with stand-in tools, this machine
```

Restart Salus after changing the backend.

## Changes

- 0.1.0: first version (memory, CPU, NVIDIA / Apple Silicon / Intel Mac GPUs).
