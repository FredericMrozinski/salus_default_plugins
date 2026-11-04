#!/usr/bin/env python3
"""Downloads restic for this machine into backend/bin and checks it against restic's published SHA-256 checksums.

Run it once on the machine that runs Salus (python3 setup.py, or setup.sh). Nothing else needs to be installed:
the backend itself only uses Python's standard library.
"""
import bz2
import hashlib
import io
import platform
import stat
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = "0.19.1"
RELEASE = f"https://github.com/restic/restic/releases/download/v{VERSION}"
BIN = Path(__file__).resolve().parent / "bin"


def asset() -> str:
    system = {"darwin": "darwin", "linux": "linux", "win32": "windows"}.get(sys.platform)
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if not system or not arch:
        sys.exit(f"No restic download for {sys.platform}/{machine}. Install restic yourself (it is then found on the PATH).")
    return f"restic_{VERSION}_{system}_{arch}.{'zip' if system == 'windows' else 'bz2'}"


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def main() -> None:
    name = asset()
    print(f"Downloading {name} …")
    data = fetch(f"{RELEASE}/{name}")
    sums = fetch(f"{RELEASE}/SHA256SUMS").decode()
    expected = next((line.split()[0] for line in sums.splitlines() if line.strip().endswith(name)), None)
    actual = hashlib.sha256(data).hexdigest()
    if expected != actual:
        sys.exit(f"Checksum mismatch for {name} (expected {expected}, got {actual}). Nothing was installed.")
    BIN.mkdir(exist_ok=True)
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(m for m in z.namelist() if m.endswith(".exe"))
            target = BIN / "restic.exe"
            target.write_bytes(z.read(member))
    else:
        target = BIN / "restic"
        target.write_bytes(bz2.decompress(data))
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"restic {VERSION} installed in {target} (checksum verified).")


if __name__ == "__main__":
    main()
