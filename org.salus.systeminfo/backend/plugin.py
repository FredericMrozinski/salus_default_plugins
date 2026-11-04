#!/usr/bin/env python3
"""System info backend: the hardware of the machine this backend runs on (see sysinfo.py).

  GET /info     {host, os, python, pid, memory: {total, available, used_percent},
                 cpu: {model, architecture, physical_cores, logical_cores}, gpus: [...]}   sizes in bytes
"""
import logging

import salus_sdk as salus
import sysinfo

app = salus.Plugin()
NO_CACHE = [("cache-control", "no-store")]


@app.route("GET", "/info")
def info(req):
    # A sync handler runs in a thread, so the external tools (system_profiler takes a moment) do not block the backend.
    return salus.Response.json(sysinfo.collect(), headers=NO_CACHE)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run()
