# Restic Control

A small native macOS app (Python + PyObjC) for browsing a restic repository Finder-style and restoring files.

```
┌ Repository ▾  Settings…   Host: [this Mac ▾]  Browse: [This Mac (local files) ▾]  Refresh  ◌ ┐
├ [Restore] [Manage (Backrest)] ─────────────────────────────────────────────────────────────┤
│ ▾ /Users/me               │ ‹ Back   Versions of /Users/me/Documents/report.docx            │
│   ▸ Desktop               │ Name         Snapshot          Modified          Size  On disk  │
│   ▾ Documents             │ report.docx  2026-09-28 02:00  2026-09-27 17:11  42 KB ✓ same   │
│     report.docx           │ report.docx  2026-09-20 02:00  2026-09-19 09:03  40 KB changed  │
│                           │ [x] Hide unchanged   [Quick Look] [Show in Finder]              │
│                           │                 [Restore Next to Original] [Restore to…]        │
└───────────────────────────┴─────────────────────────────────────────────────────────────────┘
```

Built for **very large repositories** (terabytes, hundreds of millions of files). The app never lists a whole snapshot.

## What it does

- **Left pane: your Mac's own disk**, starting at the backed-up folders (the `paths` of this Mac's snapshots). It's as fast as Finder. You can also switch *Browse* to a single snapshot, which costs one `restic ls` per folder you open.
- **Right pane, file selected:** every version of that file, newest first, found with **one** `restic find` across all snapshots. Identical versions are folded into one row ("×7"). *On disk* shows whether a version matches the current file.
- **Right pane, folder selected:** the folder's distinct **versions**, found by binary search over folder fingerprints (see below). Each row shows the backup range (*Backup* … *Since*), how many snapshots it spans, and *Changes* compared with the previous version: first the direct changes ("2 files changed, 1 folder changed"), then recursive totals from `restic diff` ("in total: 5 changed, 3 added, 1 removed files", restic ≥ 0.17).
- **Several backup plans:** a folder's versions come only from backups that contain the whole folder. A plan that backs up just a subfolder (say, one project inside Documents) is left out of Documents' versions, instead of making everything else look deleted and re-added every time it runs; it still shows for that subfolder, interleaved with the other plan. Only above every backup root (e.g. /Users) are partial backups used, since there are no others. The *Plan* column shows the Backrest plan (its `plan:` tag) of each version.
- **New backups appear by themselves.** Once a minute the app runs `restic list snapshots` (a directory listing of the repository, no snapshot files read); only if the list changed are the snapshots read. New ones are added quietly: the host and snapshot menus keep their choice, the left tree stays, and the right pane is recomputed in the background (fingerprints and diffs are cached, so usually one or two restic calls) and then shows the new version on top, with the rows you had open still open, the selection kept (unless you selected something else meanwhile) and the scroll position kept. Deleted backups disappear the same way. *Refresh* does the same check at once.
- **Browse This Version** (right-click a folder version) opens it **compared with the previous version**. Entries are marked *changed* / *added* / *removed* (removed ones come from the older snapshot, so you can restore them), and unchanged ones are dimmed. *Only show changes* hides the unchanged ones. Double-click a changed subfolder to follow the change down; each step costs at most two cheap calls.
- **See what changed:** the list on the right is a tree: each folder version has a ▸ at the left. Open it (click ▸, double-click, → or Return) to show, right below it, only the items that differ from the previous version: *changed* / *added* / *removed* / *dates/permissions only* (dimmed). Open a changed folder to go one level down, and so on; a folder whose only change is one subfolder opens that one automatically. The `restic diff --metadata` that produces a version's "in total" counts is kept (per pair of snapshots, shared if two parts of the app ask at once): it lists every changed item, so opening levels needs no further restic call. Only sizes and dates, which `restic diff` doesn't report, are read in the background from the two cached folder listings and fill in a moment later. If that diff hasn't finished yet, or is huge (over 300,000 changes: then only its counts are kept), a level is built from those two listings instead. Folders show how many files changed, were added or removed below them (restic ≥ 0.16); files show old → new size. ← closes a row or jumps to its parent. Double-click a folder in the tree to go to it in the left pane — or, if it's no longer on this Mac, to the nearest folder above it that is. The rows are ordinary rows: Quick Look (right-click also offers the previous version), Restore, Calculate Size, *Show in Finder* (the current copy on this Mac; if it's gone, *Show Enclosing Folder in Finder* opens the nearest folder above it) and *Show All Versions* work on them. *Browse This Version* (right-click) still shows a version's complete contents.
- **Quick Look (Space / ⌘Y)** works in both panes. On the left it previews your local file; on the right it previews the backup version. Until a backup file is downloaded, the panel shows a "Fetching … from the backup of …" page with the real file name, and swaps in the file when it arrives. Arrow keys move through the list while the panel is open.
- **Sorting:** click any column header on either side. *View → Keep Folders on Top* is on by default.
  Re-sorting keeps the selected item selected and scrolls it into view.
- **Hidden files:** *View → Show Hidden Files* (⌘⇧., like Finder) shows or hides dotfiles and items macOS marks as hidden (such as `~/Library`), in both panes. The setting is remembered.
- **Columns:** right-click a column header to choose which columns to show (remembered).
- **From Finder:** right-click any file or folder → **Quick Actions** (or **Services**) → **Check in Restic Control**. Dropping items on the app's Dock icon, or `open -a "Restic Control" <path>`, does the same. The app comes forward, expands the tree down to the item and shows its versions. Hidden folders on the way, such as `~/Library`, are shown automatically. A file deleted since the backup opens its folder instead, and a path outside every backed-up folder gets a clear message. This needs the built app (`./build.sh`), not `python main.py`. If the menu item doesn't show up right away, log out and in once, or check System Settings → Keyboard → Keyboard Shortcuts → Services → Files and Folders.
- **Calculate Size** (right-click): for backup folders, restic lists the folder once and the result goes into the Size column. It's kept permanently (snapshots never change), shows up again after a restart, and the restore dialog reuses it, so a restore of that folder knows its size instantly. Backup roots like the whole home folder cost nothing: they use the snapshot's total. On the left pane it adds up the folder's size on this Mac.
- **Right-click menus.** Backup versions: Open This Version, Quick Look, Open Copy / Open With (read-only preview copies), Restore to…, Restore Next to Original, Show Preview Copy in Finder, Copy Path. Local files: Open, Open With, Quick Look, Show in Finder, Copy Path.
- **Restore to…** / **Restore Next to Original:** if the name already exists, a dialog asks what to keep, with a live line *Restore size 412 GB so far · 830 GB free · differs from yours: 8 GB*:
  - **Keep both:** rename the current one so the restored copy gets the name, or give the restored copy a new name. Needs room for a full copy.
  - **Keep only the restored version:** **Update only what changed** (keeps files added since the backup), or **Make it exactly like the backup** (removes them). These work in place, need only the difference, and use restic ≥ 0.17. **Safe** (on by default) instead builds a complete copy first and moves the current version to the Trash only after it succeeded; it switches off once it's clear both copies won't fit.
  - **Restore** can be pressed while the size is still being calculated. It's disabled only for an option already known not to fit (totals only grow). If Keep both doesn't fit, the selection moves to Keep only, unless you already changed it yourself. If the snapshot's own total fits, nothing is calculated. In-place options ask for confirmation, and deletions are permanent. Copies go to a hidden temporary item first, so a failed or cancelled copy never touches your current version.
  - **restic fails** (network drop, lid closed and the connection lost): the partial copy is removed at once.
  - **You quit during a restore:** the app asks first; quitting anyway stops restic and removes the partial copy.
  - **Crash or power loss:** a small journal (`~/Library/Application Support/ResticControl/restores-in-progress.json`) lets the next launch clean up.
  - **While restoring:** the Mac is kept from idle-sleeping; closing the lid still sleeps it.
  - **Progress sheet:** measuring and restoring show a sheet on the window with what's happening, a progress bar (restic's own percentage, bytes and files done, and time left) and **Cancel**. Cancelling a copy removes the partial copy; cancelling Update in Place leaves the files already updated, and running it again finishes the job.
  - **Measuring is skipped when possible:** snapshots made with restic ≥ 0.17 record their total size. That total is an upper bound for anything restored from the snapshot, so if it fits, nothing is measured. For a backed-up root folder (e.g. the whole home folder) it is the exact size.
  - **Free space** is checked before restoring, with a 2 % / 100 MB safety margin (items that go to the Trash still count). If nothing fits, the question offers **Choose Another Location…** as well.
- **Host** defaults to this Mac's hostname, so `find` only searches this machine's snapshots.
- **Manage tab:** Backrest in an embedded web view. The address bar takes a full URL, just a port, or `host:port` of Backrest on another machine. Before loading, the app checks that Backrest really answers there. If it doesn't, the tab explains why instead of showing a blank page:
  - **On this Mac, other port:** if Backrest runs on a different port, the app finds it (via `lsof`) and switches the address.
  - **Installed but stopped:** *Start Backrest* starts it the way it was installed (the `com.backrest` launchd service, `brew services`, or the plain binary). Backrest keeps running after you quit Restic Control.
  - **Remote server not answering:** the tab says so, and reminds you that Backrest only accepts local connections unless it's started with `--bind-address`/`BACKREST_PORT`.

## Setup

```bash
brew install restic                 # or point Settings at your binary
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Build a double-clickable app: `./build.sh`. It runs py2app, signs the app and installs it in /Applications. With Anaconda, `setup.py` bundles conda's shared libraries (libffi, libssl, …) automatically; a non-conda venv gives a smaller app.

**Icon.** `python resources/make_icon.py` regenerates the icon (SVG, PNG, `.icns`); the umbrella is the default. `--motif folder` or `--motif squirrel` draw the alternative versions. Ready-made versions of all three are in `resources/icons/` (switch with e.g. `cp resources/icons/squirrel.icns resources/ResticControl.icns; cp resources/icons/squirrel.png resources/icon.png`). Rendering needs `brew install librsvg` or the cairo library; `--center some.png` puts your own image in the middle instead of the folder, e.g. restic's logo from its repository (`doc/logo/logo.png`, which comes with restic's BSD-2-Clause license; keep restic's copyright notice if you redistribute the app). Then rebuild with `./build.sh`.

**Permissions.** Grant Full Disk Access once, in System Settings → Privacy & Security → Full Disk Access; the app offers to open that pane when it's missing. When the Keychain asks for the password, choose **Always Allow**. macOS ties both to the app's code signature, so run `./make-signing-cert.sh` once (it creates a self-signed code-signing certificate from Terminal, no Keychain Access needed); after that every build keeps them.

### sftp notes

- The repository URL looks like `sftp:user@host:/path/to/repo` or `sftp://user@host:2222//path`.
- A GUI app can't answer ssh prompts, so use **key-based auth** (ssh-agent / Keychain via `UseKeychain yes` in `~/.ssh/config`). Connect once from Terminal first so the host key is known.
- The default extra options `-o sftp.args='-oBatchMode=yes -oServerAliveInterval=30'` make ssh fail fast instead of hanging on a prompt.
- Read-only operations use `--no-lock` so browsing never interferes with a running backup.

## How it talks to restic

| Need | Command | Cost |
|---|---|---|
| snapshot list | `restic snapshots --json` | once |
| left tree (local mode) | `os.scandir` | none |
| versions of a file | `restic find --json [--host H] /exact/path` | 1 process; find skips every subtree that isn't on the way to the file |
| folder fingerprint + listing | `restic cat tree <snap>:<dir>` | 1 process; SHA-256 of the output = the folder's tree id, which covers everything below it; also yields every subfolder's fingerprint |
| folder versions | binary search over fingerprints | unchanged folder: 2 calls; e.g. 3 versions in 100 snapshots: 17 calls in 5 parallel rounds |
| folder changes vs previous version | compare two cached trees | usually free |
| recursive change totals | `restic diff --json A:<dir> B:<dir>` | 1 process per version, skips identical subfolders (restic ≥ 0.17) |
| folder in a snapshot | `restic ls --json <snap> <dir>` | only if the tree isn't cached already |
| Quick Look / restore file | `restic dump <snap> <file>` | 1 process |
| restore folder | `restic restore <snap>:<dir> --target <dest>` | needs restic ≥ 0.14 |

The binary search assumes a folder doesn't change and then change back between two probed snapshots that agree (rare). Fingerprints, trees and listings are cached permanently in SQLite, because snapshots are immutable.

`find` results are stored per file and snapshot, so a file you have looked at before opens instantly, even after a restart, and when new backups arrive only those snapshots are searched (`--snapshot`). While the first search runs, all candidate snapshots are shown greyed out and then weeded out. Selecting something new stops any restic process still running for the previous selection.

Folder listings and `find` results are cached (listings persist in `~/Library/Caches/ResticControl/index/`). When you click through files quickly, the previous `find` is killed, so requests never queue up.

**The remaining floor** is restic's startup: every process loads the repository index. On a 3.5 TB repository that can take several seconds. Measure it with `time restic find /path/to/some/file`; that's how long selecting a file will take. Removing that cost would need a process that stays alive (see next steps).

The streaming full-snapshot indexer (`Restic.index_snapshot`) is still in the backend, but the UI no longer uses it: for billions of files it would be far too big.

## Code layout

```
main.py                    entry point
resticcontrol/backend.py   restic CLI wrapper, find-based history, local-disk helpers (no AppKit; unit-tested)
resticcontrol/index.py     SQLite listing cache, de-duplicated by content hash
resticcontrol/backrest.py  find / check / start a Backrest server
resticcontrol/config.py    settings JSON (~/Library/Application Support/ResticControl) + Keychain
resticcontrol/app.py       main window: outline, versions table, Quick Look, restore, Backrest tab
resticcontrol/settings.py  repository settings window
tests/                     backend tests against throw-away repos, Backrest helpers, and a static check that
                           every method the Cocoa code calls exists: python -m pytest tests
```

## Ideas for next steps

- Drag versions out of the table into Finder (`NSFilePromiseProvider`).
- A long-lived backend so the index is loaded only once: either `restic mount` (needs macFUSE), or a small built-in reader of the restic repository format that keeps the index in memory and reads tree blobs straight from restic's local cache.
- A native "manage" view (snapshots, forget/prune, check) instead of Backrest, built on the same `backend.Restic`.
- A diff between two versions of a text file (for example with `opendiff` / FileMerge).


## Troubleshooting

If the app quits unexpectedly, the Python traceback is printed in the terminal (when started from one) or appended to `~/Library/Logs/Restic Control.log`.
