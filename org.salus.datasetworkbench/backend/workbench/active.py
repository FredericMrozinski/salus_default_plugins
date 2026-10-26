"""The workbench that is in use, or the reason that there is none yet."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from .service import Workbench
from .settings import RootSettings
from .util import WorkbenchError

log = logging.getLogger("workbench")
NOT_SET_UP = 412  # "precondition failed": the answer of every data route while the root folder is not set up


class Active:
    """Holds the `Workbench` for the configured root folder. Routes use it as if it were the workbench (`wb.catalog`, `wb.datasets` …);
    while there is no usable root that raises a WorkbenchError with status 412, which the frontend answers with its setup screen.

    The root comes from the plugin's config file (`RootSettings`). It can be changed while running with `use()`: the new folder is
    checked and prepared first, so a bad choice leaves the working one in place."""

    def __init__(self, settings: RootSettings | None, notify: Callable[[dict], None], unavailable: str | None = None):
        self.settings = settings
        self.notify = notify
        self.error: str | None = unavailable  # why the stored root (or the config directory) cannot be used
        self.root: Path | None = None
        self._wb: Workbench | None = None
        self._lock = threading.RLock()

    def __getattr__(self, name):
        wb = self.__dict__.get("_wb")
        if wb is None:
            raise WorkbenchError("The workbench folder is not set up yet.", NOT_SET_UP)
        return getattr(wb, name)

    @property
    def active(self) -> bool:
        return self._wb is not None

    # ---- starting and following the setting
    def load(self) -> None:
        """Uses the root from the config file, if there is one."""
        with self._lock:
            stored = self.settings.stored() if self.settings else None
            if stored:
                self._activate(Path(stored))

    def _activate(self, path: Path) -> bool:
        try:
            wb = Workbench(path, self.notify)  # creates sources/ and datasets/ if needed
        except OSError as e:
            self.error = f"The folder {path} cannot be used: {e.strerror or e}."
            log.warning(self.error)
            return False
        self._wb, self.root, self.error = wb, path, None
        threading.Thread(target=wb.uploads.clean_stale_parts, daemon=True, name="clean-parts").start()
        log.info("Workbench folder: %s", path)
        return True

    # ---- changing it
    def use(self, text) -> Path:
        """Makes `text` the workbench folder: checked, prepared (created if needed), activated, and written to the config file."""
        if self.settings is None:
            raise WorkbenchError(self.error or "This Salus has no config directory for plugins, so the folder cannot be stored.", 500)
        with self._lock:
            path = self.settings.normalize(text)
            problem = self.settings.check(str(path))["problem"]
            if problem:
                raise WorkbenchError(problem)
            try:
                wb = Workbench(path, self.notify)
            except OSError as e:
                raise WorkbenchError(f"The folder cannot be used: {e.strerror or e}.") from None
            try:
                self.settings.save(path)
            except OSError as e:
                raise WorkbenchError(f"The choice could not be stored in the plugin's config: {e.strerror or e}.", 500) from None
            self._wb, self.root, self.error = wb, path, None
            threading.Thread(target=wb.uploads.clean_stale_parts, daemon=True, name="clean-parts").start()
            log.info("Workbench folder changed to %s", path)
        self.notify({"type": "config_changed"})
        return path

    def status(self) -> dict:
        """What the frontend needs to know. Also follows a config file that was edited by hand."""
        with self._lock:
            stored = self.settings.stored() if self.settings else None
            if stored and (self.root is None or Path(stored) != self.root):
                changed = self._activate(Path(stored))
                if changed:
                    self.notify({"type": "config_changed"})
            return {
                "root": stored,
                "active": self._wb is not None,
                "error": self.error,
                "suggestion": str(RootSettings.suggestion()),
                "suggestion_exists": RootSettings.suggestion().is_dir(),
                "config_file": str(self.settings.config.path / "settings.json") if self.settings and hasattr(self.settings.config, "path") else None,
            }
