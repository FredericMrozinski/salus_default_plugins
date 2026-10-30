"""Annotation projects: where the images are, where the annotations go, and the classes.

A project lives in its save folder:

    <save>/project.json                     name, root folder, file types, classes, export settings
    <save>/annotations/<image path>.geojson one file per annotated image (<image path> is relative to the root)

The images themselves stay where they are; only annotations are written.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path

from errors import ViewerError

PROJECT_FILE = "project.json"
ANNOTATIONS_DIR = "annotations"
FORMAT_VERSION = 1
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
DOWNSAMPLES = (1, 2, 4, 8, 16, 32)  # the resolutions pixel maps can be exported at (1 = full resolution)


def write_json_atomic(path: Path, data) -> None:
    """Writes JSON so that a crash never leaves a half-written file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.chmod(tmp, 0o644)  # mkstemp makes the file private; these files are as readable as any other
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def normalize_extensions(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,\s;]+", value)
    if not isinstance(value, list):
        raise ViewerError("Give the file types as a list such as .svs, .tif.")
    out = []
    for ext in value:
        ext = str(ext).strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        if not re.fullmatch(r"\.[a-z0-9]+(\.[a-z0-9]+)?", ext):
            raise ViewerError(f"'{ext}' is not a file type.")
        if ext not in out:
            out.append(ext)
    if not out:
        raise ViewerError("Give at least one file type, such as .svs.")
    return out


def check_color(value, label: str) -> str:
    if not isinstance(value, str) or not HEX_COLOR.match(value.strip()):
        raise ViewerError(f"The {label} must be a color like #ff8800.")
    return value.strip().lower()


def check_rel(rel) -> str:
    """An image path relative to the root: no absolute paths, no .. and no backslashes."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or any(p in ("", ".", "..") for p in rel.split("/")):
        raise ViewerError("Invalid image path.")
    return rel


def absolute_dir(value, label: str) -> Path:
    path = Path(str(value or "").strip()).expanduser()
    if not str(value or "").strip() or not path.is_absolute():
        raise ViewerError(f"Enter the full path of the {label} (it starts with / or ~).")
    return path


class Project:
    def __init__(self, save: Path, data: dict):
        self.save = save
        self.data = data
        self.lock = threading.RLock()
        self._images: list[str] | None = None

    # -- creating and opening
    @classmethod
    def create(cls, name: str, root, extensions, save) -> Project:
        name = str(name or "").strip()
        if not name:
            raise ViewerError("Give the project a name.")
        root = absolute_dir(root, "image folder")
        save = absolute_dir(save, "save folder")
        if not root.is_dir():
            raise ViewerError("The image folder does not exist on the server.")
        if (save / PROJECT_FILE).exists():
            raise ViewerError("There is a project in that save folder already. Open it instead.")
        if save.exists() and not save.is_dir():
            raise ViewerError("The save folder is a file.")
        if save.resolve() == root.resolve():
            raise ViewerError("Choose a save folder other than the image folder, so that annotations and images stay apart.")
        save.mkdir(parents=True, exist_ok=True)
        data = {"format": FORMAT_VERSION, "id": uuid.uuid4().hex, "name": name, "root": str(root),
                "extensions": normalize_extensions(extensions), "classes": [], "next_class_id": 1}
        project = cls(save, data)
        project.write()
        return project

    @classmethod
    def open(cls, save) -> Project:
        save = absolute_dir(save, "save folder")
        file = save / PROJECT_FILE
        if not file.is_file():
            raise ViewerError("There is no project in that folder (no project.json).", 404)
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ViewerError(f"The project file cannot be read: {e}") from None
        if not isinstance(data, dict) or "id" not in data or "root" not in data:
            raise ViewerError("The project file is not a segmentation project.")
        data.setdefault("classes", [])
        data.setdefault("next_class_id", max([c["id"] for c in data["classes"]] + [0]) + 1)
        data.setdefault("extensions", [".svs"])
        # Projects of the first version kept an export color in each class; it is an export setting now.
        for c in data["classes"]:
            if "export_color" in c:
                data.setdefault("export", {}).setdefault("colors", {})[str(c["id"])] = c.pop("export_color")
        return cls(save, data)

    def write(self) -> None:
        with self.lock:
            write_json_atomic(self.save / PROJECT_FILE, self.data)

    # -- what the frontends see
    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def root(self) -> Path:
        return Path(self.data["root"])

    def info(self) -> dict:
        return {"id": self.id, "name": self.data["name"], "root": self.data["root"], "save": str(self.save),
                "extensions": self.data["extensions"], "classes": self.data["classes"],
                "root_missing": not self.root.is_dir()}

    # -- images
    def images(self, rescan: bool = False) -> list[str]:
        """Paths of the images below the root (relative, with /), sorted."""
        with self.lock:
            if self._images is None or rescan:
                found = []
                extensions = tuple(self.data["extensions"])
                for directory, subdirs, files in os.walk(self.root, onerror=lambda e: None):
                    subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
                    rel_dir = Path(directory).relative_to(self.root)
                    for name in files:
                        if not name.startswith(".") and name.lower().endswith(extensions):
                            found.append((rel_dir / name).as_posix())
                self._images = sorted(found, key=str.lower)
            return list(self._images)

    def image_path(self, rel: str) -> Path:
        """The file of an image of this project; refuses anything outside the root."""
        path = self.root / check_rel(rel)
        if not path.is_file():
            raise ViewerError("That image does not exist (any more).", 404)
        return path

    def annotation_file(self, rel: str) -> Path:
        return self.save / ANNOTATIONS_DIR / f"{check_rel(rel)}.geojson"

    # -- classes
    def cls(self, class_id: int) -> dict:
        for c in self.data["classes"]:
            if c["id"] == class_id:
                return c
        raise ViewerError("No such class.", 404)

    def add_class(self, name, color) -> dict:
        with self.lock:
            name = self._check_name(name)
            color = check_color(color, "drawing color")
            new = {"id": self.data["next_class_id"], "name": name, "color": color}
            self.data["next_class_id"] += 1
            self.data["classes"].append(new)
            self.write()
            return new

    def update_class(self, class_id: int, changes: dict) -> dict:
        """Name and drawing color can change; classes are never deleted."""
        with self.lock:
            target = self.cls(class_id)
            if "name" in changes:
                target["name"] = self._check_name(changes["name"], skip=class_id)
            if "color" in changes:
                target["color"] = check_color(changes["color"], "drawing color")
            self.write()
            return target

    # -- export settings: which color each class gets in pixel maps, which class wins where shapes overlap, ...
    def export_settings(self) -> dict:
        """The stored export settings, completed for classes added since: {background, classes: [{id, name, color, include}]
        in priority order (the first wins where shapes overlap), folder, downsample}."""
        with self.lock:
            stored = self.data.get("export", {})
            colors = stored.get("colors", {})
            excluded = set(stored.get("excluded", []))
            ids = [c["id"] for c in self.data["classes"]]
            order = [i for i in stored.get("order", []) if i in ids] + [i for i in ids if i not in stored.get("order", [])]
            classes = []
            for class_id in order:
                c = self.cls(class_id)
                classes.append({"id": class_id, "name": c["name"], "drawing_color": c["color"],
                                "color": colors.get(str(class_id), c["color"]), "include": class_id not in excluded})
            return {"background": stored.get("background", "#000000"), "classes": classes,
                    "folder": stored.get("folder", ""), "downsample": stored.get("downsample", 1)}

    def save_export_settings(self, settings: dict) -> dict:
        """Checks and keeps the settings of an export (they are the defaults of the next one). Returns them completed."""
        with self.lock:
            background = check_color(settings.get("background", "#000000"), "background color")
            ids = {c["id"] for c in self.data["classes"]}
            order, colors, excluded = [], {}, []
            for entry in settings.get("classes", []):
                class_id = int(entry["id"])
                if class_id not in ids or class_id in order:
                    raise ViewerError("Unknown or repeated class.")
                order.append(class_id)
                colors[str(class_id)] = check_color(entry.get("color"), f"color of {self.cls(class_id)['name']}")
                if not entry.get("include", True):
                    excluded.append(class_id)
            used = {}
            for class_id in order:
                if class_id in excluded:
                    continue
                color = colors[str(class_id)]
                name = self.cls(class_id)["name"]
                if color == background:
                    raise ViewerError(f"{name} has the background color. Give it another color.")
                if color in used:
                    raise ViewerError(f"{name} and {used[color]} have the same color, so they could not be told apart.")
                used[color] = name
            downsample = int(settings.get("downsample", 1))
            if downsample not in DOWNSAMPLES:
                raise ViewerError("Unsupported resolution.")
            folder = str(settings.get("folder") or "").strip()
            if folder:
                folder = str(absolute_dir(folder, "export folder"))
                if Path(folder).resolve() == self.root.resolve():
                    raise ViewerError("Choose an export folder other than the image folder.")
            self.data["export"] = {"background": background, "colors": colors, "order": order, "excluded": excluded,
                                   "folder": folder, "downsample": downsample}
            self.write()
            return self.export_settings()

    def _check_name(self, name, skip: int | None = None) -> str:
        name = str(name or "").strip()
        if not name:
            raise ViewerError("Give the class a name.")
        if len(name) > 100:
            raise ViewerError("The name is too long.")
        if any(c["name"].lower() == name.lower() and c["id"] != skip for c in self.data["classes"]):
            raise ViewerError("There is a class with that name already.")
        return name


class Registry:
    """The projects this plugin knows (created or opened), kept in a small file in the user's home folder.

    Salus' plugin settings are for settings, not data, so the list lives here.
    """

    def __init__(self, file: Path):
        self.file = file
        self.lock = threading.Lock()

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            return [e for e in data.get("projects", []) if isinstance(e, dict) and "save" in e]
        except (OSError, ValueError, AttributeError):
            return []

    def remember(self, project: Project) -> None:
        with self.lock:
            entries = [e for e in self.load() if e.get("id") != project.id and e.get("save") != str(project.save)]
            entries.append({"id": project.id, "name": project.data["name"], "save": str(project.save)})
            write_json_atomic(self.file, {"projects": entries})

    def forget(self, project_id: str) -> None:
        with self.lock:
            entries = [e for e in self.load() if e.get("id") != project_id]
            write_json_atomic(self.file, {"projects": entries})
