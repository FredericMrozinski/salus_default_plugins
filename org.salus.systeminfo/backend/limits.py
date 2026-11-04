"""What this process may really use when it runs in a container or a VM.

A container shares the kernel of its host, and `/proc/meminfo` and `/proc/cpuinfo` (which psutil reads) show the host's
memory and CPUs. The caps of a container live in its cgroup:

  cgroup v2   memory.max, memory.current, memory.stat (inactive_file), cpu.max ("quota period"), cpuset.cpus.effective
  cgroup v1   memory/memory.limit_in_bytes, memory.usage_in_bytes, memory.stat (total_inactive_file),
              cpu/cpu.cfs_quota_us + cpu.cfs_period_us, cpuset/cpuset.effective_cpus

A cap may be set on any ancestor of the process' cgroup (Kubernetes sets it on the pod, Docker on the container), so every
level from the process' cgroup up to the root is read and the tightest one counts. Memory in use is counted like
`docker stats`: usage minus the page cache the kernel can drop (inactive_file).

A VM, by contrast, has its own kernel: what the OS reports already is the VM's share. It is only detected and named.

Everything is read below `root` (normally "/"), so the tests can use a folder that looks like one.
"""
from __future__ import annotations

import os
import platform
from collections.abc import Callable
from pathlib import Path
from typing import Optional

Runner = Callable[[list[str]], Optional[str]]

# cgroup v1 writes a page-aligned LONG_MAX for "no limit"; anything this large is no cap.
UNLIMITED = 1 << 60


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def parse_cpu_list(text: str | None) -> int | None:
    """Number of CPUs in a list like "0-3,6,8-9" (cpuset files); None if empty or unreadable."""
    if not text:
        return None
    count = 0
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        start, sep, end = part.partition("-")
        try:
            count += int(end) - int(start) + 1 if sep else 1
        except ValueError:
            return None
    return count or None


def _stat_value(text: str | None, key: str) -> int | None:
    for line in (text or "").splitlines():
        name, _, value = line.partition(" ")
        if name == key:
            return _int(value.strip())
    return None


# --------------------------------------------------------------------------- the cgroup of this process

def own_cgroups(root: Path) -> dict[str, str]:
    """/proc/self/cgroup as {controller: path}; the cgroup v2 entry ("0::/path") is under "".

    v1 lines name their controllers ("4:memory:/docker/abc", "3:cpu,cpuacct:/..."), each controller gets the path.
    """
    paths = {}
    for line in (_read(root / "proc/self/cgroup") or "").splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        _, controllers, path = parts
        for controller in controllers.split(",") if controllers else [""]:
            paths[controller] = path
    return paths


def _levels(mount: Path, path: str) -> list[Path]:
    """The cgroup folders from the process' own up to the mount point, the deepest first. A path that does not exist below
    the mount (a cgroup v1 container sees its host's path but has its own cgroup mounted at the root) is skipped."""
    parts = [part for part in path.split("/") if part]
    levels = [mount.joinpath(*parts[:depth]) for depth in range(len(parts), 0, -1)]
    return [level for level in levels if level.is_dir()] + [mount]


def cgroup_version(root: Path) -> int | None:
    base = root / "sys/fs/cgroup"
    if (base / "cgroup.controllers").is_file():
        return 2
    if (base / "memory").is_dir() or (base / "cpu").is_dir() or (base / "cpu,cpuacct").is_dir():
        return 1
    return None


def _v1_mount(root: Path, controller: str) -> Path | None:
    base = root / "sys/fs/cgroup"
    for name in (controller, "cpu,cpuacct", "cpuacct,cpu") if controller == "cpu" else (controller,):
        if (base / name).is_dir():
            return base / name
    return None


# --------------------------------------------------------------------------- memory

def memory_limit(root: Path = Path("/")) -> dict | None:
    """The tightest memory cap of this process' cgroups: {limit, available, usage, source}; None without one."""
    version = cgroup_version(root)
    cgroups = own_cgroups(root)
    if version == 2:
        levels = _levels(root / "sys/fs/cgroup", cgroups.get("", "/"))
        max_file, usage_file, cache_key = "memory.max", "memory.current", "inactive_file"
    elif version == 1:
        mount = _v1_mount(root, "memory")
        if mount is None:
            return None
        levels = _levels(mount, cgroups.get("memory", "/"))
        max_file, usage_file, cache_key = "memory.limit_in_bytes", "memory.usage_in_bytes", "total_inactive_file"
    else:
        return None

    best = None
    for level in levels:
        limit = _int(_read(level / max_file))  # "max" (v2, no cap) reads as None
        if limit is None or limit >= UNLIMITED:
            continue
        usage = _int(_read(level / usage_file))
        if usage is not None:
            usage = max(0, usage - (_stat_value(_read(level / "memory.stat"), cache_key) or 0))
        available = max(0, limit - usage) if usage is not None else limit
        if best is None or available < best["available"] or (available == best["available"] and limit < best["limit"]):
            best = {"limit": limit, "available": available, "usage": usage, "source": f"cgroup v{version}", "kind": "cgroup"}
    return best


def own_memory_usage(root: Path = Path("/")) -> int | None:
    """Memory in use by this process' own cgroup (the container), without droppable page cache; None if unreadable.
    Readable also where the cgroup has no limit of its own."""
    version = cgroup_version(root)
    cgroups = own_cgroups(root)
    if version == 2:
        level = _levels(root / "sys/fs/cgroup", cgroups.get("", "/"))[0]
        usage_file, cache_key = "memory.current", "inactive_file"
    elif version == 1 and (mount := _v1_mount(root, "memory")) is not None:
        level = _levels(mount, cgroups.get("memory", "/"))[0]
        usage_file, cache_key = "memory.usage_in_bytes", "total_inactive_file"
    else:
        return None
    usage = _int(_read(level / usage_file))
    if usage is None:
        return None
    return max(0, usage - (_stat_value(_read(level / "memory.stat"), cache_key) or 0))


# --------------------------------------------------------------------------- limits a spawner hands over

_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def parse_bytes(text: str | None) -> int | None:
    """"4294967296", "4G", "512M", "1.5G" in bytes; None if empty or not a size."""
    text = (text or "").strip().upper().removesuffix("B").removesuffix("I")
    if not text:
        return None
    unit = text[-1] if text[-1] in _UNITS and not text[-1].isdigit() else ""
    try:
        value = float(text[:-1] if unit else text)
    except ValueError:
        return None
    return int(value * _UNITS[unit]) if value > 0 else None


def spawner_limits(environ: dict | None = None) -> dict:
    """The limits JupyterHub's spawner gives the single-user server, as `MEM_LIMIT` (bytes) and `CPU_LIMIT` (cores). They are
    what the Hub allots; whether they are enforced depends on the spawner (a container spawner usually also sets the cgroup)."""
    environ = os.environ if environ is None else environ
    try:
        cpu = float(environ.get("CPU_LIMIT", "")) or None
    except ValueError:
        cpu = None
    return {"memory": parse_bytes(environ.get("MEM_LIMIT")), "cpu": cpu if cpu and cpu > 0 else None}


# --------------------------------------------------------------------------- CPU

def cpu_quota(root: Path = Path("/")) -> dict | None:
    """The tightest CPU time cap: {cores (a fraction, e.g. 1.5), source}; None without one."""
    version = cgroup_version(root)
    cgroups = own_cgroups(root)
    quotas = []
    if version == 2:
        for level in _levels(root / "sys/fs/cgroup", cgroups.get("", "/")):
            parts = (_read(level / "cpu.max") or "").split()
            if len(parts) == 2 and parts[0] != "max":
                quota, period = _int(parts[0]), _int(parts[1])
                if quota and period:
                    quotas.append(quota / period)
    elif version == 1:
        mount = _v1_mount(root, "cpu")
        if mount is not None:
            for level in _levels(mount, cgroups.get("cpu", "/")):
                quota, period = _int(_read(level / "cpu.cfs_quota_us")), _int(_read(level / "cpu.cfs_period_us"))
                if quota and quota > 0 and period:
                    quotas.append(quota / period)
    if not quotas:
        return None
    return {"cores": round(min(quotas), 2), "source": f"cgroup v{version}"}


def cpuset_count(root: Path = Path("/")) -> int | None:
    """How many CPUs this process may run on: its affinity (which follows the cpuset), else the cpuset file."""
    if root == Path("/") and hasattr(os, "sched_getaffinity"):
        try:
            return len(os.sched_getaffinity(0))
        except OSError:
            pass
    version = cgroup_version(root)
    cgroups = own_cgroups(root)
    if version == 2:
        for level in _levels(root / "sys/fs/cgroup", cgroups.get("", "/")):
            count = parse_cpu_list(_read(level / "cpuset.cpus.effective"))
            if count:
                return count
    elif version == 1:
        mount = _v1_mount(root, "cpuset")
        if mount is not None:
            for level in _levels(mount, cgroups.get("cpuset", "/")):
                count = parse_cpu_list(_read(level / "cpuset.effective_cpus") or _read(level / "cpuset.cpus"))
                if count:
                    return count
    return None


def effective_cpus(logical: int | None, root: Path = Path("/"), spawner_cpu: float | None = None) -> dict:
    """{cores, limited_by: None | "quota" | "cpuset" | "spawner", quota, cpuset, spawner}: how many CPUs' worth of time
    this process can get."""
    quota = cpu_quota(root)
    cpuset = cpuset_count(root)
    candidates = [(float(logical), None)] if logical else []
    if spawner_cpu is not None and (logical is None or spawner_cpu < logical):
        candidates.append((round(spawner_cpu, 2), "spawner"))
    if cpuset is not None and (logical is None or cpuset < logical):
        candidates.append((float(cpuset), "cpuset"))
    if quota is not None and (logical is None or quota["cores"] < logical):
        candidates.append((quota["cores"], "quota"))
    if not candidates:
        return {"cores": None, "limited_by": None, "quota": quota, "cpuset": cpuset, "spawner": spawner_cpu}
    cores, by = min(candidates, key=lambda candidate: candidate[0])
    return {"cores": cores, "limited_by": by, "quota": quota, "cpuset": cpuset, "spawner": spawner_cpu}


# --------------------------------------------------------------------------- what we run in

def container(root: Path = Path("/"), environ: dict | None = None) -> dict | None:
    """{kind, evidence} if this process runs in a container (Docker, Podman, Kubernetes, LXC, ...), else None."""
    environ = os.environ if environ is None else environ
    if environ.get("KUBERNETES_SERVICE_HOST"):
        return {"kind": "Kubernetes", "evidence": "KUBERNETES_SERVICE_HOST is set"}
    if (root / ".dockerenv").exists():
        return {"kind": "Docker", "evidence": "/.dockerenv"}
    if (root / "run/.containerenv").exists():
        return {"kind": "Podman", "evidence": "/run/.containerenv"}
    if environ.get("container"):
        return {"kind": environ["container"], "evidence": "$container"}
    cgroup = _read(root / "proc/1/cgroup") or ""
    for marker, kind in (("kubepods", "Kubernetes"), ("docker", "Docker"), ("libpod", "Podman"), ("lxc", "LXC"),
                         ("containerd", "containerd")):
        if marker in cgroup:
            return {"kind": kind, "evidence": "/proc/1/cgroup"}
    for line in (_read(root / "proc/self/mountinfo") or "").splitlines():
        fields = line.split()
        if len(fields) > 4 and fields[4] == "/" and " - overlay " in line:
            return {"kind": "container", "evidence": "the root file system is an overlay"}
    return None


VM_VENDORS = (
    ("qemu", "QEMU/KVM"), ("kvm", "KVM"), ("vmware", "VMware"), ("virtualbox", "VirtualBox"), ("innotek", "VirtualBox"),
    ("xen", "Xen"), ("amazon ec2", "Amazon EC2"), ("google compute engine", "Google Compute Engine"),
    ("parallels", "Parallels"), ("bochs", "Bochs"), ("openstack", "OpenStack"), ("apple virtualization", "Apple Virtualization"),
)


def virtual_machine(run: Runner, root: Path = Path("/"), system: str | None = None, release: str | None = None) -> dict | None:
    """{kind, evidence} if the OS runs in a virtual machine, else None. (In a container this is about the host's kernel.)"""
    system = system or platform.system()
    release = release if release is not None else platform.release()
    if system == "Darwin":
        if (run(["sysctl", "-n", "kern.hv_vmm_present"]) or "").strip() == "1":
            return {"kind": "virtual machine", "evidence": "kern.hv_vmm_present"}
        return None
    if system != "Linux":
        return None
    if "microsoft" in release.lower():
        return {"kind": "WSL 2 (Hyper-V)", "evidence": "kernel release"}
    detected = (run(["systemd-detect-virt", "--vm"]) or "").strip()
    if detected and detected != "none":
        return {"kind": detected, "evidence": "systemd-detect-virt"}
    dmi = " ".join(filter(None, (_read(root / f"sys/class/dmi/id/{name}") for name in ("sys_vendor", "product_name")))).lower()
    for marker, kind in VM_VENDORS:
        if marker in dmi:
            return {"kind": kind, "evidence": "DMI"}
    if "microsoft corporation" in dmi and "virtual" in dmi:
        return {"kind": "Hyper-V", "evidence": "DMI"}
    for line in (_read(root / "proc/cpuinfo") or "").splitlines():
        if line.startswith("flags") and " hypervisor" in line:
            return {"kind": "virtual machine", "evidence": "CPU flag hypervisor"}
    return None


# --------------------------------------------------------------------------- what to send when the values look wrong

# Only these variables are shown (never the whole environment: it holds e.g. JUPYTERHUB_API_TOKEN).
DIAGNOSTIC_VARIABLES = ("MEM_LIMIT", "MEM_GUARANTEE", "CPU_LIMIT", "CPU_GUARANTEE", "CUDA_VISIBLE_DEVICES",
                        "NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES", "KUBERNETES_SERVICE_HOST", "container")
CGROUP_FILES = ("memory.max", "memory.high", "memory.current", "memory.limit_in_bytes", "memory.usage_in_bytes",
                "cpu.max", "cpu.cfs_quota_us", "cpu.cfs_period_us", "cpuset.cpus.effective", "cpuset.effective_cpus")


def diagnostics(root: Path = Path("/"), environ: dict | None = None) -> list[str]:
    """Plain lines with the raw inputs of the limits: cgroup files at every level, the relevant mounts and variables."""
    environ = os.environ if environ is None else environ
    lines = ["# environment"]
    lines += [f"{name}={environ[name]}" for name in DIAGNOSTIC_VARIABLES if name in environ] or ["(none of the limit/GPU variables set)"]
    lines += ["", "# /proc/self/cgroup", _read(root / "proc/self/cgroup") or "(unreadable)"]
    lines += ["", f"# cgroup version: {cgroup_version(root)}"]
    cgroups = own_cgroups(root)
    mounts = [("", root / "sys/fs/cgroup")] if cgroup_version(root) == 2 else \
        [(c, m) for c in ("memory", "cpu", "cpuset") if (m := _v1_mount(root, c)) is not None]
    for controller, mount in mounts:
        for level in _levels(mount, cgroups.get(controller, "/")):
            values = [f"{name}={value}" for name in CGROUP_FILES if (value := _read(level / name)) is not None]
            lines.append(f"{level}: {' '.join(values) or '(no limit files)'}")
    lines += ["", "# cgroup mounts"]
    lines += [line for line in (_read(root / "proc/self/mountinfo") or "").splitlines() if "cgroup" in line][:20] or ["(none)"]
    meminfo = [line for line in (_read(root / "proc/meminfo") or "").splitlines() if line.startswith(("MemTotal", "MemAvailable"))]
    lines += ["", "# /proc/meminfo", *(meminfo or ["(unreadable)"])]
    devices = sorted(p.name for p in (root / "dev").glob("nvidia*")) if (root / "dev").is_dir() else []
    lines += ["", "# /dev/nvidia*", " ".join(devices) or "(none)"]
    return lines
