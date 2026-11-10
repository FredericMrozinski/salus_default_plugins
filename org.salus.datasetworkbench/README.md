# Dataset Workbench

A Salus plugin to organize raw data in **sources** and bundle it into **datasets** for machine learning.
Design: `design/dataset-workbench.md` in the Salus repository.

| Component | Panel | What it does |
|---|---|---|
| `browser` (entry) | left | **Sources** tab: folders, source cohorts: file sources (browse, search, upload), table sources (CSV, columns, issues). A folder without source cohorts in it can become a general source cohort (right-click › *Convert to general source cohort*; its files become items, nothing moves). **Datasets** tab: list, create, delete. |
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
Closing the last connection to a database checkpoints its WAL, which after a large write (a Refresh that recorded many files,
an automation that created many entries) is slow on a network filesystem and used to hold up every request, so Salus answered
504. A connection that changed 1000 rows or more now checkpoints itself before closing, outside that lock (`CheckpointTests`
in `backend/tests/test_util.py`).

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
