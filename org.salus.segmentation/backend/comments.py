"""Comments on shapes, so that users can discuss annotations.

Each image has one file, `<save folder>/comments/<image path>.json`:

    {"image": "case1/slide.svs", "shapes": {"<shape id>": [entry, ...]}}

An entry is a comment, {id, author, time, text, edited?}, or a resolve or reopen, {id, kind: "resolved" | "reopened",
author, time}, in the order they happened. A conversation is resolved while its last entry is a resolve (anyone can
resolve and reopen): its comments stay, but the class editor no longer opens it when the shape is picked. A new comment
opens it again.

The comments are kept apart from the GeoJSON so that undo and redo of the drawing leave them alone, and so that
users, each with a backend of their own, never write over each other's comments: every change re-reads the file under
a lock. The author is the system user running the backend ($USER); only the author can edit or delete a comment
(an edited one gets an "edited" time). Comments stay with the shape id; a shape merged
into another by the brush gives up its id, and with it its comments (a deleted shape keeps them, for undo).
"""
from __future__ import annotations

import contextlib
import fcntl
import getpass
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from errors import ViewerError
from project import Project, check_rel, write_json_atomic

COMMENTS_DIR = "comments"
MAX_TEXT = 10_000


def current_user() -> str:
    """The system user, as $USER names it (getpass falls back to LOGNAME and the password database)."""
    return os.environ.get("USER") or getpass.getuser()


class Comments:
    def __init__(self, project: Project, image: str):
        self.image = check_rel(image)
        self.folder = project.save / COMMENTS_DIR
        self.file: Path = self.folder / f"{self.image}.json"

    @contextlib.contextmanager
    def _locked(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(self.folder / ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _load(self) -> dict[str, list[dict]]:
        """The file's content, checked: shape id -> its entries, oldest first. (A "resolved" map written by an earlier
        version becomes a resolve entry.)"""
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as e:
            raise ViewerError(f"The comments of this image cannot be read: {e}") from None
        data = data if isinstance(data, dict) else {}
        shapes = data.get("shapes") if isinstance(data.get("shapes"), dict) else {}
        shapes = {str(k): [c for c in v if isinstance(c, dict)] for k, v in shapes.items() if isinstance(v, list)}
        for k, v in (data.get("resolved") if isinstance(data.get("resolved"), dict) else {}).items():
            entries = shapes.get(str(k))
            if entries and isinstance(v, dict) and entries[-1].get("kind") != "resolved":
                entries.append({"id": uuid.uuid4().hex, "kind": "resolved", "author": v.get("by"), "time": v.get("time")})
        return shapes

    def all(self) -> dict[str, list[dict]]:
        """Shape id -> its entries, oldest first: comments, and resolve and reopen entries."""
        return self._load()

    def resolved(self) -> dict[str, dict]:
        """Shape id -> its last entry, for the resolved conversations (those whose last entry is a resolve)."""
        return {k: v[-1] for k, v in self._load().items() if v and v[-1].get("kind") == "resolved"}

    def resolve(self, shape_id: str, resolved: bool = True, user: str | None = None) -> dict | None:
        """Resolves a shape's conversation (or opens it again) with an entry in it. Returns the new entry, or None if
        the conversation was resolved (or open) already."""
        with self._locked():
            shapes = self._load()
            entries = shapes.get(str(shape_id))
            if not any(not is_event(c) for c in entries or []):
                raise ViewerError("There are no comments on this shape to resolve.", 404)
            if (entries[-1].get("kind") == "resolved") == bool(resolved):
                return None
            entry = {"id": uuid.uuid4().hex, "kind": "resolved" if resolved else "reopened", "author": user or current_user(), "time": now()}
            entries.append(entry)
            self._save(shapes)
            return entry

    def add(self, shape_id: str, text, author: str | None = None) -> dict:
        """Adds a comment; on a resolved conversation it opens the conversation again (it is the last entry then)."""
        comment = {"id": uuid.uuid4().hex, "author": author or current_user(), "time": now(), "text": check_text(text)}
        with self._locked():
            shapes = self._load()
            shapes.setdefault(str(shape_id), []).append(comment)
            self._save(shapes)
        return comment

    def edit(self, shape_id: str, comment_id: str, text, author: str | None = None) -> dict:
        """Changes the text of one's own comment."""
        text = check_text(text)
        with self._locked():
            shapes = self._load()
            comment = self._own(shapes, shape_id, comment_id, author, "edit")
            if comment["text"] != text:
                comment["text"], comment["edited"] = text, now()
                self._save(shapes)
            return comment

    def delete(self, shape_id: str, comment_id: str, author: str | None = None) -> None:
        """Deletes one's own comment (for good: there is no undo for comments). With the last comment, the resolve and
        reopen entries go too."""
        with self._locked():
            shapes = self._load()
            comment = self._own(shapes, shape_id, comment_id, author, "delete")
            entries = shapes[str(shape_id)]
            entries.remove(comment)
            if not any(not is_event(c) for c in entries):
                del shapes[str(shape_id)]
            self._save(shapes)

    def _own(self, shapes, shape_id, comment_id, author, action) -> dict:
        comment = next((c for c in shapes.get(str(shape_id), []) if c.get("id") == comment_id and not is_event(c)), None)
        if comment is None:
            raise ViewerError("This comment no longer exists.", 404)
        if comment.get("author") != (author or current_user()):
            raise ViewerError(f"Only {comment.get('author') or 'its author'} can {action} this comment.", 403)
        return comment

    def _save(self, shapes: dict) -> None:
        write_json_atomic(self.file, {"image": self.image, "shapes": shapes})


def is_event(entry: dict) -> bool:
    """A resolve or reopen entry, not a comment."""
    return entry.get("kind") in ("resolved", "reopened")

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_text(text) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ViewerError("Write a comment first.")
    text = text.strip()
    if len(text) > MAX_TEXT:
        raise ViewerError(f"A comment can be at most {MAX_TEXT} characters long.")
    return text
