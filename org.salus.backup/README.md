# Backup

Backs up folders **on demand** (no schedule) with [restic](https://restic.net): encrypted, compressed and deduplicated, so every
backup after the first stores only what changed. Restores single files, folders or whole snapshots.

## Bundles

A **bundle** is a set of folders that are backed up together into one restic repository with **its own password**. Bundles live
in the **backup folder** (the plugin setting `backup_root`; the plugin asks for it on first use, and it can be changed in the Salus
settings):

```
<backup folder>/<bundle>/bundle.json   the bundle's folders and exclude patterns
<backup folder>/<bundle>/repo/         the restic repository: its snapshots, encrypted
```

Everything about a bundle is in its folder. To take a bundle to another machine, copy the folder into that machine's backup folder
(USB drive, rsync, a share); it is encrypted, so it can travel over untrusted media. There, open it with its password and restore,
for example into another folder. A plain restic repository put into the backup folder shows up as a bundle too.

## Passwords

A bundle's password is asked when it is first needed (backup, snapshots, restore, clean-up) and kept **only in the open page** while
the bundle is open (**Lock** forgets it). It is never stored: not in the settings, not on disk. The backend hands it to restic on
its standard input, never on the command line or in the environment. **Without the password a bundle cannot be read, and it
cannot be recovered.**

## What you can do

- **New bundle**: name (also its folder name), password, folders, exclude patterns (restic's `--exclude`, e.g. `*.tmp`, `node_modules`).
- **Back up now**: one snapshot of all the bundle's folders, with progress (files, bytes, time left). **Stop** ends it; restic then
  writes no snapshot. Files that cannot be read are listed; the snapshot is kept.
- **Snapshots**: date, machine, folders, size, and how much new data each one stored (compressed).
- **Browse and restore**: open a snapshot as a tree, tick files and folders (or nothing for the whole snapshot) and restore them
  - to their **original locations**, or **into another folder**, with or without the folders above the chosen items
    (`/home/me/data/x` → `<folder>/x`, or `<folder>/home/me/data/x`);
  - **keeping** files that exist already, or **replacing** the ones that differ.
- **Clean up**: keep the newest *N* snapshots, delete the others and free their space.
- **Check integrity**: restic's check of the repository.
- **Edit folders**: the next backup uses the new list; earlier snapshots keep what they had.

One job runs per bundle at a time; it keeps running when the panel is closed.

## Setup

```sh
backend/setup.sh        # or: python3 backend/setup.py (also on Windows)
```

Downloads restic 0.19.1 for the platform (Linux, macOS, Windows; x86-64 and ARM) from the official GitHub release into
`backend/bin`, after checking its SHA-256 against the release's `SHA256SUMS`. Without it, a restic on the PATH is used. The backend
itself needs only Python 3.10+ (standard library).

## Notes

- Restoring to the original locations of a snapshot made on **Windows** is not handled yet; restore into a folder instead.
- File owners and permissions are restored where the operating system allows it; they do not carry over fully between systems.
- restic keeps a cache (in the user's cache folder) that speeds up later backups; it can be deleted at any time.

## Layout

```
manifest.toml, configurable.toml (backup_root), icon.svg
backend/   plugin.py (routes), bundles.py (bundles, jobs), restic.py (running restic), setup.py + setup.sh, run.sh, tests/
frontend/  index.html, app.js, style.css, common.js, picker.js (folder picker), base.css, salus_sdk.js
```

Tests (need restic): `cd backend && python3 -m unittest discover -s tests -t .`
