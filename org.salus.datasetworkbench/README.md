# Dataset Workbench

A Salus plugin to organize raw data in **sources** and bundle it into **datasets** for machine learning.
Design: `design/dataset-workbench.md` in the Salus repository.

| Component | Panel | What it does |
|---|---|---|
| `browser` (entry) | left | **Sources** tab: folders, source cohorts: file sources (browse, search, upload), table sources (CSV, columns, issues). **Datasets** tab: list, create, delete. |
| `dataset-viewer` | right | All entries of one dataset (virtual list, search, state filter), `+ Entry`, statistics. |
| `entry-editor` | center | Edits one entry (typed value editors, searchable reference pickers, item states, "Mark as valid") and the dataset schema. |

Files and tables open in other plugins through `salus.openFile` (the Relational Viewer for CSV/JSON/SQLite, the Image Viewer for slides).

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

The workbench folder (it holds `sources/` and `datasets/`) is stored in the plugin's config directory that Salus provides
(`settings.json`: `{"root": "/path/to/folder"}`; see the Salus docs, "Configuration files"). It must be on a local disk (SQLite).

- **First start:** nothing is set, so every component shows a setup screen that asks for the folder (suggesting `~/SalusWorkbench`, the old default),
  tells what would happen (created, an existing workbench is picked up, a problem) and stores the choice.
- **Later:** right-click the background of the Sources or Datasets tab, **Workbench folder…**, to choose another folder. Nothing is deleted in the old one.
- **By hand:** the file can be edited; the plugin follows it. If the stored folder cannot be used (unplugged disk), the setup screen says why.
- Backend routes `GET/PUT /settings` and `POST /settings/check` work without a folder; every other route answers 412 until one is set up.
- The old environment variable `SALUS_WORKBENCH_ROOT` is no longer read.

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
