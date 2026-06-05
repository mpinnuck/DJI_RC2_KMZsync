# DJI RC-2 KMZ Mission Sync

Desktop utility for syncing Dronelink KMZ missions between your computer and DJI RC-2 mission slots.

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

## Platform Support

Fully supported platforms:

- Windows (PyInstaller onedir build via `DJI_RC2_KMZsync_w.spec`)
- macOS (PyInstaller onedir + `.app` build via `DJI_RC2_KMZsync_m.spec`)

## Quick Start (60 seconds)

Windows:

```powershell
.venv\Scripts\Activate.ps1
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe djirc2kmzsync.py
```

macOS:

```bash
source .venvm/bin/activate
./.venvm/bin/python -m pip install -r requirements.txt
./.venvm/bin/python djirc2kmzsync.py
```

In the app:
1. Set RC-2 Root and PC KMZ Folder.
2. Click Sync/Refresh and wait for both lists.
3. In RC-2 list, select the mission slot you want to use as dummy, then click Set Dummy Slot.
4. In PC list, select the source KMZ.
5. Click left COPY (PC to selected RC mission, or to dummy slot if no RC mission is selected).
6. After RC edits, click right COPY (RC-2 to PC) to pull updates back.

Air 3S note:
- If you build missions in Dronelink, set PC KMZ Folder to your Dronelink KMZ export folder.
- Export new Dronelink missions into that same folder so they appear in the app's PC list for sync.
- No manual `dummy.kmz` file creation is required.
- When the configured dummy slot is selected in RC-2 and you click right COPY without selecting a PC target, the app creates/updates `dummy.kmz` in the PC KMZ folder root.

## User Setup (First Time)

Complete this once before normal use.

1. Create the dummy baseline on RC (2-waypoint mission).
  - Open Waypoint mode on the RC.
  - Create a simple mission with exactly two waypoints.
  - Save it with a clear name such as Dummy Mission.
  - This slot will be reused as the overwrite target for PC to RC uploads.
2. Start the RC Sync app.
  - Connect RC-2 to your computer.
  - Launch DJI RC-2 KMZ Mission Sync.
  - Set RC-2 Root and PC KMZ Folder if they are not already set.
  - For Air 3S with Dronelink, point PC KMZ Folder at your Dronelink KMZ export directory.
  - Click Sync/Refresh and wait until the RC and PC lists are populated.
3. Set the dummy mission in the app.
  - In the RC-2 mission list, click the dummy mission you created.
  - Click Set Dummy Slot.
  - Confirm the success message in the status/activity log.
  - The configured dummy slot is highlighted in the RC-2 list.
4. Create `dummy.kmz` on PC using copy-back from the dummy slot.
  - Keep the configured dummy slot selected in the RC-2 list.
  - Click right COPY (RC-2 to PC) with no PC target file selected.
  - The app automatically writes the mission to `dummy.kmz` in the PC KMZ folder root.
  - Confirm `dummy.kmz` appears in the PC list after Sync/Refresh.

How the dummy is used:
- Left COPY writes the selected PC KMZ into the selected RC mission slot.
- If no RC mission is selected, Left COPY writes to the configured dummy slot.
- RC-2 then creates a new mission from that loaded content when you use Adjust and open new mission and save.
- Right COPY with the dummy slot selected and no PC target selected writes to `dummy.kmz` in the PC KMZ folder root.
- Restore Dummy writes `dummy.kmz` from the PC KMZ folder back into the dummy slot.

After setup, normal PC to RC copy only requires selecting a PC KMZ and clicking COPY.

## Build Quick Start

## Zip Source Files

Create a zip containing all tracked source files (including local uncommitted edits):

```bash
./zip_sources.sh
```

Output is written to:

- `artifacts/source-archives/DJI_RC2_KMZsync-source-YYYYMMDD-HHMMSS.zip`

Generated zip archives are ignored by git.

Windows build:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath dist DJI_RC2_KMZsync_w.spec
```

macOS build:

```bash
./.venvm/bin/python -m pip install -r requirements.txt
./.venvm/bin/python -m PyInstaller --noconfirm --clean --distpath distm DJI_RC2_KMZsync_m.spec
```

Distribute:

- Windows: full `dist\DJI_RC2_KMZsync` folder
- macOS: `distm/DJI_RC2_KMZsync.app` (or `DJI_RC2_KMZsync_macos.zip` from Releases)

## GitHub Release Builds

This repository includes a GitHub Actions workflow that builds both platforms and publishes assets on tags.

- Workflow file: .github/workflows/release-build.yml
- Trigger release build: push a tag like v1.0.0
- Also supports manual runs via workflow_dispatch

Release assets:

- DJI_RC2_KMZsync_windows.zip
- DJI_RC2_KMZsync_macos.zip

After downloading:

1. Extract the zip.
2. Launch the app.
3. Set PC KMZ Folder.
4. Set RC-2 Root (MTP or ADB as needed).

After those two folders are set, normal sync/copy flows are ready to use.

Supported RC-2 access modes:
- MTP (recommended; Explorer-style on Windows)
- ADB (optional, untested)
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
- Windows 10/11 or macOS
- Python 3.10+ (tested with Python 3.14 in venv)
- Pillow (mission preview decode)

Optional tools:
- ADB in PATH (only required for adb: mode)

The app does not require ADB to run.
- On Windows, it uses the built-in Explorer/Shell MTP interface.
- On macOS, it uses libmtp/pymtp-based access.

macOS note (important):
- RC-2 waypoint files can reject standard MTP `GetObject` reads (`get_file_to_file`) while still allowing `GetPartialObject` reads.
- The macOS backend uses chunked `GetPartialObject` reads first, then falls back to `GetObject` only when needed.
- This avoids competing MTP sessions and resolves the intermittent copy-back/preview failures seen with CLI-based reads.
- Install libmtp with:

```bash
brew install libmtp
```

Development/build dependencies:
- pytest
- pyinstaller

Install dependencies in venv:

Windows:

```powershell
.venv\Scripts\Activate.ps1
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install pyinstaller
```

macOS:

```bash
source .venvm/bin/activate
./.venvm/bin/python -m pip install -r requirements.txt
./.venvm/bin/python -m pip install pyinstaller
```

## ADB Setup (optional)

If you plan to use `adb:` RC-2 paths, install Android Platform Tools.

Important:
- The RC-2 must have USB debugging (developer/debug mode) enabled before ADB will connect.
- ADB support in this app is currently untested and should be treated as experimental.

Why install ADB if it is optional:
- MTP/Explorer access can be unreliable on some PCs; ADB is a useful fallback path.
- `adb devices` and `adb shell` provide better diagnostics when RC-2 connectivity is unclear.
- ADB enables scriptable, repeatable command-line checks/copy operations.

Windows install with winget:

```powershell
winget search platform-tools
winget install --id Google.PlatformTools --exact
```

Verify ADB installation (Windows/macOS):

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

Windows:

```powershell
.venv\Scripts\python.exe djirc2kmzsync.py
```

macOS:

```bash
./.venvm/bin/python djirc2kmzsync.py
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

## Build (PyInstaller, Windows and macOS)

Windows:

```powershell
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath dist DJI_RC2_KMZsync_w.spec
```

Windows output:

```text
dist\DJI_RC2_KMZsync\
  DJI_RC2_KMZsync.exe
  _internal\
  kmz_sync_config.json
  kmz_copy_map.json
```

macOS:

```bash
./.venvm/bin/python -m PyInstaller --noconfirm --clean --distpath distm DJI_RC2_KMZsync_m.spec
```

macOS output:

```text
distm/
  DJI_RC2_KMZsync.app
  DJI_RC2_KMZsync/
```

Important:

- Windows: distribute/copy the entire `dist\DJI_RC2_KMZsync` folder.
- macOS: distribute `DJI_RC2_KMZsync.app` (or the release zip).

## Workflow: Add Mission Using Dummy Slot

Use this workflow to push a PC KMZ to RC-2, edit it on the RC, and pull the edited result back.

1. Connect RC-2 and launch the app.
2. Confirm RC-2 Root and PC KMZ Folder paths.
3. Click Refresh.
4. On first startup refresh, the app saves a baseline GUID list and does not mark rows as new.
5. If first run (or dummy mission changed), select the RC dummy mission slot and click Set Dummy Slot.
6. In PC list, select the new mission source KMZ you want to upload.
7. Click the left COPY button (this will copy the selected PC kmz to the selected RC mission, or to the dummy mission if no RC mission is selected).
8. Verify success in Activity Log and optional Mission Mapping tab.
9. On RC-2, open the dummy mission.
10. Verify the new mission is loaded correctly.
11. Apply mission edits (global speed, review altitudes, signal-loss behavior, etc.).
12. Return to the mission history list.
13. Press Save and select "Save As". A new mission will be created.
14. In the RC UI, rename the new mission as required.
15. Return to the app and click Refresh.
16. The newly created mission will be highlighted in light green after the refresh:
  - New GUID rows are highlighted light green for that refresh cycle.
  - On the next refresh, that row returns to normal background.
17. Click the new mission to view the larger preview image in the right panel.
18. Double-click the preview image to open a large popup map preview; the popup follows slot selection while open.
19. Use the preview image to identify which new RC mission GUID corresponds to your intended PC KMZ target.
20. Select the target PC KMZ filename to overwrite.
21. Click the right COPY button (copies the updated RC-2 kmz to the PC) to pull the edited mission back.
22. Confirm the updated file on PC and review Mission Mapping. This RC-to-PC copy-back updates the mission GUID-to-PC KMZ association and timestamp.
23. In the app, click Restore Dummy. This copies the PC `dummy.kmz` back to the dummy mission.
24. On the RC, open the dummy mission, select "Adjust and open" if prompted, then return to the RC mission list and click Save for the dummy mission. The cycle is reset and ready for the next new-mission copy.

Preview note:

- On RC-2 you can zoom the waypoint map so the mission occupies the full RC-2 display.
- Then go back to the history list and press Save to store a new preview image.
- In KMZ Sync, click the Refresh Preview button above the log to fetch updated previews.
- The app caches preview images for list-refresh performance.

Dummy slot notes:
- Left COPY targets the selected RC mission when one is selected.
- Left COPY targets the configured dummy slot only when no RC mission is selected.
- You only need to run Set Dummy Slot again when you want to change dummy target slots.

Copy notes:
- Upload and copy-back overwrite existing target filenames when present.
- In this workflow, the preview image is the practical way to identify which new RC mission GUID matches your intended PC KMZ.
- RC-to-PC copy-back to the selected target PC KMZ updates Mission Mapping so the latest GUID-to-PC KMZ relationship is visible.

## Diagnostics

Use Activity Log actions:
- Quick Inspect: fast slot + KMZ structure checks.
- Deep Inspect: includes metadata-history and binary candidate probing.
- Detect RC-2 and ADB Status: connectivity checks.

## Tests

Run all tests:

Windows:

```powershell
.venv\Scripts\python.exe -m pytest
```

macOS:

```bash
./.venvm/bin/python -m pytest
```

## Notes on Timestamps

- Filesystem mode uses local file modified timestamps.
- MTP mode uses Explorer details metadata when available.
- Invalid sentinel MTP dates (for example 12/30/1899 00:00:00) are treated as unknown.

## License

No license file is currently included in this repository.