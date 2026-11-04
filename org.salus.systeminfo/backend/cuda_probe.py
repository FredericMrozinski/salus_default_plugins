#!/usr/bin/env python3
"""The GPUs CUDA programs of this process can use, read from the NVIDIA driver (libcuda) with ctypes: no PyTorch, no nvidia-smi.

In a container the NVIDIA runtime often mounts only the CUDA driver (`NVIDIA_DRIVER_CAPABILITIES=compute`): nvidia-smi
and NVML are missing, but PyTorch works. PyTorch uses this same driver library, so whatever `torch.cuda` sees is seen here.
CUDA applies CUDA_VISIBLE_DEVICES itself, and a MIG slice shows up as its own device with its own memory
(named e.g. "NVIDIA A100-SXM4-40GB MIG 3g.20gb").

Only device properties are read; no CUDA context is created, so no GPU memory is taken. Free memory comes from NVML
(libnvidia-ml) where it is installed and the device is a whole GPU (NVML would report a MIG slice's parent).

sysinfo.py runs this as a subprocess (`python cuda_probe.py` prints JSON), so a driver that hangs or crashes cannot take
the backend with it.
"""
from __future__ import annotations

import ctypes
import json
import sys

CUDA_LIBRARIES = ("libcuda.so.1", "libcuda.so", "nvcuda.dll")
NVML_LIBRARIES = ("libnvidia-ml.so.1", "libnvidia-ml.so", "nvml.dll")

ATTRIBUTE_MULTIPROCESSORS = 16
ATTRIBUTE_CC_MAJOR = 75
ATTRIBUTE_CC_MINOR = 76


def load(names):
    for name in names:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


def format_uuid(raw: bytes, mig: bool = False) -> str:
    h = raw.hex()
    return f"{'MIG' if mig else 'GPU'}-{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


class _NvmlMemory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


def nvml_memory(nvml, uuid: str) -> dict | None:
    """{total, free, used} of the whole GPU with this UUID, from NVML; None if NVML cannot tell."""
    if nvml is None or not uuid.startswith("GPU-"):
        return None
    handle = ctypes.c_void_p()
    if nvml.nvmlDeviceGetHandleByUUID(uuid.encode(), ctypes.byref(handle)) != 0:
        return None
    memory = _NvmlMemory()
    if nvml.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(memory)) != 0:
        return None
    return {"total": memory.total, "free": memory.free, "used": memory.used}


def query(cuda, nvml=None) -> dict:
    """{"gpus": [...], "driver": version} from a loaded libcuda (and optionally NVML), or {"error": ...}."""
    if cuda is None:
        return {"error": "the CUDA driver library (libcuda) is not installed or not visible here"}
    status = cuda.cuInit(0)
    if status == 100:  # CUDA_ERROR_NO_DEVICE: e.g. CUDA_VISIBLE_DEVICES="" or no GPU given to the container
        return {"gpus": [], "driver": None}
    if status != 0:
        return {"error": f"cuInit failed with CUDA error {status}"}
    version = ctypes.c_int()
    driver = None
    if cuda.cuDriverGetVersion(ctypes.byref(version)) == 0 and version.value:
        driver = f"CUDA {version.value // 1000}.{version.value % 1000 // 10}"
    count = ctypes.c_int()
    if cuda.cuDeviceGetCount(ctypes.byref(count)) != 0:
        return {"error": "cuDeviceGetCount failed"}

    nvml_ok = nvml is not None and nvml.nvmlInit_v2() == 0
    gpus = []
    for ordinal in range(count.value):
        device = ctypes.c_int()
        if cuda.cuDeviceGet(ctypes.byref(device), ordinal) != 0:
            continue
        name = ctypes.create_string_buffer(256)
        cuda.cuDeviceGetName(name, 256, device)
        name = name.value.decode(errors="replace")
        total = ctypes.c_size_t()
        total = total.value if cuda.cuDeviceTotalMem_v2(ctypes.byref(total), device) == 0 else None

        def attribute(code):
            value = ctypes.c_int()
            return value.value if cuda.cuDeviceGetAttribute(ctypes.byref(value), code, device) == 0 else None

        major, minor = attribute(ATTRIBUTE_CC_MAJOR), attribute(ATTRIBUTE_CC_MINOR)
        mig = " MIG " in f" {name} "
        raw = ctypes.create_string_buffer(16)
        getter = getattr(cuda, "cuDeviceGetUuid_v2", None) or getattr(cuda, "cuDeviceGetUuid", None)
        uuid = format_uuid(raw.raw, mig) if getter is not None and getter(raw, device) == 0 else None
        memory = nvml_memory(nvml, uuid) if nvml_ok and uuid and not mig else None
        gpus.append({
            "vendor": "NVIDIA", "name": name, "index": ordinal, "uuid": uuid, "mig": mig,
            "memory_total": total, "memory_free": memory["free"] if memory else None,
            "memory_used": memory["used"] if memory else None,
            "compute_capability": f"{major}.{minor}" if major is not None and minor is not None else None,
            "multiprocessors": attribute(ATTRIBUTE_MULTIPROCESSORS),
            "unified_memory": False, "driver": driver, "source": "CUDA driver" + (" + NVML" if memory else ""),
        })
    if nvml_ok:
        nvml.nvmlShutdown()
    return {"gpus": gpus, "driver": driver}


if __name__ == "__main__":
    try:
        result = query(load(CUDA_LIBRARIES), load(NVML_LIBRARIES))
    except Exception as e:  # noqa: BLE001 - reported to the backend, which shows it
        result = {"error": f"{type(e).__name__}: {e}"}
    json.dump(result, sys.stdout)
