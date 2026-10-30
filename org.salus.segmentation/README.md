# Segmentation

Polygon annotation of whole slide images in **annotation projects**, with export to pixel maps. Five components share one backend:

| Component | Panel | What it does |
|---|---|---|
| `browser` (entry) | left | Chooses the project (New / Open / remove from the list) and lists its images with their folder and number of shapes. Clicking an image opens it. Opens the viewer, tools and classes; **Import from QuPath** and **Export** are here too. |
| `viewer` | center | The image (OpenSlide tiles in OpenSeadragon) with the annotations drawn on top. |
| `tools` | bottom | Pan, Polygon, Eraser, Smart brush; eraser size; fill opacity; undo / redo; the class being drawn. |
| `classes` | right | The project's classes. Click one to draw with it. |
| `export` | center | Exports pixel maps (see below). |

## Projects

A project has a name, an **image folder** (searched recursively for the chosen file types, e.g. `.svs, .tif`) and a **save folder**.
The images are only read. Everything the plugin writes goes into the save folder:

```
<save folder>/project.json                          name, image folder, file types, classes
<save folder>/annotations/<image path>.geojson      one file per annotated image (<image path> relative to the image folder)
```

Open an existing project with **Open** and its save folder. The list of known projects is kept in `~/.salus/segmentation-projects.json`
(plugin settings are for settings, not data); removing a project from the list leaves its files alone.

## Classes

Each class has a **name** and a **drawing color** (how it looks while annotating); both can be changed at any time. Classes cannot be
deleted. The colors of exported pixel maps are chosen when exporting.

## Drawing

- **Polygon** (`P`): click to place points; double-click, click the first point or press Enter to close. Or press and drag to draw freehand;
  releasing closes the shape. `Esc` cancels, `Backspace` removes the last point. Self-intersections are repaired.
- **Eraser** (`E`): drag over shapes to cut the brushed area away. Shapes can split or get holes; shapes erased completely disappear.
- **Smart brush** (`B`): drag over a region to fill it, stopping at edges; `Shift`+drag erases with the same brush (from
  every class). Painted areas merge with the shapes of the same class they touch. The brush segments the image **as it is
  shown**, at the current zoom: its circles (16 px inner, 200 px outer; only the inner one is drawn) are screen pixels, so it behaves the same at every
  zoom, and zooming in gives finer outlines. Each dab: the region under the brush is blurred and its colours bucketed
  (PyTorch on CUDA, MPS or the CPU); from every seed in the inner circle the region is flooded on a 4-pixel lattice up to
  the outer circle, where a pixel joins if its bucket is close to the seed's and its exact colour within a threshold;
  gaps up to 40 px enclosed on all four sides are filled. The stroke becomes a polygon when the button is released.
  Settings are fixed in `backend/smartbrush.py` for now.
- **Smart eraser** (`X`): the smart brush, always erasing.
- **Smart polygon** (`S`): drawn like a polygon; when it is closed, the polygon is filled and smart brush dabs are run
  along its outline (every 16 px), seeded only from inside the polygon, so the shape grows out to the edges around it.
  The preview fills in while the dabs run; `Esc` cancels.
- **Pan** (`V`), or hold `Space` with any tool. The mouse wheel always zooms.
- Undo / redo: `Ctrl/Cmd+Z`, `Ctrl/Cmd+Shift+Z` (50 steps per image, while the plugin runs).

Shapes of different classes may overlap; every shape is independent.

## The GeoJSON files

A `FeatureCollection` per image. Coordinates are **pixels of the full-resolution image** (x to the right, y down, (0,0) the top left
of level 0), the convention of QuPath's GeoJSON, so the files can be dropped onto the image in QuPath and read by scripts. Slides whose
scanned area does not start at (0,0) (OpenSlide bounds, e.g. MRXS) are shown cropped, but annotations still use level-0 coordinates. Every feature is a `Polygon` or `MultiPolygon` (with holes where erased) with

```json
"properties": {"objectType": "annotation", "class_id": 1, "classification": {"name": "Tumor", "color": [255, 0, 0]}}
```

`class_id` is what counts; name and drawing color are written for other tools (and rewritten when a class changes). Ids are UUIDs, as in QuPath.
Each change is written at once (atomically).

## Importing from QuPath

**Import** in the image browser takes GeoJSON exported from QuPath (*File › Export objects as GeoJSON*, as a list of features or a
FeatureCollection):

- **One file** for the open image, or **a folder**: every `.geojson`/`.json` below it is matched to the image of the same name, by path
  (`case1/slide.svs.geojson` or `case1/slide.geojson` for `case1/slide.svs`) or, if that name is unique in the project, by file name alone.
  Files without an image, or with several candidates, are listed and left out.
- Then every QuPath class found (including *Unclassified*, and derived classes such as `Tumor: Positive`) is matched to a project class
  or left out. Classes with the same name are preselected.
- If an image already has annotations you choose whether the imported shapes are **added** or **replace** them (one undo step).

Only area annotations are imported; detections, cells, points and lines are left out. The QuPath files are only read, never changed.

## Exporting pixel maps

**Export** opens the export view in the center:

- **Classes**: the color each class gets in the maps, whether it is exported at all, and the background color. Where shapes of
  several classes overlap, the class **higher in the list** wins (↑/↓ change the order). Two exported classes cannot share a color,
  and none can have the background color.
- **Images**: which images to export (all, the annotated ones, or picked by hand).
- **Output**: the folder, the resolution (full, or 1/2 … 1/32), and whether existing maps are replaced or skipped.

Each image becomes `<folder>/<image path>.tif` (e.g. `case1/slide.tif`): an RGB, tiled, **pyramidal BigTIFF** with lossless (deflate)
compression and the image's resolution (microns per pixel), the size of the image's full-resolution level divided by the downsample,
in the same coordinates as the annotations. OpenSlide, QuPath and libvips open it. `classes.json` in the folder lists every class with
its color (hex and RGB) and priority, the background color, the downsample and which file belongs to which image.

The maps are drawn in 4096-pixel tiles and joined by libvips, so memory stays small (a 157 316 × 21 227 px slide took 40 s and 1.6 GB
on a laptop). Progress is shown per image; **Stop** ends the export (half-written files are removed). The settings are kept in the
project and are the defaults of the next export.

## Setup

```sh
backend/setup.sh        # creates backend/.venv with OpenSlide, Pillow, Shapely, libvips and PyTorch
```

Create it on the machine that runs Salus; the virtualenv cannot be copied between machines.

## Layout

```
manifest.toml
backend/   plugin.py (routes, shared state), project.py, annotations.py (Shapely), smartbrush.py (smart brush, PyTorch), qupath.py (import), export.py (pixel maps), slides.py + errors.py (from the File Viewer), run.sh, setup.sh, tests/
frontend/  browser/, viewer/, tools/, classes/, export/ (plain ES modules, no build step), shared/ (common.js, picker.js, osd.js, base.css, salus_sdk.js), vendor/ (OpenSeadragon)
```

Tests: `cd backend && .venv/bin/python -m unittest discover -s tests -t .`
