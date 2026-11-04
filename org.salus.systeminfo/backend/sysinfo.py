"""What hardware the machine has that this backend runs on: memory, CPU and GPUs.

Everything is read on each call (available memory changes). External tools (`nvidia-smi`, `sysctl`, `system_profiler`)
are called through `run`, so the tests can stand in for them; a tool that is missing or fails just gives nothing.
PyTorch is used when it can be imported, but it is not a dependency: without it, NVIDIA GPUs come from `nvidia-smi`
and MPS support is inferred from the machine and macOS version.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Optional

import psutil

import limits

log = logging.getLogger("systeminfo")

Runner = Callable[[list[str]], Optional[str]]

MIB = 1024 * 1024


def run_command(args: list[str], timeout: float = 10) -> str | None:
    """stdout of a command, or None if it is not installed, fails or takes too long."""
    if shutil.which(args[0]) is None:
        return None
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        log.info("%s failed: %s", args[0], e)
        return None
    if result.returncode != 0:
        log.info("%s exited with %s: %s", args[0], result.returncode, result.stderr.strip())
        return None
    return result.stdout


def is_apple_silicon(system: str | None = None, machine: str | None = None) -> bool:
    return (system or platform.system()) == "Darwin" and (machine or platform.machine()) == "arm64"


# --------------------------------------------------------------------------- memory and CPU

def user_rss() -> int:
    """Resident memory of all processes of this user: in a container, what the container uses."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    total = 0
    for process in psutil.process_iter(["uids", "memory_info"]):
        try:
            if uid is None or (process.info["uids"] and process.info["uids"].real == uid):
                total += process.info["memory_info"].rss
        except (psutil.Error, AttributeError, TypeError):
            continue
    return total


def spawner_memory_cap(root: Path, environ: dict | None = None) -> dict | None:
    """JupyterHub's MEM_LIMIT as a cap: in use is the container's cgroup counter, else the memory of this user's processes."""
    limit = limits.spawner_limits(environ)["memory"]
    if limit is None:
        return None
    usage = limits.own_memory_usage(root)
    if usage is None:
        usage = user_rss()
    return {"limit": limit, "available": max(0, limit - usage), "usage": usage, "source": "MEM_LIMIT", "kind": "spawner"}


def memory(root: Path = Path("/"), environ: dict | None = None) -> dict:
    """`total` and `available` are what this process can use: the machine's (in a VM the VM's), or less where a container's
    cgroup or JupyterHub's MEM_LIMIT caps it (`limit` says which). `machine_total` / `machine_available` are what the OS reports."""
    vm = psutil.virtual_memory()
    info = {"total": vm.total, "available": vm.available, "used_percent": vm.percent,
            "machine_total": vm.total, "machine_available": vm.available, "limit": None}
    caps = [cap for cap in (limits.memory_limit(root), spawner_memory_cap(root, environ)) if cap]
    cap = min(caps, key=lambda c: (c["limit"], c["available"])) if caps else None
    if cap and cap["limit"] < vm.total:
        info["total"] = cap["limit"]
        info["available"] = min(cap["available"], vm.available)
        info["used_percent"] = round(100 * (1 - info["available"] / cap["limit"]), 1) if cap["limit"] else 100.0
        info["limit"] = cap
    elif cap and cap["available"] < vm.available:  # a cap above the machine's memory that is nearly used up
        info["available"] = cap["available"]
        info["limit"] = cap
    return info


def cpu_model(run: Runner = run_command, system: str | None = None) -> str | None:
    system = system or platform.system()
    if system == "Darwin":
        name = (run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "").strip()
        if name:
            return name
    elif system == "Linux":
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                name = parse_proc_cpuinfo(f.read())
            if name:
                return name
        except OSError:
            pass
    return platform.processor() or None


def parse_proc_cpuinfo(text: str) -> str | None:
    """The CPU name in /proc/cpuinfo: `model name` on x86, `Model`/`Hardware` on some ARM boards."""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and value.strip():
            fields.setdefault(key.strip().lower(), value.strip())
    return fields.get("model name") or fields.get("model") or fields.get("hardware")


def apple_core_kinds(run: Runner = run_command) -> dict:
    """Performance and efficiency cores of Apple Silicon (`hw.perflevel0/1.physicalcpu`); empty elsewhere."""
    kinds = {}
    for level, key in ((0, "performance_cores"), (1, "efficiency_cores")):
        value = (run(["sysctl", "-n", f"hw.perflevel{level}.physicalcpu"]) or "").strip()
        if value.isdigit():
            kinds[key] = int(value)
    return kinds


def cpu(run: Runner = run_command, root: Path = Path("/")) -> dict:
    """The machine's cores, and `effective`: how many CPUs' worth of time this process can get (a container's CPU quota or
    cpuset can make that fewer, and a quota can be a fraction such as 1.5)."""
    logical = psutil.cpu_count(logical=True)
    info = {
        "model": cpu_model(run),
        "architecture": platform.machine() or None,
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": logical,
        "effective": limits.effective_cpus(logical, root, limits.spawner_limits()["cpu"]),
    }
    if is_apple_silicon():
        info.update(apple_core_kinds(run))
    return info


# --------------------------------------------------------------------------- GPUs

NVIDIA_SMI_QUERY = ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,memory.free,driver_version,uuid",
                    "--format=csv,noheader,nounits"]


def _mib(value: str) -> int | None:
    try:
        return int(float(value) * MIB)
    except ValueError:  # "[N/A]", "[Not Supported]"
        return None


def parse_nvidia_smi(text: str) -> list[dict]:
    gpus = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        index, name, total, used, free, driver = parts[:6]
        gpus.append({
            "vendor": "NVIDIA", "name": name, "index": int(index) if index.isdigit() else None,
            "memory_total": _mib(total), "memory_used": _mib(used), "memory_free": _mib(free),
            "unified_memory": False, "driver": driver or None, "uuid": parts[6] if len(parts) > 6 and parts[6] else None,
            "source": "nvidia-smi",
        })
    return gpus


def visible_gpus(gpus: list[dict], environ: dict | None = None) -> list[dict]:
    """The GPUs CUDA programs of this process may use: `CUDA_VISIBLE_DEVICES` (indices or UUIDs, as CUDA reads it: up to
    the first entry that matches nothing) narrows what nvidia-smi lists. A container given only some GPUs (`--gpus`)
    shows only those to nvidia-smi already."""
    environ = os.environ if environ is None else environ
    value = environ.get("CUDA_VISIBLE_DEVICES")
    if value is None:
        return gpus
    chosen = []
    for entry in (part.strip() for part in value.split(",")):
        match = next((gpu for gpu in gpus if (entry.isdigit() and gpu.get("index") == int(entry))
                      or (entry and gpu.get("uuid") and gpu["uuid"].startswith(entry if entry.startswith("GPU-") else "GPU-" + entry))), None)
        if match is None or match in chosen:
            break
        chosen.append(match)
    for gpu in chosen:
        gpu["visible_by"] = "CUDA_VISIBLE_DEVICES"
    return chosen


def torch_spec_present() -> bool:
    return importlib.util.find_spec("torch") is not None


def cuda_gpus_from_torch() -> list[dict]:
    try:
        import torch
    except Exception:  # noqa: BLE001 - a broken install is the same as none
        return []
    try:
        if not torch.cuda.is_available():
            return []
        gpus = []
        for index in range(torch.cuda.device_count()):
            props = torch.cuda.get_device_properties(index)
            free = total = None
            try:
                free, total = torch.cuda.mem_get_info(index)
            except Exception:  # noqa: BLE001
                total = props.total_memory
            gpus.append({
                "vendor": "NVIDIA", "name": props.name, "index": index, "memory_total": total,
                "memory_used": total - free if free is not None and total is not None else None, "memory_free": free,
                "unified_memory": False, "driver": None, "source": "PyTorch",
            })
        return gpus
    except Exception as e:  # noqa: BLE001
        log.info("torch.cuda failed: %s", e)
        return []


PROBE = Path(__file__).with_name("cuda_probe.py")
PROBE_CACHE_SECONDS = 60
_probe_cache: dict = {}
_probe_lock = threading.Lock()


def run_cuda_probe(timeout: float = 20) -> dict:
    """cuda_probe.py in a subprocess (see there): {"gpus": [...]} or {"error": ...}. Kept for a minute: the GPUs a
    process may use do not change while it runs, and starting the CUDA driver takes a moment."""
    with _probe_lock:
        cached = _probe_cache.get("result")
        if cached is not None and time.monotonic() - _probe_cache["at"] < PROBE_CACHE_SECONDS:
            return cached
        try:
            done = subprocess.run([sys.executable, str(PROBE)], capture_output=True, text=True, timeout=timeout, check=False)
            result = json.loads(done.stdout) if done.stdout.strip() else {"error": done.stderr.strip()[-300:] or f"exit code {done.returncode}"}
        except subprocess.TimeoutExpired:
            result = {"error": f"the CUDA driver did not answer within {timeout:.0f} s"}
        except (OSError, ValueError) as e:
            result = {"error": str(e)}
        _probe_cache.update(result=result, at=time.monotonic())
        return result


def nvidia_gpus(run: Runner = run_command, torch_present: Callable[[], bool] = torch_spec_present,
                probe: Callable[[], dict] = run_cuda_probe, notes: list | None = None) -> list[dict]:
    """NVIDIA GPUs: from nvidia-smi; without it from the CUDA driver (what PyTorch would see, MIG slices included);
    last from PyTorch if it is installed here. Why a source gave nothing goes into `notes`."""
    notes = notes if notes is not None else []
    output = run(NVIDIA_SMI_QUERY)
    if output:
        gpus = parse_nvidia_smi(output)
        if gpus:
            return visible_gpus(gpus)
    notes.append("nvidia-smi: not found or not working")
    result = probe()
    if result.get("gpus"):
        return result["gpus"]
    notes.append("CUDA driver: " + (result.get("error") or "no GPU visible (CUDA_VISIBLE_DEVICES or none given to the container)"))
    if torch_present():
        found = cuda_gpus_from_torch()
        if not found:
            notes.append("PyTorch: torch.cuda sees no GPU")
        return found
    return []


def parse_system_profiler(text: str) -> list[dict]:
    """The GPUs in `system_profiler SPDisplaysDataType -json`."""
    try:
        entries = json.loads(text).get("SPDisplaysDataType", [])
    except (ValueError, AttributeError):
        return []
    gpus = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("sppci_model") or entry.get("_name")
        if not name:
            continue
        cores = entry.get("sppci_cores")
        gpus.append({
            "name": name,
            "vendor": (entry.get("spdisplays_vendor") or "").replace("sppci_vendor_", "") or None,
            "cores": int(cores) if isinstance(cores, str) and cores.isdigit() else cores if isinstance(cores, int) else None,
            "vram": parse_vram(entry.get("spdisplays_vram") or entry.get("spdisplays_vram_shared")),
            "metal": metal_name(entry.get("spdisplays_mtlgpufamilysupport") or entry.get("spdisplays_metal")),
        })
    return gpus


def metal_name(value) -> str | None:
    """`"spdisplays_metal4"` as `"Metal 4"`."""
    if not isinstance(value, str) or not value:
        return None
    suffix = value.removeprefix("spdisplays_").removeprefix("metal")
    return f"Metal {suffix}".strip() if suffix != value else value


def parse_vram(text) -> int | None:
    """`"8 GB"` or `"1536 MB"` in bytes."""
    if not isinstance(text, str):
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    number, unit = parts
    factor = {"MB": MIB, "GB": 1024 * MIB, "TB": 1024 * 1024 * MIB}.get(unit.upper())
    try:
        return int(float(number) * factor) if factor else None
    except ValueError:
        return None


def macos_version_tuple(version: str | None = None) -> tuple[int, ...]:
    version = version if version is not None else platform.mac_ver()[0]
    try:
        return tuple(int(part) for part in version.split(".") if part)
    except ValueError:
        return ()


def mps_status(torch_present: Callable[[], bool] = torch_spec_present, mac_version: str | None = None) -> dict:
    """Whether PyTorch can use the Apple GPU (Metal Performance Shaders). Asked from PyTorch when it is installed here."""
    if torch_present():
        try:
            import torch
            return {"available": bool(torch.backends.mps.is_available()), "built": bool(torch.backends.mps.is_built()),
                    "source": "PyTorch " + torch.__version__}
        except Exception as e:  # noqa: BLE001
            log.info("torch.backends.mps failed: %s", e)
    supported = macos_version_tuple(mac_version) >= (12, 3)
    return {"available": supported, "built": None, "source": "macOS " + (mac_version or platform.mac_ver()[0])
            + " (PyTorch is not installed in this plugin's environment; MPS needs macOS 12.3 or later)"}


def apple_gpus(run: Runner = run_command, total_memory: int | None = None,
               torch_present: Callable[[], bool] = torch_spec_present, mac_version: str | None = None) -> list[dict]:
    """The Apple Silicon GPU: it has no memory of its own but shares the unified memory with the CPU."""
    chip = (run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "").strip() or "Apple Silicon"
    profiled = parse_system_profiler(run(["system_profiler", "SPDisplaysDataType", "-json"]) or "")
    apple = next((gpu for gpu in profiled if "apple" in gpu["name"].lower() or gpu.get("vendor") == "apple"), None)
    return [{
        "vendor": "Apple", "name": apple["name"] if apple else chip, "chip": chip,
        "cores": apple.get("cores") if apple else None, "metal": apple.get("metal") if apple else None,
        "memory_total": total_memory, "memory_used": None, "memory_free": None, "unified_memory": True,
        "mps": mps_status(torch_present, mac_version), "source": "system_profiler" if apple else "sysctl",
    }]


def other_mac_gpus(run: Runner = run_command) -> list[dict]:
    """GPUs of an Intel Mac (system_profiler)."""
    return [{
        "vendor": gpu.get("vendor"), "name": gpu["name"], "memory_total": gpu.get("vram"), "memory_used": None,
        "memory_free": None, "unified_memory": False, "source": "system_profiler",
    } for gpu in parse_system_profiler(run(["system_profiler", "SPDisplaysDataType", "-json"]) or "")]


def gpus(run: Runner = run_command, system: str | None = None, machine: str | None = None,
         torch_present: Callable[[], bool] = torch_spec_present, total_memory: int | None = None,
         probe: Callable[[], dict] | None = None, notes: list | None = None) -> list[dict]:
    system = system or platform.system()
    if is_apple_silicon(system, machine):
        return apple_gpus(run, total_memory, torch_present)
    if system == "Darwin":  # an Intel Mac: no CUDA
        return other_mac_gpus(run)
    return nvidia_gpus(run, torch_present, probe or run_cuda_probe, notes)


# --------------------------------------------------------------------------- all of it

def environment(run: Runner = run_command, root: Path = Path("/")) -> dict:
    """Whether this backend runs in a container and/or a VM, and which cgroup version caps it."""
    return {
        "container": limits.container(root),
        "virtual_machine": limits.virtual_machine(run, root),
        "cgroup": limits.cgroup_version(root) if platform.system() == "Linux" else None,
    }


def collect(run: Runner = run_command) -> dict:
    mem = memory()
    notes: list[str] = []
    found = gpus(run, total_memory=mem["total"], notes=notes)
    return {
        "gpu_notes": notes if not found else [],
        "environment": environment(run),
        "host": socket.gethostname(),
        "os": {"system": platform.system(), "release": platform.release(),
               "version": platform.mac_ver()[0] if platform.system() == "Darwin" else platform.version()},
        "python": sys.version.split()[0],
        "pid": os.getpid(),
        "memory": mem,
        "cpu": cpu(run),
        "gpus": found,
    }
