"""Timing log for the smart brush: where the time of a dab, a stroke's start or its commit goes.

Every brush request (or streamed message) is wrapped in `measure(label)`; the code it runs marks its parts with
`step(name)`. When the request ends, one line goes to the log (the backend's stderr, next to Salus' request lines):

    timing dab (stroke 3f2a, first, 1 dab): 2134 ms | import 1630, settings 12, open 3, read 41, blur 230, grow 180, ...

Steps that run several times in a request (several dabs) are added up; `x3` after a step says how often it ran. Time not
in any step is shown as `other`. One-off costs (importing torch, scipy or numba, compiling numba functions, loading a
model) show up in the first request that pays them.

Set the environment variable SEGMENTATION_TIMING=0 to turn the log off.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time

log = logging.getLogger("segmentation.timing")
ENABLED = os.environ.get("SEGMENTATION_TIMING", "1") != "0"
_local = threading.local()


def _current():
    return getattr(_local, "steps", None)


@contextlib.contextmanager
def step(name: str):
    """Adds the time of the block to step `name` of the request being measured (nothing outside a request)."""
    steps = _current()
    if steps is None or not ENABLED:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        took, count = steps.get(name, (0.0, 0))
        steps[name] = (took + time.perf_counter() - start, count + 1)


@contextlib.contextmanager
def measure(label: str, **info):
    """Measures a request and logs one line with its total and steps. `info` (e.g. stroke, dabs) goes into the line;
    the block may add to it through the yielded dict. Nested inside another request, its steps also count there."""
    if not ENABLED:
        yield {}
        return
    outer = _current()
    steps: dict[str, tuple[float, int]] = {}
    _local.steps = steps
    start = time.perf_counter()
    try:
        yield info
    finally:
        total = time.perf_counter() - start
        _local.steps = outer
        if outer is not None:
            took, count = outer.get(label, (0.0, 0))
            outer[label] = (took + total, count + 1)
        inside = sum(t for t, _ in steps.values())
        parts = [f"{name} {t * 1000:.0f}" + (f" x{c}" if c > 1 else "") for name, (t, c) in steps.items()]
        if total - inside >= 0.0005:
            parts.append(f"other {(total - inside) * 1000:.0f}")
        details = ", ".join(f"{k} {v}" if v is not True else k for k, v in info.items() if v not in (None, False))
        log.info("timing %s%s: %.0f ms | %s", label, f" ({details})" if details else "", total * 1000, ", ".join(parts))


def once(name: str, start: float) -> None:
    """Logs a one-off cost (e.g. an import) that took since `start` (time.perf_counter())."""
    if ENABLED:
        log.info("timing %s: %.0f ms (once)", name, (time.perf_counter() - start) * 1000)
