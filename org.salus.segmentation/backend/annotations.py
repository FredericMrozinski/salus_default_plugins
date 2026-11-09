"""The annotations of one image: polygons with a class, stored as GeoJSON in the project's save folder.

Coordinates are pixels of the full-resolution image (x to the right, y down), like QuPath's GeoJSON export, so the files
can be read by QuPath and by scripts. Shapes of different classes may overlap; each shape is independent.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from shapely import make_valid
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from errors import ViewerError
from project import Project, write_json_atomic

UNDO_LIMIT = 50
DECIMALS = 2  # coordinates are written with this many decimals (1/100 px)


def polygonal(geometry: BaseGeometry) -> BaseGeometry | None:
    """Only the area of a geometry (lines and points left over by operations are dropped); None if nothing is left."""
    if geometry.is_empty:
        return None
    if geometry.geom_type in ("Polygon", "MultiPolygon"):
        return geometry
    parts = []
    for part in getattr(geometry, "geoms", []):
        if part.geom_type == "Polygon":
            parts.append(part)
        elif part.geom_type == "MultiPolygon":
            parts.extend(part.geoms)
    parts = [p for p in parts if not p.is_empty and p.area > 0]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def rounded(geometry: BaseGeometry) -> dict:
    def walk(value):
        if isinstance(value, (list, tuple)):
            if value and isinstance(value[0], (int, float)):
                return [round(float(v), DECIMALS) for v in value]
            return [walk(v) for v in value]
        return value
    data = mapping(geometry)
    return {"type": data["type"], "coordinates": walk(data["coordinates"])}


def hex_to_rgb(color: str) -> list[int]:
    return [int(color[i:i + 2], 16) for i in (1, 3, 5)]


def parse_points(points) -> list[tuple[float, float]]:
    try:
        out = [(float(p[0]), float(p[1])) for p in points]
    except (TypeError, ValueError, IndexError):
        raise ViewerError("Invalid points.") from None
    if len(out) > 200_000:
        raise ViewerError("Too many points.")
    return out


def check_name(name) -> str | None:
    """A shape name: text up to 200 characters; empty (or not text) is no name."""
    if not isinstance(name, str) or not name.strip():
        return None
    name = " ".join(name.split())
    if len(name) > 200:
        raise ViewerError("The name is too long.")
    return name


def brush_area(points, radius) -> BaseGeometry:
    """The area a round brush of `radius` covers along a line of image pixels."""
    points = parse_points(points)
    if not points or not radius or radius <= 0:
        raise ViewerError("Invalid brush stroke.")
    return (Point(points[0]) if len(points) == 1 else LineString(points)).buffer(float(radius), quad_segs=8)


class Shape:
    __slots__ = ("id", "class_id", "geometry", "name")

    def __init__(self, id: str, class_id: int, geometry: BaseGeometry, name: str | None = None):
        # name: given by the user (or by QuPath, whose GeoJSON keeps it in properties.name); None shows a generic one
        self.id, self.class_id, self.geometry, self.name = id, class_id, geometry, name


class Annotations:
    def __init__(self, project: Project, image: str):
        self.project = project
        self.image = image
        self.file: Path = project.annotation_file(image)
        self.lock = threading.RLock()
        self.shapes: list[Shape] = []
        self.undo_stack: list[list[Shape]] = []
        self.redo_stack: list[list[Shape]] = []
        self._load()

    # -- the file
    def _load(self) -> None:
        if not self.file.is_file():
            return
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ViewerError(f"The annotation file of this image cannot be read: {e}") from None
        names = {c["name"]: c["id"] for c in self.project.data["classes"]}
        for feature in features_of(data):
            props = feature.get("properties") or {}
            class_id = props.get("class_id")
            if class_id is None:  # a file written by another tool: match the class by name
                class_id = names.get((props.get("classification") or {}).get("name"))
            try:
                geometry = polygonal(make_valid(shape(feature["geometry"])))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if geometry is not None and class_id is not None:
                self.shapes.append(Shape(str(feature.get("id") or str(uuid.uuid4())), int(class_id), geometry, check_name(props.get("name"))))

    def save(self) -> None:
        with self.lock:
            if not self.shapes and not self.file.exists():
                return
            write_json_atomic(self.file, self.to_geojson())

    def to_geojson(self) -> dict:
        classes = {c["id"]: c for c in self.project.data["classes"]}
        features = []
        for s in self.shapes:
            c = classes.get(s.class_id)
            props = {"objectType": "annotation", "class_id": s.class_id}
            if s.name:
                props["name"] = s.name  # where QuPath keeps the name of an annotation
            if c:
                props["classification"] = {"name": c["name"], "color": hex_to_rgb(c["color"])}
            features.append({"type": "Feature", "id": s.id, "geometry": rounded(s.geometry), "properties": props})
        return {"type": "FeatureCollection", "image": self.image, "features": features}

    # -- changes (each one can be undone)
    def _checkpoint(self) -> None:
        self.undo_stack.append(list(self.shapes))  # shapes are never changed in place, so a shallow copy is a snapshot
        del self.undo_stack[:-UNDO_LIMIT]
        self.redo_stack.clear()

    def add_polygon(self, points, class_id: int, simplify: float = 0.0) -> str | None:
        """Adds a polygon (a closed ring of image pixels). Self-intersections are repaired. Returns the new id."""
        self.project.cls(class_id)
        points = parse_points(points)
        if len(points) < 3:
            raise ViewerError("A polygon needs at least three points.")
        geometry = polygonal(make_valid(Polygon(points)))
        if geometry is not None and simplify > 0:
            geometry = polygonal(geometry.simplify(simplify, preserve_topology=True))
        if geometry is None or geometry.area < 1e-6:
            return None
        with self.lock:
            self._checkpoint()
            new = Shape(str(uuid.uuid4()), class_id, geometry)
            self.shapes.append(new)
            self.save()
            return new.id

    def add_area(self, geometry: BaseGeometry, class_id: int) -> str | None:
        """Adds an area painted with a brush. Shapes of the same class it overlaps or touches are merged into it
        (the merged shape keeps the id of the first of them). Returns the id of the resulting shape."""
        self.project.cls(class_id)
        geometry = polygonal(make_valid(geometry))
        if geometry is None or geometry.area < 1e-6:
            return None
        with self.lock:
            merged, kept, first = [geometry], [], None
            for s in self.shapes:
                if s.class_id == class_id and s.geometry.intersects(geometry):
                    merged.append(s.geometry)
                    first = first or s
                else:
                    kept.append(s)
            union = polygonal(make_valid(unary_union(merged)))
            if union is None:
                return None
            self._checkpoint()
            # the merged shape keeps the id of the first one, and the first name among them
            name = next((s.name for s in self.shapes if s.name and s.class_id == class_id and s.geometry.intersects(geometry)), None)
            new = Shape(first.id if first else str(uuid.uuid4()), class_id, union, name)
            self.shapes = kept + [new]
            self.save()
            return new.id

    def paint(self, points, radius: float, class_id: int) -> str | None:
        """Paints a brush stroke (a line of image pixels, `radius` wide on each side) with a class; see add_area."""
        return self.add_area(brush_area(points, radius), class_id)

    def erase(self, points, radius: float, skip=()) -> int:
        """Cuts a brush stroke (a line of image pixels, `radius` wide on each side) out of every shape. Returns how many shapes changed."""
        return self.erase_area(brush_area(points, radius), skip)

    def erase_area(self, brush: BaseGeometry, skip=()) -> int:
        """Cuts an area out of every shape but those of the classes in `skip` (the hidden ones). Returns how many shapes changed."""
        with self.lock:
            changed, result = 0, []
            for s in self.shapes:
                if s.class_id in skip or not s.geometry.intersects(brush):
                    result.append(s)
                    continue
                changed += 1
                rest = polygonal(make_valid(s.geometry.difference(brush)))
                if rest is not None:
                    result.append(Shape(s.id, s.class_id, rest, s.name))
            if changed:
                self._checkpoint()
                self.shapes = result
                self.save()
            return changed

    def delete(self, shape_id: str) -> bool:
        with self.lock:
            if not any(s.id == shape_id for s in self.shapes):
                return False
            self._checkpoint()
            self.shapes = [s for s in self.shapes if s.id != shape_id]
            self.save()
            return True

    def rename(self, shape_id: str, name) -> bool:
        """Names a shape; an empty name gives it back its generic one."""
        name = check_name(name)
        with self.lock:
            target = next((s for s in self.shapes if s.id == shape_id), None)
            if target is None:
                raise ViewerError("No such shape.", 404)
            if target.name == name:
                return False
            self._checkpoint()
            self.shapes = [Shape(s.id, s.class_id, s.geometry, name) if s is target else s for s in self.shapes]
            self.save()
            return True

    def set_class(self, shape_id: str, class_id: int) -> bool:
        """Gives a shape another class. It keeps its id, outline and name, and is not merged with the shapes of its new
        class it overlaps (unlike painting). Returns False if it had that class already."""
        self.project.cls(class_id)
        with self.lock:
            target = next((s for s in self.shapes if s.id == shape_id), None)
            if target is None:
                raise ViewerError("No such shape.", 404)
            if target.class_id == class_id:
                return False
            self._checkpoint()
            self.shapes = [Shape(s.id, class_id, s.geometry, s.name) if s is target else s for s in self.shapes]
            self.save()
            return True

    def import_shapes(self, shapes: list[tuple], replace: bool) -> int:
        """Adds shapes (class id, geometry[, name]) read from another file, or replaces all shapes with them. One undo step."""
        with self.lock:
            self._checkpoint()
            kept = [] if replace else list(self.shapes)
            self.shapes = kept + [Shape(str(uuid.uuid4()), s[0], s[1], s[2] if len(s) > 2 else None) for s in shapes]
            self.save()
            return len(shapes)

    def undo(self) -> bool:
        with self.lock:
            if not self.undo_stack:
                return False
            self.redo_stack.append(self.shapes)
            self.shapes = self.undo_stack.pop()
            self.save()
            return True

    def redo(self) -> bool:
        with self.lock:
            if not self.redo_stack:
                return False
            self.undo_stack.append(self.shapes)
            self.shapes = self.redo_stack.pop()
            self.save()
            return True

    def summary(self) -> dict:
        classes: dict[str, int] = {}  # class id (as text, a JSON key) -> number of shapes
        for s in self.shapes:
            classes[str(s.class_id)] = classes.get(str(s.class_id), 0) + 1
        return {"shapes": len(self.shapes), "classes": classes, "can_undo": bool(self.undo_stack), "can_redo": bool(self.redo_stack)}


def count_shapes(file: Path) -> int:
    """Shapes in an annotation file, without loading the geometry (for the image list)."""
    try:
        return len(features_of(json.loads(file.read_text(encoding="utf-8"))))
    except (OSError, ValueError, AttributeError):
        return 0


def features_of(data) -> list[dict]:
    """The features of GeoJSON as QuPath and others write it: a FeatureCollection, a plain list of features, or one feature."""
    if isinstance(data, list):
        features = data
    elif isinstance(data, dict) and data.get("type") == "FeatureCollection":
        features = data.get("features") or []
    elif isinstance(data, dict) and data.get("type") == "Feature":
        features = [data]
    else:
        features = []
    return [f for f in features if isinstance(f, dict)]
