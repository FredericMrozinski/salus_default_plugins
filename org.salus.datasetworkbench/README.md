# Dataset Workbench

A Salus plugin to organize raw data in **sources** and bundle it into **datasets** for machine learning.
Design: `design/dataset-workbench.md` in the Salus repository.

| Component | Panel | What it does |
|---|---|---|
| `browser` (entry) | left | **Sources** tab: folders, source cohorts: file sources (browse, search, upload), table sources (CSV, columns, issues). A folder without source cohorts in it can become a general source cohort (right-click › *Convert to general source cohort*; its files become items, nothing moves). Inside a general source cohort every folder on disk is shown, also an empty one; right-click the cohort or one of its folders › *Create folder here…* makes an empty folder to upload into (`POST /sources/{id}/folders {parent, name}`). **Datasets** tab: list, create, delete. |
| `dataset-viewer` | right | All entries of one dataset (virtual list, search), **Automation**, **Edit schema**, `+ Entry`; the footer counts the entries per state, and clicking a count shows only those. Several entries can be picked (Ctrl/Cmd+click, Shift+click for a range, Ctrl/Cmd+A for all the list shows, Esc for none); the right-click menu deletes them (confirmed by typing "delete") and, with the automation on, runs it now, which creates the deleted entries again if their source items still match. |
| `entry-editor` | center | Edits one entry (typed value editors, searchable reference pickers, item states, "Mark as valid"), the dataset schema and its automation. |

Files and tables open in other plugins through `salus.openFile` (the Relational Viewer for CSV/JSON/SQLite, the Image Viewer for slides).

## Refresh

**Refresh** (the same button and progress bar in the Sources and the Datasets tab) reads every source cohort again: file cohorts are scanned for new, moved, changed and gone files, table
cohorts read their CSV again; then the datasets that use them are updated and their automation runs. It is one background job
(`refresh`, `POST /scan`) that reads the cohorts **one after the other**, not all at once, so on a large or remote filesystem the walks
do not compete. A bar under the toolbar shows which cohort it is reading ("2 of 5: Whole Slide Images") and how many files it has
seen there. Nothing is blocked meanwhile: the walk over a folder, the slow part, runs without the cohort's lock, which is only taken
for the short database update at the end. Uploads, renames and deletes made during the walk are kept as they are (the scan does
not undo or duplicate them); a cohort moved or renamed during its walk is left for the next Refresh. A cohort that is being
scanned on its own (*Rescan files*) is skipped. Code: `Workbench.scan_all` in `backend/workbench/service.py`, `FileSource.scan` in
`backend/workbench/sources.py`; tests: `ScanDuringChangesTests` in `backend/tests/test_workbench.py`.

Every SQLite connection is opened and closed under one lock (`workbench/util.py`, a workaround for hangs of conda's SQLite).
Connections are therefore kept open after use and reused (a small pool per database file, at most 64 in all): closing the last
connection to a database checkpoints its WAL and deletes it, with fsyncs, which on an HDD held up every other request behind that
lock. A pooled connection is only reused for the very file it was opened on (device and inode), and deleting, moving or renaming
a source cohort, a folder or a dataset closes the idle connections under it first. A connection that changed 1000 rows or more
checkpoints the WAL when it is given back, outside the lock (`CheckpointTests`, `PoolTests` in `backend/tests/test_util.py`).

An upload's commit, a rename and a delete only tell the views that the files changed; the datasets that use the cohort (their
entries' states, the automation that makes entries from new files) are brought up to date by one background worker, once for a
burst of changes (`source_changed_later` in `workbench/service.py`, `BackgroundUpdateTests`). Done inside every commit, a folder
upload into a cohort with a large automated dataset queued the commits up behind each other until they ran into Salus' 30 s
timeout (504), and with them every other request. The backend has 64 threads for requests (asyncio's default is cpus + 4) so a
few slow requests cannot take all of them; the views refetch at most once per burst of events, and two files are uploaded at a
time.

After a scan the datasets are updated from the cohort's index (the sizes and dates the scan has just recorded), not by
looking at every referenced file again, and the automation re-evaluates only the entries it created or changed: on a slow
disk, reading everything a second time kept the backend busy long after a Refresh. Opening a dataset still checks its files
themselves (`POST /datasets/{id}/check`). SQLite runs with `synchronous=NORMAL` (safe in WAL mode; saves an fsync per commit).

**If the backend is slow or hangs:** requests over 5 s are logged ("slow request: GET /… took 12.3 s"), so are slow checkpoints and
closes; `kill -USR1 <backend pid>` prints the stack of every thread to the backend's stderr.

## Automation

The **Automation** button (bolt icon) in the entries panel opens the automation settings in the center panel (like *Edit schema*; *Save and
apply* or *Cancel* returns to the entry). The automation builds a dataset by itself. Settings are per dataset, stored in the dataset's
database (`meta` key `automation`) and so survive restarts and travel with the dataset; rules of fields deleted from the schema are dropped.

- **Create from source cohort entry:** at most one file or row field can have this option (the backend refuses a second one). On every refresh
  (the Refresh button / any scan, an upload, a CSV ingest, opening the dataset, saving the settings) each item of that field's source cohorts that no
  entry has in the field yet (files with an allowed extension, rows of a table) becomes a new entry with the item in that field.
  Entries are never deleted by the automation.
  - **Filter (optional):** a regex searched (Python `re.search`, not anchored) in each item's text, the relative path of a file or the key of a
    row; only items it finds become entries. Empty: every item. `^cohort_a/` takes one folder, `_HE\.svs$` only H&E slides,
    `^(?!.*_old/)` everything outside `_old/` folders. Entries that exist already stay when the filter changes; a wider filter adds what it
    now lets through on the next refresh. The preview shows how many items match and a few that are left out.
- **Leave empty** or **Derive from field** for every other field. A derivation reads the text of another field (a file's relative path, a
  row's key, a value), searches the **read** regex in it (Python `re.search`, not anchored) and writes the **value** template with the groups
  filled in. Group references are **backslash style**: `\1` … `\99`, `\g<name>`, `\g<0>` for the whole match, `\\` for a backslash.
  Unlike `sed s/read/value/`, the result is only the template, not the input with the match replaced: `_(\d+)\.svs$` → `\1` turns
  `slides/P1_17.svs` into `17`.
  - A value field gets the text converted to its type (`17` → integer 17). A file or row field links the item the text names in one of its
    sources: a relative path, or a file name without a folder when exactly one file has it; a row key.
  - **Derivations chain**: a field can derive from a derived field (e.g. slide → case id → patient row); the order is worked out, cycles are refused.
  - **Existing values.** The runs on a refresh only fill **empty** cells: a value typed by hand stays, and later derivations in a chain
    continue from it. When you apply the settings yourself (*Save and apply*, or *Run automation now* in the entries' right-click menu)
    and some entries already hold a value that differs from what the automation gives, a dialog shows how many, with examples, and
    asks: **Cancel**, **Fill in missing** (only empty fields; those values stay) or **Overwrite existing** (they get the automation's,
    and chains continue from the new values). A derivation that fails never touches a value that is there. Backend:
    `POST /datasets/{id}/automation/conflicts {rules?}` counts without changing anything; `PUT …/automation` and
    `POST …/automation/run` take `{mode: "fill" | "overwrite"}`.
  - When a derivation fails (no match, empty result, wrong type, no such item, an ambiguous file name) the cell stays empty and is **flagged**:
    state *invalid* with the reason ("automation: …") in the entry editor, so the entry counts as incomplete. The flag goes away once the cell is
    filled, by hand or by a later run (e.g. after the missing mask was uploaded).
- The view shows a **live preview** of every derivation for a sample: one of the first items of the source cohort that pass the filter, or a
  text of your own.
- Backend: `backend/workbench/automation.py`; routes `GET|PUT /datasets/{id}/automation`, `POST /datasets/{id}/automation/run`,
  `POST /datasets/{id}/automation/preview`. Tests: `backend/tests/test_automation.py`.

## Export

The **Export** button (upload icon) in the entries panel, below *Automation* and *Edit schema*, opens the export wizard in the center panel.
It goes through pages like the Pharaoh plugin; every page shows what the export would do (the backend checks the options on each change):

1. **Entries:** valid entries are always exported; incomplete and corrupted ones only if ticked (each option shows how many there are).
2. **Keys:** each field becomes a key of every exported entry, its name by default; rename keys for the export here (they must differ and
   not be empty). The page shows the first entry as it would be written.
3. **Sources:** either **copy the source files** into the export (only the files the exported entries use, plus the CSV of each table cohort,
   under `sources/<the cohort's folder in the workbench>/`; the JSON names them relative to itself), or **refer to them where they are**:
   by **absolute path** on the server, or by their **path inside the cohort** after an optional per-cohort **prefix** (none: just the
   relative path).
4. **Destination:** a **folder** (`dataset.json`, and `sources/` with copies), a **zip file** (the same; copied files are stored, not
   compressed again) or **only the JSON file** (not with copies). The path is on the server; the default is `<workbench>/exports/<dataset>`.
   The page shows the number of entries, the size of the copies and the free space there.
5. **Export:** progress, *Cancel export*, and where it went.

The JSON: `{format: "salus.datasetworkbench.export", version: 1, dataset: {id, name}, exported_at, states, paths, fields: [{key, name,
label, kind, type, required, sources}], sources: [{name, kind, location, csv?}], entries: [{<key>: value}]}`. A value field is its value
(null when empty), a file field the file's path, a row field `{key, values: {column: value}}`. A file or row that is gone is null.

**Writing:** the export is written under a temporary name next to the destination and renamed into place when complete, so a failed or
cancelled export leaves the destination as it was. An existing destination is only replaced after you confirm: a file with a click, a folder
only if it holds an earlier export (`dataset.json`) and after typing "delete" (its contents go). Destinations inside the workbench's
`sources/` or `datasets/` are refused. Files and folders are created the ordinary way (copies do not take over the original's mode), so
the umask Salus runs with decides their permissions, as for every other file the plugin writes.

Backend: `backend/workbench/export.py`; routes `POST /datasets/{id}/export/preview` (the wizard's checks), `POST /datasets/{id}/export`
(`{options, overwrite}`, a background job "export"; 409 when the destination exists and `overwrite` is not set), `GET /datasets/{id}/export`
(the last export's state and progress, so a reopened wizard shows a running one) and `POST /datasets/{id}/export/cancel`.
Frontend: `src/editor/ExportWizard.tsx`. Tests: `backend/tests/test_export.py`.

## Layout

```
manifest.toml
backend/
  plugin.py          HTTP routes + event broadcast (Salus SDK)
  workbench/         catalog logic without any Salus dependency: sources, uploads, datasets, states
  tests/             python3 -m unittest discover -s tests
frontend/            React + TypeScript + Vite, one page per component, shared code in src/shared
  dist/              build output (what manifest.toml points at)
```

## Configuration

The workbench folder (it holds `sources/` and `datasets/`) is the `root` setting of the plugin, defined in `configurable.toml` and kept by Salus
(see the Salus docs, "Settings"). It must be on a local disk (SQLite).

- **First start:** nothing is set, so every component says "Go to Settings → Dataset Workbench and enter the folder". It continues by itself
  (it checks every few seconds) once the folder is set. The plugin does not suggest or create anything on its own.
- **Choosing the folder:** in the Salus settings, **Dataset Workbench → Workbench folder**. The folder is created if needed; an existing workbench
  in it is picked up. Choosing another folder deletes nothing in the old one.
- If the set folder cannot be used (unplugged disk, a relative path), the same message says why.
- Backend route `GET /settings` works without a folder; every other route answers 412 until one is set up.
- The old environment variable `SALUS_WORKBENCH_ROOT` and the old `settings.json` in the plugin's config directory are no longer read.

## Build and install

The plugin folder is what you copy (or symlink) into the Salus plugins directory; it is already there when you develop in place.

```sh
cd frontend
npm install
npm run build          # type check + build into frontend/dist
npm test               # tests of the upload queue (Node 22.6+)
cd ../backend
python3 -m unittest discover -s tests
```

If the backend ever seems stuck, `kill -USR1 <backend pid>` prints the stack of every thread to the server's log.
Manifest and backend changes need a server restart (the backend lives for the whole session); a rebuilt frontend only needs the tab to be reopened.
The backend needs Python 3.10+ and uses only the standard library. `backend/salus_sdk.py` and `frontend/src/shared/salus_sdk.js` (+ `.d.ts`) are copies of the Salus SDKs.
