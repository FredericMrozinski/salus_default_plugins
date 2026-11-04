#!/usr/bin/env python3
"""System info backend: the hardware of the machine this backend runs on (see sysinfo.py).

  GET /info          what this backend can use: memory, CPU, GPUs, the container/VM it runs in (see README)
  GET /diagnostics   plain text with the raw inputs (cgroup files, limit and GPU variables, the CUDA probe), to send along
                     when a value looks wrong
"""
import ctypes.util
import json
import logging
import shutil

import salus_sdk as salus
import sysinfo

app = salus.Plugin()
NO_CACHE = [("cache-control", "no-store")]


@app.route("GET", "/info")
def info(req):
    # A sync handler runs in a thread, so the external tools (system_profiler takes a moment) do not block the backend.
    return salus.Response.json(sysinfo.collect(), headers=NO_CACHE)


@app.route("GET", "/diagnostics")
def diagnostics(req):
    lines = [f"System Info plugin, Python {sysinfo.sys.version.split()[0]}, {sysinfo.platform.platform()}", ""]
    lines += sysinfo.limits.diagnostics()
    lines += ["", "# GPU tools",
              f"nvidia-smi: {shutil.which('nvidia-smi') or 'not found'}",
              f"libcuda: {ctypes.util.find_library('cuda') or 'not found by find_library'}",
              f"libnvidia-ml: {ctypes.util.find_library('nvidia-ml') or 'not found by find_library'}",
              f"PyTorch in the plugin's environment: {'yes' if sysinfo.torch_spec_present() else 'no'}"]
    sysinfo._probe_cache.clear()
    lines += ["", "# CUDA probe", json.dumps(sysinfo.run_cuda_probe(), indent=1)]
    info = sysinfo.collect()
    lines += ["", "# what /info reports", json.dumps({k: info[k] for k in ("environment", "memory", "cpu", "gpus", "gpu_notes")}, indent=1)]
    return salus.Response.text("\n".join(lines) + "\n", headers=NO_CACHE)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run()
