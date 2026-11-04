# Segmentation

Polygon annotation of whole slide images in **annotation projects**, with export to pixel maps. Five components share one backend:

| Component | Panel | What it does |
|---|---|---|
| `browser` (entry) | left | Chooses the project (the picker at the top; **+** creates one, the folder button opens one) and lists its images with their folder and status, filtered by name and status. Clicking an image opens it. Opens the viewer, tools and classes; **Export**, **Statuses** and, under **⋯**, **Project information** (folders and file types) and **Import from QuPath** are here too. |
| `viewer` | center | The image (OpenSlide or Pillow tiles in OpenSeadragon) with the annotations drawn on top. |
| `tools` | bottom | The mode (Exact or a smart one); Pan, Brush, Eraser, Polygon; the tool width; undo / redo; the class being drawn. |
| `classes` | right | The project's classes, with their numbers of shapes in the open image and a hide button. Click one to draw with it. The fill opacity of the shapes is at the bottom. |
| `export` | center | Exports pixel maps (see below). |

## Projects

A project has a name, an **image folder** (searched recursively for the chosen file types, e.g. `.svs, .tif`) and a **save folder**.
Whole slide images are read by OpenSlide; anything it cannot read, such as `.jpg`, `.png`, `.bmp` or untiled TIFF, by
Pillow (decoded into memory once when opened, up to 1 gigapixel), so plain pictures can be annotated, segmented and
exported the same way. The images are only read. Everything the plugin writes goes into the save folder:

```
<save folder>/project.json                          name, image folder, file types, classes, statuses
<save folder>/annotations/<image path>.geojson      one file per annotated image (<image path> relative to the image folder)
```

Open an existing project with the folder button and its save folder. The list of known projects is kept in `~/.salus/segmentation-projects.json`
(plugin settings are for settings, not data). Projects cannot be removed from the list in the browser for now.

## Classes

Each class has a **name** and a **drawing color** (how it looks while annotating); both can be changed at any time. Classes cannot be
deleted. The colors of exported pixel maps are chosen when exporting.

The class editor shows how many shapes of each class the open image has, and an eye button per class that **hides** its
shapes in the viewer. Hidden shapes are left alone by the eraser (so nothing is erased that cannot be seen), and drawing
with a hidden class shows it again. Which classes are hidden is not saved; opening a project shows all of them.

The arrow in front of a class lists its shapes in the open image, each with a small picture of its outline. Shapes are
called *Tumor 1*, *Tumor 2*, … until they are renamed (double-click or ✎; an empty name gives back the generic one; undo
works). Clicking a shape moves the viewer to it, zoomed so that it sits in the middle with a margin, and outlines it until
you click beside it while panning (dragging keeps it) or draw again. Right-click a shape in the list to **delete** it
(after confirming; undo brings it back). Names are kept in the GeoJSON as `properties.name`, where QuPath keeps the names of annotations, so names
given in QuPath come along when importing and names given here show up in QuPath.

## Statuses

Every image has a **status**, shown as a colored dot in the image list; the footer shows a bar with a segment per status
(as wide as its share of the images) and counts the images per status. The chips under the search field filter the list by
status: with none chosen every image is shown, otherwise the images with one of the chosen statuses.

Several images get a status at once by selecting them: `Ctrl/Cmd`+click adds or removes one, `Shift`+click selects a range,
`Ctrl/Cmd+A` all images in the (filtered) list, and `Esc` or a plain click ends the selection. While images are selected,
the status buttons are at the bottom of the list and apply to all of them; a status some of them have is half filled.
If more than 4 images would change their status, a dialog asks first and lists the statuses they have now
(`CONFIRM_STATUS_ABOVE` in `frontend/browser/script.js`); Cancel has the focus.

Right-clicking an image (or the selection) offers **Remove from project**: the images are no longer listed, imported into
or exported, but nothing is deleted. The image files stay in the image folder and their annotations in the save folder;
**⋯ › Removed images** puts them back. The removed images are listed in `project.json`. The open
image lists all statuses under its name, and clicking one sets it. A new project has *Empty* (gray), *To review* (yellow)
and *Reviewed* (green). **Statuses** in the browser edits the list: names, colors, order, adding and removing. The first status is
what images start with; images whose status is removed get the first one. The statuses, and which image has which, are kept
in `project.json`.

## Drawing

Four tools, and the **mode** (the row of buttons above them) that says how brush, eraser and polygon work: **Exact** draws exactly
what is drawn; the smart modes make them follow the image. So far there is one smart mode, **Color Similarity**. The
**width** (screen pixels) is the brush and eraser diameter, and in a smart mode the diameter of the inner circle.

- **Pan** (`V`), or hold `Ctrl` or `Space` with any tool. The mouse wheel always zooms. The viewer takes the keyboard
  when the mouse comes onto it, so keys work there right after a click in another panel; Space never presses a button
  in the other panels.
- **Right-drag** erases with any tool but Pan (with the smart eraser in a smart mode); with the Eraser chosen it paints with
  the brush instead.
- **Pen and fingers** (iPad with Apple Pencil, Surface or Wacom pens; Pointer Events `pointerType`): the pen always
  draws, with the chosen tool, or the brush when Pan is chosen; its eraser end or barrel button erases (paints when the
  Eraser is chosen). Fingers always pan and pinch-zoom, whatever the tool. While the pen is on or just above the screen,
  and for half a second after, new touches are taken for the resting hand and ignored, so a palm cannot move the image
  during a stroke. The mouse works as before.
- **Brush** (`B`): drag to paint with the class; `Shift`+drag erases. Painted areas merge with the shapes of the same class they touch.
- **Eraser** (`E`): drag over shapes to cut the brushed area away (from every class). Shapes can split or get holes; shapes
  erased completely disappear.
- **Polygon** (`P`): click to place points; double-click, click the first point or press Enter to close. Or press and drag to draw freehand;
  releasing closes the shape. `Esc` cancels, `Backspace` removes the last point. Self-intersections are repaired.

In the Color Similarity mode:

- **Smart brush**: drag over a region to fill it, stopping at edges; `Shift`+drag erases with the same brush (from
  every class). The brush segments the image **as it is shown**, at the current zoom: its circles (the inner one is drawn;
  the outer one, 200 px, is how far a dab may reach) are screen pixels, so it behaves the same at every zoom, and zooming in
  gives finer outlines. Each dab: the region under the brush is blurred and its colours bucketed (PyTorch on CUDA, MPS or
  the CPU); from every seed in the inner circle the region is flooded on a 4-pixel lattice up to the outer circle, where a
  pixel joins if its bucket is close to the seed's and its exact colour within a threshold; gaps up to 40 px enclosed on
  all four sides are filled. Dabs are placed one inner radius apart. The stroke becomes a polygon when the button is
  released. The other settings are fixed in `backend/smartbrush.py` for now.
- **Smart eraser**: the smart brush, always erasing.
- **Smart polygon**: drawn like a polygon; when it is closed, the polygon is filled and smart brush dabs are run along its
  outline (one inner radius apart), seeded only from inside the polygon, so the shape grows out to the edges around it.
  The preview fills in while the dabs run; `Esc` cancels.
- **Border Growth** (experimental mode, `backend/borderbrush.py`): like Color Similarity, the inner circle defines the
  allowed colours, and nothing leaves the outer circle. But a dab does not flood from seeds in the
  inner circle: it grows what the stroke has painted so far from its border, through allowed pixels (4-connected, every
  pixel, no stride), and **compares with the neighbour**: a pixel is only entered from a neighbour whose colour differs
  by at most a step limit (blurred colours change by about 1–2 per pixel inside smooth regions and 5–10 across edges),
  so the growth cannot chain through a series of allowed colours across an edge. Both limits depend on the distance
  from the **inner circle**: lenient inside it and up to half its radius (at least 8 px) beyond, with colours within
  18 of the inner circle's and steps up to 3; then falling over 60 px to strict, colours within 5 and steps up to 2,
  for the rest of the outer circle (`THRESHOLD_NEAR/FAR`, `STEP_NEAR/FAR`, `NEAR_MARGIN`, `FALLOFF`; Color
  Similarity uses 15 everywhere). The brush is generous where it is held and strict further away, so where the brush
  goes, and its width, steer the growth. Inside the inner circle the step limit is loosened further: 10 at the centre,
  falling to 3 at the inner circle's rim (`STEP_CENTRE`), so a click on an edge or a fine structure, where colours change
  by more than 3 from pixel to pixel, still gets away from its centre pixel instead of painting nothing. The first dab, or one with nothing
  painted in its outer circle, starts from the brush's centre; an allowed colour that is not connected to the painted
  area is not reached, so a stroke that starts on the wrong side of an edge stays there. Gaps are filled as in Color
  Similarity. The growth is one connected-component labelling (scipy) on a grid of twice the resolution, whose extra
  cells are the links between neighbours; each distinct colour is tested once against a KD-tree of the inner circle's
  colours. A dab takes about 20 ms. The smart polygon works the same way, with only the inner circle's colours inside
  the polygon.
- Undo / redo: `Ctrl/Cmd+Z`, `Ctrl/Cmd+Shift+Z` (50 steps per image, while the plugin runs).

Shapes of different classes may overlap; every shape is independent.

## Model brushes (your own segmentation models)

A segmentation model can drive the brush, eraser and polygon as a smart mode of its own (`backend/modelbrush.py`). Put a
module in `backend/models/` that registers the model (an example is in `backend/models/__init__.py`); when the backend
starts, it appears in the tools as a mode after Color Similarity.

**Swin Edges** (`backend/models/swin_edges.py`) is the Swin-UNet edge model (Swin-B encoder, convolutional decoder,
92 M parameters), rebuilt from the training code without Lightning; it gets RGB as 0..1. The weights come from its
Lightning checkpoint, which goes to `backend/models/weights/swin_edges.ckpt` (not in git; or set `SEGMENTATION_SWIN_EDGES`
to its path). It runs on
CUDA, else **MPS** (Apple GPU), else the CPU (`SEGMENTATION_DEVICE` overrides): on an Apple laptop a 1024 × 1024 view
takes 0.3 s on MPS (the first one about 2 s), 2.2 s on the CPU, and about 2.5 GB of GPU memory.

- The model gets the view around the brush as it is seen on screen: 1024 × 1024 RGB pixels (per model: `view`) at the
  current zoom, and returns a map of the same size, either
  - an **edge map** (`output="edges"`): 1 on edges, 0 elsewhere (or probabilities, edge above `threshold` = 0.5). Each
    dab casts 360 rays from the brush's centre (one per degree); each stops at the first edge or at the circle. The
    dab paints (or erases) the area within the rays' ends, including the edge line. A ray longer than the shortest
    rays on both sides of it, within `neighbours` degrees (default 10°), went through a hole in the edge: it is
    interpolated between the rays that stopped, so holes up to 10° wide do not let the fill out (likewise, a ray cut
    short by a speck is lengthened). A ray only one side agrees with is a genuine step, e.g. where an edge ends inside
    the circle, and stays. Gaps up to 2 × `close` pixels (default 2 px) are also closed beforehand. The area is
    star-shaped around the centre: whatever lies behind an edge, as seen from the centre, is not painted, even if a
    path around the edge would reach it. If the centre is on an edge, the rays start from the nearest pixel off it; or
  - a **label map** (`output="labels"`, the default): a region id per pixel (0 = none). Each dab paints everything
    inside the circle that belongs to the region under the brush's centre.
- The polygon grows along its outline the same way.
- The model runs in a background thread, one view at a time. While it works the viewer says "The model is segmenting…"
  and paints when it is done. The last 12 views are kept, so painting again in the same place at the same zoom is
  immediate.
- PyTorch is only imported at startup when `backend/models/` has a model module; a module that fails to load is logged
  and left out. Load weights on the model's first call, not at import, so the plugin starts quickly.

## The GeoJSON files

A `FeatureCollection` per image. Coordinates are **pixels of the full-resolution image** (x to the right, y down, (0,0) the top left
of level 0), the convention of QuPath's GeoJSON, so the files can be dropped onto the image in QuPath and read by scripts. Slides whose
scanned area does not start at (0,0) (OpenSlide bounds, e.g. MRXS) are shown cropped, but annotations still use level-0 coordinates. Every feature is a `Polygon` or `MultiPolygon` (with holes where erased) with

```json
"properties": {"objectType": "annotation", "class_id": 1, "name": "Region A", "classification": {"name": "Tumor", "color": [255, 0, 0]}}
```

(`name` only for shapes that were given one.)

`class_id` is what counts; name and drawing color are written for other tools (and rewritten when a class changes). Ids are UUIDs, as in QuPath.
Each change is written at once (atomically).

## Importing from QuPath

**Import from QuPath** (in the **⋯** menu of the image browser) takes GeoJSON exported from QuPath (*File › Export objects as GeoJSON*, as a list of features or a
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

pyvips must use the libvips bundled by `pyvips-binary`, not a system one (Homebrew): that one loads a second OpenMP
library next to PyTorch's, and the backend crashes (segmentation fault in `libomp`, Salus then answers 503) as soon as
PyTorch works hard, e.g. with a segmentation model. `setup.sh` takes care of it; in an older virtualenv run
`backend/.venv/bin/pip install --force-reinstall --no-deps pyvips-binary`.

## Layout

```
manifest.toml
backend/   plugin.py (routes, shared state), project.py, annotations.py (Shapely), smartbrush.py (smart brush, PyTorch), modelbrush.py + models/ (model brushes), qupath.py (import), export.py (pixel maps), slides.py + errors.py (from the File Viewer), run.sh, setup.sh, tests/
frontend/  browser/, viewer/, tools/, classes/, export/ (plain ES modules, no build step), shared/ (common.js, picker.js, osd.js, base.css, salus_sdk.js), vendor/ (OpenSeadragon)
```

Tests: `cd backend && .venv/bin/python -m unittest discover -s tests -t .`
