#!/usr/bin/env python3
"""Settings backend: nothing to do.

The settings plugin works through the framework's settings API (see frontend/script.js); every plugin needs a
backend process, so this one only keeps the connection to Salus open.
"""
import salus_sdk as salus

app = salus.Plugin()

if __name__ == "__main__":
    app.run()
