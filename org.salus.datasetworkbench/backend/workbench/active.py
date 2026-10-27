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

    The root is the `root` setting of the plugin (`RootSettings`), which the user sets in Salus' settings. The plugin does not ask for it
    itself: until it is set, the frontends say where to set it. A change in the settings is followed the next time `status()` is asked."""

    def __init__(self, settings: RootSettings, notify: Callable[[dict], None]):
        self.settings = settings
        self.notify = notify
        self.error: str | None = None  # why the set folder cannot be used
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
        """Uses the root from the settings, if there is one."""
        with self._lock:
            stored = self.settings.stored()
            if stored:
                self._activate(stored)

    def _activate(self, text: str) -> bool:
        try:
            path = self.settings.normalize(text)
            wb = Workbench(path, self.notify)  # creates the folder with sources/ and datasets/ if needed
        except WorkbenchError as e:
            self.error = e.message
            log.warning(self.error)
            return False
        except OSError as e:
            self.error = f"The folder {text} cannot be used: {e.strerror or e}."
            log.warning(self.error)
            return False
        self._wb, self.root, self.error = wb, path, None
        threading.Thread(target=wb.uploads.clean_stale_parts, daemon=True, name="clean-parts").start()
        log.info("Workbench folder: %s", path)
        return True

    def status(self) -> dict:
        """What the frontend needs to know. Also follows a change of the setting."""
        with self._lock:
            stored = self.settings.stored()
            if stored and (self.root is None or self.settings.normalize_or_none(stored) != self.root):
                if self._activate(stored):
                    self.notify({"type": "config_changed"})
            elif not stored and self._wb is not None:
                self._wb, self.root = None, None  # the setting was cleared
                self.notify({"type": "config_changed"})
            return {"root": stored, "active": self._wb is not None, "error": self.error if stored else None}
