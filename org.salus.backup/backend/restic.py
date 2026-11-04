"""Running restic: finding the program, passing the password, reading its JSON output.

The password goes to restic on its standard input (restic reads it from there when stdin is not a terminal); it is
never put on the command line or into the environment, and it is never stored.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from errors import BackupError

BIN = Path(__file__).resolve().parent / "bin"

# restic's exit codes (https://restic.readthedocs.io/en/stable/075_scripting.html#exit-codes)
EXIT_INCOMPLETE = 3  # backup: some files could not be read; the snapshot exists
EXIT_NO_REPOSITORY = 10
EXIT_LOCKED = 11
EXIT_WRONG_PASSWORD = 12
EXIT_INTERRUPTED = 130


def binary() -> str:
    """The restic program: the one setup downloaded into backend/bin, else one on the PATH."""
    name = "restic.exe" if sys.platform == "win32" else "restic"
    own = BIN / name
    if own.is_file():
        return str(own)
    found = shutil.which("restic")
    if found:
        return found
    raise BackupError("restic is not installed. Run backend/setup.sh (or setup.py) of the backup plugin first.", 503)


def environment() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}  # nothing from outside decides for us
    env["RESTIC_PROGRESS_FPS"] = "2"  # status lines per second with --json
    return env


def explain(code: int, stderr: str) -> str:
    text = " ".join(line.strip() for line in stderr.strip().splitlines()[-3:] if line.strip())
    if code == EXIT_WRONG_PASSWORD or "wrong password" in stderr:
        return "Wrong password for this bundle."
    if code == EXIT_NO_REPOSITORY:
        return "The bundle's repository is missing or damaged."
    if code == EXIT_LOCKED:
        return "The bundle is locked by another restic process. Wait until it has finished."
    return text or f"restic failed (exit code {code})."


class Process:
    """One restic run. `lines` gets every JSON line restic prints (progress, results)."""

    def __init__(self, repo: Path, args: list[str], password: str, lines: Callable[[dict], None] | None = None):
        self.repo, self.args, self.password, self.lines = repo, args, password, lines
        self.popen: subprocess.Popen | None = None
        self.stopped = False

    def run(self) -> tuple[int, list[dict], str]:
        """Runs restic; returns (exit code, the JSON lines it printed, what it wrote to stderr)."""
        command = [binary(), "--repo", str(self.repo), "--json", *self.args]
        self.popen = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env=environment(), text=True, encoding="utf-8", errors="replace",
                                      creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            # init asks twice; other commands read one line.
            self.popen.stdin.write(f"{self.password}\n{self.password}\n")
            self.popen.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        stderr_chunks: list[str] = []

        def read_stderr():
            for chunk in self.popen.stderr:
                stderr_chunks.append(chunk)

        reader = threading.Thread(target=read_stderr, daemon=True)
        reader.start()
        out: list[dict] = []
        for line in self.popen.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if self.lines and isinstance(value, dict):
                self.lines(value)
            out.append(value)
        code = self.popen.wait()
        reader.join(5)
        self.popen.stdout.close()
        self.popen.stderr.close()
        return code, out, "".join(stderr_chunks)

    def stop(self) -> None:
        """Asks restic to stop (like Ctrl+C): it ends without writing a snapshot and removes its lock."""
        self.stopped = True
        if self.popen and self.popen.poll() is None:
            if sys.platform == "win32":
                self.popen.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.popen.send_signal(signal.SIGINT)


def run(repo: Path, args: list[str], password: str) -> list:
    """Runs a short restic command and returns its JSON output; raises BackupError with a readable message."""
    code, out, stderr = Process(repo, args, password).run()
    if code != 0:
        raise BackupError(explain(code, stderr), 401 if code == EXIT_WRONG_PASSWORD else 400)
    return out
