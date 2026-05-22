# DJI RC-2 KMZ Mission Sync

Desktop utility for syncing Dronelink KMZ missions between PC and DJI RC-2 mission slots.

## Quick Start (60 seconds)

```powershell
.venv\Scripts\Activate.ps1
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe djirc2kmzsync.py
```

In the app:
1. Set RC-2 Root and PC KMZ Folder.
2. Click Sync and wait for both lists.
3. Select RC-2 mission slot + PC KMZ.
4. Click left COPY (PC to RC-2).
5. After RC edits, click right COPY (RC-2 to PC) to pull updates back.

## Build Quick Start

```powershell
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean DJI_RC2_KMZsync.spec
```

Distribute the full dist\DJI_RC2_KMZsync folder (exe + _internal).

Supported RC-2 access modes:
- MTP (recommended, Explorer-style)
- ADB (optional)
- Local filesystem path (when available)

## Features

- Loads RC-2 mission slots and PC KMZ files side-by-side.
- Shows RC-2 mission previews (Pillow-backed image decode).
- Sorts RC-2 missions newest-first when timestamp is available.
- Uploads PC KMZ into a selected RC-2 mission slot (overwrite flow).
- Copies RC-2 mission KMZ back to PC using selected PC filename.
- Tracks source-to-slot mapping in Mission Mapping tab.
- Provides Quick Inspect and Deep Inspect mission diagnostics.
- Runs copy/delete/refresh operations in background threads.

## Dependencies

Runtime dependencies:
- Windows 10/11
- Python 3.10+ (tested with Python 3.14 in venv)
- Pillow (mission preview decode)

Optional tools:
- ADB in PATH (only required for adb: mode)

Development/build dependencies:
- pytest
- pyinstaller

Install dependencies in venv:

```powershell
.venv\Scripts\Activate.ps1
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install pyinstaller
```

## Run From Source

```powershell
.venv\Scripts\python.exe djirc2kmzsync.py
```

## Configuration Files

The app uses two JSON files in the runtime base directory:
- kmz_sync_config.json
- kmz_copy_map.json

In source runs, this is the project working directory.
In packaged runs, this is the folder containing DJI_RC2_KMZsync.exe.

If missing, both files are auto-created on first launch.

Example kmz_sync_config.json:

```json
{
  "rc2_folder": "mtp:DJI RC 2|Internal shared storage|Android|data|dji.go.v5|files|waypoint",
  "pc_folder": "C:\\Drone\\RC2Missions",
  "rc2_refresh_retry_interval_seconds": 30
}
```

## Build (PyInstaller, onedir)

The repository includes DJI_RC2_KMZsync.spec configured for onedir output.

Build command:

```powershell
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean DJI_RC2_KMZsync.spec
```

Output folder:

```text
dist\DJI_RC2_KMZsync\
  DJI_RC2_KMZsync.exe
  _internal\
  kmz_sync_config.json
  kmz_copy_map.json
```

Important:
- Distribute/copy the entire dist\DJI_RC2_KMZsync folder.
- The exe requires _internal beside it in onedir mode.

## Workflow: Add New KMZ, Fly, Edit On RC, Copy Back

1. Connect RC-2 and launch the app.
2. Confirm RC-2 Root and PC KMZ Folder paths.
3. Click Sync and wait for both lists to load.
4. In RC-2 mission list, select an existing mission slot to overwrite.
5. In PC list, select the source KMZ.
6. Click the left COPY button (PC to RC-2).
7. Verify success in Activity Log and optional Mission Mapping tab.
8. Fly the mission. If RC signal is lost, DJI may prompt mission adjust/open behavior.
9. After editing on RC-2, reconnect and refresh.
10. Select the edited RC-2 mission slot.
11. Select the target PC KMZ filename to overwrite (or leave none for GUID default).
12. Click the right COPY button (RC-2 to PC) to pull mission back.
13. Confirm updated file on PC and review mapping row timestamp.

Copy notes:
- Upload and copy-back both overwrite existing target filenames when present.
- Copy-back updates mapping so latest source/target relationship is visible.

## Diagnostics

Use Activity Log actions:
- Quick Inspect: fast slot + KMZ structure checks.
- Deep Inspect: includes metadata-history and binary candidate probing.
- Detect RC-2 and ADB Status: connectivity checks.

## Tests

Run all tests:

```powershell
.venv\Scripts\python.exe -m pytest
```

## Notes on Timestamps

- Filesystem mode uses local file modified timestamps.
- MTP mode uses Explorer details metadata when available.
- Invalid sentinel MTP dates (for example 12/30/1899 00:00:00) are treated as unknown.

## License

No license file is currently included in this repository.