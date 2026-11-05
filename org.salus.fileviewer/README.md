# File Viewer

One plugin, one backend, several viewers for files that other plugins open with `salus.openFile(path)`.

| Component | File types | Notes |
|---|---|---|
| `slide` | svs, tif, tiff, ndpi, scn, mrxs, vms, vmu, bif | Whole slide images: OpenSlide renders Deep Zoom tiles, OpenSeadragon shows them. Needs the virtualenv (below). |
| `image` | png, jpg, jpeg, gif, webp, bmp, heic, heif | Pictures: Pillow decodes the file once and serves Deep Zoom tiles (pan, zoom to single pixels, navigator). The original file never reaches the browser. Transparency shows white, EXIF rotation is applied, animated GIF/WebP show the first frame, up to 120 megapixels. HEIC/HEIF (iPhone photos) is decoded by `pillow-heif` when installed (`backend/setup.sh` installs it), otherwise converted with macOS `sips` or libheif's `heif-convert`. |
| `table` | csv, tsv, json, jsonl, ndjson, sqlite, sqlite3, db | Grid with paging, sorting, global and per-column filters. Files are loaded into a temporary SQLite database; SQLite files are opened read-only. **CSV and TSV can be edited** (see below). |
| `pdf` | pdf | The server renders the pages (pypdfium2) and the viewer shows them as images with zoom. The PDF file itself never reaches the browser, and there is no download or print button. |
| `text` | txt, log, md, geojson, topojson, ipynb, xml, kml, gpx, svg, html, yaml, toml, ini, cfg, py, js, ts, css, sh, r, sql, c, cpp, java, rs, go, … (see the manifest) | Read-only text with line numbers and syntax highlighting by [highlight.js](https://highlightjs.org) 11.11.1 (BSD-3-Clause, `frontend/vendor/highlight.min.js` with its 36 common languages). The language comes from the file type, or highlight.js guesses it from the text; the **Language** list changes it. JSON (GeoJSON …) can be shown **formatted**, with short arrays such as points kept on one line; minified files start formatted. **Wrap** breaks long lines. UTF-8, UTF-16 (with BOM) and Windows-1252 are read; binary files are refused; files over 8 MB show their first 8 MB, and over 2 MB of text is shown without colors. The colors follow Salus' light or dark theme. `json`, `csv` and `tsv` stay with the table viewer. |

**Slides without a pyramid.** A slide whose smallest level is still larger than 4096 px (no pyramid, or one that stops too
early) has to be read once at full resolution before it can be shown zoomed out (the overview, `slides.py`), which takes
minutes for a large one. Before opening, the slide viewer asks the backend (`GET /slide/check?file=`, from the file's
metadata only); for such a slide over 100 megapixels (`WARN_PIXELS`) that is not open already, it says that the slide
has no pyramidal image structure, that opening it can take a few minutes and that the Pharaoh plugin can add one, and opens
it only if you choose **Open anyway**.

There is no entry component, so the plugin is not in the **+** list. Several viewers for one type would make Salus ask which to use; this plugin is the only one that should claim these types.

## Layout

```
manifest.toml
backend/   plugin.py (routes), slides.py, pictures.py, tables.py, pdfs.py, texts.py, errors.py, run.sh + setup.sh (virtualenv), salus_sdk.py
frontend/  slide/, image/, table/, pdf/, text/ (plain HTML + ES modules, no build step), vendor/ (OpenSeadragon, highlight.js), osd.js (shared viewer setup), salus_sdk.js
```

## Setup

The slide viewer needs OpenSlide and Pillow, the image viewer Pillow, the PDF viewer pypdfium2 (Python 3.10+). The table viewer uses the standard library only.
They live in a virtualenv, which you create **on the machine that runs Salus**:

```sh
backend/setup.sh        # creates backend/.venv
```

**A virtualenv cannot be copied to another machine** (it holds that machine's Python and compiled libraries). When you deploy the plugin elsewhere, leave
`backend/.venv` out (for example `rsync --exclude .venv`) and run `setup.sh` there.

Without a working `backend/.venv` the backend does not start (the launcher says "virtualenv missing. Run backend/setup.sh first." in the server log), and Salus
answers **503 Service Unavailable** after waiting ten seconds for it. If every viewer of this plugin fails like that, run `setup.sh`.

Manifest and backend changes need a server restart.

## Editing tables (CSV and TSV)

The **Edit** switch at the top turns on edit mode: change cells (click, Enter to confirm, Esc to cancel, Tab to move on), add and delete rows and
columns, rename columns (double-click). Edits go to a private working copy on the server. **Save** writes them to the file; switching Edit off saves
automatically; **Discard changes** loads the file again. Nothing is locked. If the file changed since editing began, saving writes nothing and asks:
save a copy elsewhere (the editor then continues on the copy), overwrite anyway, or discard and reload.

- Values stay text exactly as in the file (`007` stays `007`). The delimiter, quote character, line endings, byte order mark and a missing final newline are kept.
- A value with the delimiter, a quote or a line break is quoted by the CSV writer, so nothing a user types can break the file.
- Saving writes a temporary file next to the original and swaps it in atomically. Files that are not UTF-8, or larger than 300 MB, are not editable.
- Rows wider than the header get unnamed columns, blank lines are dropped on save.
- Closing the viewer drops the working copy; unsaved edits are lost.
- In a tabular source cohort of the Dataset Workbench, saving changes the rows: dataset entries that use them show "source changed". Do not save a copy into such a folder (the editor warns).
- Routes: `/table/edit/...` (`table_edit.py`); tests: `python3 -m unittest discover -s backend/tests`.

## Adding a viewer

Add a module with its logic, its routes in `plugin.py` (prefix them with the viewer's name), a page under `frontend/` and a `[[frontend-component]]` with `file_viewer_for`.
