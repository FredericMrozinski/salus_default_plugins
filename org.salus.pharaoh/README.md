# Pharaoh

Repairs and compresses whole slide images in bulk. Pharaoh reads each slide once with libvips and writes it as a tiled BigTIFF with:

- **all missing pyramid levels** (each half the size of the previous one, down to one tile), so viewers zoom out quickly,
- **a different compression**: JPEG or WebP with a quality of your choice, or lossless Deflate / Zstandard / LZW.

The plugin is a four-step wizard in the center panel:

1. **Files**: a list of slides. **+** adds a file, or every slide (tif, tiff, svs, ndpi, scn, mrxs, vms, vmu, bif) below a folder. Files are inspected (size, levels) in the background.
2. **Transformations**: pyramid, compression, quality, tile size, BigTIFF, and "skip slides that already have a pyramid".
3. **Saving**: replace the originals (asks first; a result that is not smaller can be dropped), save next to them with a prefix and/or suffix, or save into another folder (subfolders of an added folder are kept). A preview lists what will happen to every file.
4. **Progress**: overall progress with an estimate, one line per file, and **Stop**. A run keeps going when the panel is closed; opening the plugin again shows it.

Outputs are written to `<name>.pharaoh-part` and renamed when complete, so a stopped or failed run never leaves a half-written slide (and never damages an original).

## Setup

```sh
backend/setup.sh        # creates backend/.venv with pyvips and the libvips it bundles
```

Create it on the machine that runs Salus; the virtualenv cannot be copied between machines (copy the plugin without `.venv`).

## Things to know

- Every file is **encoded again**. Re-encoding a JPEG slide with JPEG loses a little more detail; to only add levels use a high quality (90+) or a lossless compression.
- Only the full-resolution image is kept. Label and macro images of vendor formats such as SVS are not copied, and vendor metadata beyond the TIFF description and resolution is lost, so OpenSlide may report the result as a generic TIFF.
- Formats other than TIFF-based ones (ndpi, mrxs, ...) only work if the bundled libvips can read them; otherwise the file is listed as unreadable.
- Transparent or 16-bit slides are flattened onto white / reduced to 8 bit when JPEG or WebP is chosen.

## Layout

```
manifest.toml
backend/   plugin.py (routes), pharaoh.py (list, plan, run), wsi.py (finding and inspecting slides), errors.py, run.sh + setup.sh, salus_sdk.py, tests/
frontend/  index.html, app.js, style.css (plain ES modules, no build step), salus_sdk.js
```

Tests: `backend/.venv/bin/python -m unittest discover -s backend/tests -t backend`
