#!/usr/bin/env python3
"""Terminal backend: one shell per open terminal.

The frontend (xterm.js) sends keystrokes as raw bytes on the channel `input` and control messages on `control`
({"type": "start" | "resize", "cols", "rows"}; "start" is ignored while a shell runs). The shell's output goes back as raw bytes on `output`; the end of the shell
(or a failure to start it) is reported on `control` ({"type": "exit", "code"} / {"type": "error", "message"}), and
`{"type": "ready"}` tells a frontend that the backend is there, so it can ask for its shell.
The shell itself runs in `session.PtySession`. A terminal ends when its tab is closed.

A shell has the rights of the user running Salus, for everyone who can use this plugin.
"""
import asyncio
import logging
import os
from pathlib import Path

import salus_sdk as salus
from session import PtySession

log = logging.getLogger("terminal")
app = salus.Plugin()


class Terminal:
    """The shell of one frontend and the queue that carries its output to the browser."""

    def __init__(self, frontend_id: int):
        self.frontend_id = frontend_id
        self.session: PtySession | None = None
        self.output: asyncio.Queue = asyncio.Queue()
        self.sender = asyncio.create_task(self._send_output())

    async def _send_output(self) -> None:
        # One task per terminal keeps the output in order; the shell is slowed down while the browser lags behind.
        while True:
            item = await self.output.get()
            try:
                if isinstance(item, bytes):
                    await app.send(item, channel="output", to=self.frontend_id)
                    if self.session is not None:
                        self.session.output_sent(len(item))
                else:
                    await app.send(salus.Payload.json(item), channel="control", to=self.frontend_id)
            except Exception:
                log.debug("terminal %s: could not deliver", self.frontend_id, exc_info=True)

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None
        self.sender.cancel()


terminals: dict[int, Terminal] = {}


def resolve_shell(setting: str) -> str:
    for candidate in (setting, os.environ.get("SHELL", ""), "/bin/sh"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise OSError("no shell found; set one under Settings → Terminal")


def start_session(terminal: Terminal, settings: dict, columns: int, rows: int) -> None:
    shell = resolve_shell(str(settings.get("shell") or ""))
    folder = str(settings.get("start_directory") or "")
    cwd = folder if folder and Path(folder).is_dir() else str(Path.home())
    env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
    terminal.session = PtySession(
        [shell, "-l"], cwd, columns, rows,
        on_output=terminal.output.put_nowait,
        on_exit=lambda code: shell_ended(terminal, code),
        env=env)
    log.info("terminal %s: %s started in %s (pid %s)", terminal.frontend_id, shell, cwd, terminal.session.pid)


def shell_ended(terminal: Terminal, code: int) -> None:
    terminal.session = None  # a later "start" begins a new shell
    terminal.output.put_nowait({"type": "exit", "code": code})


def size_of(message: dict) -> tuple[int, int]:
    def clamp(value, default):
        return max(1, min(int(value), 1000)) if isinstance(value, (int, float)) else default
    return clamp(message.get("cols"), 80), clamp(message.get("rows"), 24)


@app.channel("control")
async def control(msg):
    message = msg.payload.as_json()
    if not isinstance(message, dict):
        return
    terminal = terminals.get(msg.sender)
    if terminal is None:
        terminal = terminals[msg.sender] = Terminal(msg.sender)
    columns, rows = size_of(message)
    kind = message.get("type")
    if kind == "start" and terminal.session is None:
        settings = await app.config.all()
        if terminal.session is not None:  # another "start" was faster while the settings were read
            return
        try:
            start_session(terminal, settings, columns, rows)
        except OSError as e:
            terminal.output.put_nowait({"type": "error", "message": f"The shell could not be started: {e.strerror or e}"})
    elif kind == "resize" and terminal.session is not None:
        terminal.session.resize(columns, rows)


@app.channel("input")
async def keys(msg):
    terminal = terminals.get(msg.sender)
    if terminal is not None and terminal.session is not None:
        terminal.session.write(msg.payload.as_bytes())


@app.on_frontend_attached
async def attached(frontend_id):
    # A "start" sent by the frontend while this backend was still starting up is lost: say when to ask (again).
    await app.send(salus.Payload.json({"type": "ready"}), channel="control", to=frontend_id)


@app.on_frontend_detached
async def detached(frontend_id):
    terminal = terminals.pop(frontend_id, None)
    if terminal is not None:
        terminal.close()
        log.info("terminal %s closed", frontend_id)


if __name__ == "__main__":
    app.run()
