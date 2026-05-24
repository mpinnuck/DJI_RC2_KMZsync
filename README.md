# DJI RC-2 KMZ Mission Sync

Desktop utility for syncing Dronelink KMZ missions between PC and DJI RC-2 mission slots.

Note: This app and workflow are intended for DJI drones where DJI has not provided a public SDK integration path, such as the Air 3S.

## Motivation

This app was created after repeated attempts to use DJI injector-style tools failed in both macOS and Windows VM environments.

In testing, the RC-2 appeared correctly in Windows Explorer inside the VM, so MTP looked available. However, injector workflows still failed. The most likely reason is architectural:

- Explorer and the Windows Shell already own the active MTP session.
- Many injector tools use a lower-level MTP path (for example raw/libmtp-style access) and attempt to open their own device session.
- In VMs, MTP passthrough can be partial: basic browsing works, but some lower-level operations fail or are dropped.

This app uses PowerShell plus Windows Shell COM (Shell.Application), which operates through the same Shell MTP layer as Explorer. That means it works with the session Windows already has, instead of competing for exclusive access.

Practical outcome:

- Reliable behavior in the exact VM setups where injector tools failed.
- No need to eject Explorer access or switch modes just to copy mission files.
- A workflow designed around overwrite-safe dummy slot injection and round-trip editing.

## Quick Start (60 seconds)

```powershell
.venv\Scripts\Activate.ps1
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe djirc2kmzsync.py
```

In the app:
1. Set RC-2 Root and PC KMZ Folder.
2. Click Sync/Refresh and wait for both lists.
3. In RC-2 list, select the mission slot you want to use as dummy, then click Set Dummy Slot.
4. In PC list, select the source KMZ.
5. Click left COPY (PC to RC-2 dummy slot).
6. After RC edits, click right COPY (RC-2 to PC) to pull updates back.

Air 3S note:
- If you build missions in Dronelink, set PC KMZ Folder to your Dronelink KMZ export folder.
- Export new Dronelink missions into that same folder so they appear in the app's PC list for sync.

## User Setup (First Time)

Complete this once before normal use.

1. On the RC, create a dummy mission with 2 waypoints.
  - Open Waypoint mode on the RC.
  - Create a simple mission with exactly two waypoints.
  - Save it with a clear name such as Dummy Mission.
  - This slot will be reused as the overwrite target for PC to RC uploads.
2. Start the RC Sync app.
  - Connect RC-2 to your PC.
  - Launch DJI RC-2 KMZ Mission Sync.
  - Set RC-2 Root and PC KMZ Folder if they are not already set.
  - For Air 3S with Dronelink, point PC KMZ Folder at your Dronelink KMZ export directory.
  - Click Sync/Refresh and wait until the RC and PC lists are populated.
3. Set the dummy mission in the app.
  - In the RC-2 mission list, click the dummy mission you created.
  - Click Set Dummy Slot.
  - Confirm the success message in the status/activity log.
  - The configured dummy slot is highlighted in the RC-2 list.

After setup, normal PC to RC copy only requires selecting a PC KMZ and clicking COPY.

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
- Uploads selected PC KMZ into the configured RC-2 dummy slot (overwrite flow).
- Highlights the configured dummy slot in the RC-2 mission list.
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

The app does not require ADB to run. By default it uses the Windows built-in MTP interface for RC-2 access.

Development/build dependencies:
- pytest
- pyinstaller

Install dependencies in venv:

```powershell
.venv\Scripts\Activate.ps1
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install pyinstaller
```

## ADB Setup (optional)

If you plan to use `adb:` RC-2 paths, install Android Platform Tools on Windows.

Why install ADB if it is optional:
- MTP/Explorer access can be unreliable on some PCs; ADB is a useful fallback path.
- `adb devices` and `adb shell` provide better diagnostics when RC-2 connectivity is unclear.
- ADB enables scriptable, repeatable command-line checks/copy operations.

Install with winget:

```powershell
winget search platform-tools
winget install --id Google.PlatformTools --exact
```

Verify ADB installation:

```powershell
adb version
adb devices
```

On RC-2, enable USB debugging and accept the host authorization prompt.

Verify you can browse RC waypoint storage over ADB:

```powershell
adb shell ls /sdcard/Android/data/dji.go.v5/files/waypoint
```

If needed, try the equivalent path:

```powershell
adb shell ls /storage/emulated/0/Android/data/dji.go.v5/files/waypoint
```

In the app, you can also use the `ADB Status` button in Activity Log as a quick connectivity check.

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
  "rc2_refresh_retry_interval_seconds": 5,
  "dummy_slot_guid": "00000000-0000-0000-0000-000000000000"
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

## Workflow: Add Mission Using Dummy Slot

Use this workflow to push a PC KMZ to RC-2, edit it on the RC, and pull the edited result back.

1. Connect RC-2 and launch the app.
2. Confirm RC-2 Root and PC KMZ Folder paths.
3. Click Sync/Refresh and wait for both lists.
4. If first run (or dummy changed): select the RC dummy mission slot and click Set Dummy Slot.
5. In PC list, select the source KMZ you want to upload.
6. Click the left COPY button (PC to RC-2 dummy slot).
7. Verify success in Activity Log and optional Mission Mapping tab.
8. On RC-2, open the uploaded mission from the dummy slot.
9. Select Adjust and open new mission.
10. Apply mission edits (speed, altitude, signal-loss behavior, etc.).
11. Save the new mission and rename as needed.
12. Return to the app and click Sync/Refresh.
13. Select the edited RC-2 mission slot.
14. Select the target PC KMZ filename to overwrite (or leave none for GUID default).
15. Click the right COPY button (RC-2 to PC) to pull the edited mission back.
16. Confirm updated file on PC and review mapping timestamp.
17. In the app, click Restore Dummy.
18. On the RC, open the dummy mission.
19. Select Adjust and open.
20. Return to the RC mission list and click Save for the dummy mission. The cycle is now reset and ready for the next new-mission copy.

Dummy slot notes:
- Left COPY always targets the configured dummy slot.
- You only need to run Set Dummy Slot again when you want to change dummy target slots.

Copy notes:
- Upload and copy-back overwrite existing target filenames when present.
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