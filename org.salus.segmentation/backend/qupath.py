"""Importing QuPath GeoJSON exports: which files belong to which image, which classes they contain, and the shapes
of the classes the user matched to project classes.

QuPath files are only read. Only area annotations are imported (polygons, also with holes); detections, cells,
points and lines are left out, and so is every class the user did not match.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from shapely import make_valid
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from annotations import features_of, polygonal
from errors import ViewerError

UNCLASSIFIED = ""  # the mapping key of annotations without a class
SUFFIXES = (".geojson", ".json")
MAX_FILES = 20_000


def read(path: Path) -> list[dict]:
    try:
        return features_of(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        raise ViewerError(f"{path.name} cannot be read as GeoJSON: {e}") from None


def class_name(feature: dict) -> str:
    classification = (feature.get("properties") or {}).get("classification") or {}
    name = classification.get("name")
    if not name and isinstance(classification.get("names"), list):  # derived classes, e.g. ["Tumor", "Positive"]
        name = ": ".join(str(n) for n in classification["names"])
    return str(name) if name else UNCLASSIFIED


def area_geometry(feature: dict) -> BaseGeometry | None:
    """The area of an annotation, or None for anything that is not one (detections, cells, points, lines)."""
    props = feature.get("properties") or {}
    kind = str(props.get("objectType") or props.get("object_type") or "annotation").lower()
    if kind != "annotation":
        return None
    try:
        return polygonal(make_valid(shape(feature["geometry"])))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def summarize(features: list[dict]) -> tuple[dict[str, int], int]:
    """Area annotations per class name, and how many features are not imported because they are not area annotations."""
    classes: dict[str, int] = {}
    skipped = 0
    for feature in features:
        if area_geometry(feature) is None:
            skipped += 1
        else:
            name = class_name(feature)
            classes[name] = classes.get(name, 0) + 1
    return classes, skipped


def shapes_for(features: list[dict], mapping: dict[str, int | None]) -> list[tuple[int, BaseGeometry]]:
    out = []
    for feature in features:
        class_id = mapping.get(class_name(feature))
        if class_id is None:
            continue
        geometry = area_geometry(feature)
        if geometry is not None:
            out.append((class_id, geometry))
    return out


def strip_suffix(name: str) -> str:
    lower = name.lower()
    for suffix in SUFFIXES:
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return name


def match_folder(folder: Path, images: list[str]) -> tuple[list[tuple[Path, str]], list[dict]]:
    """Pairs the GeoJSON files below a folder with the project's images, by name.

    A file matches an image when its path in the folder, without .geojson/.json, is the image's path (with or without
    its extension), e.g. case1/slide.svs.geojson or case1/slide.geojson for case1/slide.svs. Otherwise its name alone
    is used (slide.geojson for .../slide.svs) if exactly one image has that name.
    Returns the matches and the files that were left out, with the reason.
    """
    by_path, by_stem_path, by_name = {}, {}, {}
    for rel in images:
        stem_rel = rel.rsplit(".", 1)[0] if "." in rel.rsplit("/", 1)[-1] else rel
        name = rel.rsplit("/", 1)[-1]
        by_path[rel.lower()] = rel
        by_stem_path[stem_rel.lower()] = rel
        for key in {name.lower(), (name.rsplit(".", 1)[0] if "." in name else name).lower()}:
            by_name.setdefault(key, set()).add(rel)

    files = []
    for directory, subdirs, names in os.walk(folder, onerror=lambda e: None):
        subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
        files += [Path(directory) / n for n in sorted(names) if not n.startswith(".") and n.lower().endswith(SUFFIXES)]
        if len(files) > MAX_FILES:
            raise ViewerError(f"The folder has more than {MAX_FILES} GeoJSON files.")

    matches, left_out, taken = [], [], {}
    for file in files:
        key = strip_suffix(file.relative_to(folder).as_posix()).lower()
        image = by_path.get(key) or by_stem_path.get(key)
        if image is None:
            candidates = by_name.get(key.rsplit("/", 1)[-1], set())
            if len(candidates) > 1:
                left_out.append({"file": str(file), "reason": "several images have this name"})
                continue
            image = next(iter(candidates), None)
        if image is None:
            left_out.append({"file": str(file), "reason": "no image with this name"})
        elif image in taken:
            left_out.append({"file": str(file), "reason": f"{Path(taken[image]).name} is imported for this image already"})
        else:
            taken[image] = str(file)
            matches.append((file, image))
    return matches, left_out
