"""Where the workbench keeps its data: the `root` setting of the plugin (see configurable.toml), kept by Salus."""
from __future__ import annotations

import os
from pathlib import Path

from .util import WorkbenchError


class RootSettings:
    """`config` is the plugin's settings (the SDK's `app.config`); only `get_blocking(key)` is used."""

    def __init__(self, config):
        self.config = config

    def stored(self) -> str | None:
        """The folder as set in the settings, or None if none was set."""
        root = self.config.get_blocking("root")
        return root if isinstance(root, str) and root.strip() else None

    @classmethod
    def normalize_or_none(cls, text) -> Path | None:
        try:
            return cls.normalize(text)
        except WorkbenchError:
            return None

    @staticmethod
    def normalize(text) -> Path:
        """The folder as an absolute path. Raises WorkbenchError if it cannot be a root."""
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
