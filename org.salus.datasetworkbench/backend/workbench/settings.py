"""Where the workbench keeps its data: the root folder, stored in the plugin's config directory (settings.json)."""
from __future__ import annotations

import os
from pathlib import Path

from .util import WorkbenchError

SETTINGS_FILE = "settings.json"
LEGACY_FOLDER = "SalusWorkbench"  # what the root used to be without any setting; offered as the suggestion


class RootSettings:
    """`config` is the plugin's config directory (the SDK's `app.config`): `read_json(name, default)` and `write_json(name, value)`."""

    def __init__(self, config):
        self.config = config

    # ---- the stored setting
    def stored(self) -> str | None:
        """The root as written in the config file, or None if none was ever set."""
        data = self.config.read_json(SETTINGS_FILE, default={})
        root = data.get("root") if isinstance(data, dict) else None
        return root if isinstance(root, str) and root.strip() else None

    def save(self, root: Path) -> None:
        data = self.config.read_json(SETTINGS_FILE, default={})
        data = dict(data) if isinstance(data, dict) else {}
        data["root"] = str(root)  # other settings, if there ever are any, stay
        self.config.write_json(SETTINGS_FILE, data)

    @staticmethod
    def suggestion() -> Path:
        return Path.home() / LEGACY_FOLDER

    # ---- what a path would mean
    @staticmethod
    def normalize(text) -> Path:
        """The folder the user typed as an absolute path. Raises WorkbenchError if it cannot be a root."""
        if not isinstance(text, str) or not text.strip():
            raise WorkbenchError("Enter the folder on the server where the workbench keeps its data.")
        text = text.strip()
        if "\0" in text:
            raise WorkbenchError("That is not a valid path.")
        path = Path(os.path.expanduser(text))
        if not path.is_absolute():
            raise WorkbenchError("Enter the full path of the folder, starting with / (or ~ for your home folder).")
        path = Path(os.path.normpath(path))
        if path == Path(path.anchor):
            raise WorkbenchError("The top of the file system cannot be the workbench folder; choose or create a folder for it.")
        return path

    def check(self, text) -> dict:
        """Tells what choosing `text` would do, for showing next to the input. `problem` is set when it cannot be used."""
        try:
            path = self.normalize(text)
        except WorkbenchError as e:
            return {"path": None, "problem": e.message}
        info = {"path": str(path), "exists": path.exists(), "will_create": False, "has_workbench": False, "has_files": False, "problem": None}
        if path.exists():
            if not path.is_dir():
                info["problem"] = "That is a file, not a folder."
                return info
            if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                info["problem"] = "The server may not read and write in that folder."
                return info
            entries = [p.name for p in path.iterdir() if not p.name.startswith(".")]
            info["has_workbench"] = "sources" in entries or "datasets" in entries
            info["has_files"] = bool(entries) and not info["has_workbench"]
        else:
            ancestor = next((p for p in path.parents if p.exists()), None)
            if ancestor is None or not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
                info["problem"] = f"The folder cannot be created: the server may not write in {ancestor or 'its parent folder'}."
                return info
            info["will_create"] = True
        return info
