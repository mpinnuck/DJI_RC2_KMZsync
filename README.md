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

## Platform Support

Windows only (supported):

- This app depends on Windows Shell MTP access (Shell.Application via PowerShell).
- The MTP integration used by this app is a Windows-specific architecture.

macOS (not supported):

- macOS MTP access for RC-2 is not a supported path for this tool.
- In practice, macOS workflows can require stopping/changing multiple services to get stable MTP access, which is outside the intended setup.

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

Platform note:
- This app is supported on Windows only.

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
3. Click Refresh.
4. On first startup refresh, the app saves a baseline GUID list and does not mark rows as new.
5. If first run (or dummy mission changed), select the RC dummy mission slot and click Set Dummy Slot.
6. In PC list, select the new mission source KMZ you want to upload.
7. Click the left COPY button (this will copy the selected PC kmz to the selected RC mission, or to the dummy mission if no RC mission is selected).
8. Verify success in Activity Log and optional Mission Mapping tab.
9. On RC-2, open the dummy mission.
10. The RC will prompt to Select "Adjust and open new mission".
11. Apply mission edits (global speed, review altitudes, signal-loss behavior, etc.).
12. Save the new mission and rename on the RC as needed.
13. Return to the app and click Refresh.
14. Identify the newly created RC mission slot:
  - New GUID rows are highlighted light green for that refresh cycle.
  - On the next refresh, that row returns to normal background.
  - also the new mission will be associated with a PC kmz, the dummy mission will be associated with the new mission
15. Click the new mission to view the larger preview image in the right panel.
16. Double-click the preview image to open a large popup map preview; the popup follows slot selection while open.
17. Select the target PC KMZ filename to overwrite (or leave none selected for GUID default filename).
18. Click the right COPY button (copies the updated RC-2 kmz to the PC) to pull the edited mission back.
19. Confirm the updated file on PC, the new RC mission is now associated the new PC kmz and review mapping timestamp.
20. In the app, click Restore Dummy, this copies the PC dummy.kmz back to the dummy mission.
21. On the RC, open the dummy mission, select "Adjust and open" if propted, then return to the RC mission list and click Save for the dummy mission. The cycle is reset and ready for the next new-mission copy.

Dummy slot notes:
- Left COPY targets the selected RC mission when one is selected.
- Left COPY targets the configured dummy slot only when no RC mission is selected.
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