# DJI RC-2 KMZ Mission Sync

A desktop utility for syncing Dronelink KMZ missions to DJI RC-2 waypoint slots.

This project is designed for Windows + RC-2 workflows where waypoint files are accessed via:
- MTP (Explorer-style device access)
- ADB (optional)
- Local filesystem path (when available)

## What It Does

- Lists RC-2 mission slots from the configured waypoint root.
- Shows mission preview thumbnails in the RC-2 list.
- Displays mission details with:
  - Active KMZ filename
  - GUID slot ID
  - Last modified timestamp (when available)
- Filters RC-2 missions by name, GUID, or timestamp.
- Copies a selected PC KMZ into a selected RC-2 mission slot.
- Runs copy in the background (UI stays responsive).
- Logs all operations in the in-app Activity Log (no modal popups).

## Important RC-2 Behavior

- RC-2 mission display names edited on-device are likely stored in DJI app metadata/index, not inside the KMZ mission payload.
- Overwriting an existing mission file can trigger RC-2 in-app "adjust/open" behavior.
- Creating synthetic mission folders from PC side is not treated as reliable mission creation for RC-2 indexing.

## Project Structure

- `djirc2kmzsync.py`: App entry point.
- `config/`: Config loading and persistence.
- `model/`: Data models (`RC2Mission`, `KMZFile`).
- `view/`: Tkinter UI.
- `viewmodel/`: Sync logic for MTP/ADB/filesystem operations.
- `tests/`: Unit tests.

## Requirements

- Windows
- Python 3.10+ (tested in a venv)
- Pillow (`PIL`) for JPEG preview decode
- Optional: ADB for ADB-mode access

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```powershell
pip install pillow pytest
```

## Run

```powershell
python djirc2kmzsync.py
```

## Configure

The app uses `kmz_sync_config.json` in the project root.

Example:

```json
{
    "rc2_folder": "mtp:DJI RC 2|Internal shared storage|Android|data|dji.go.v5|files|waypoint",
  "pc_folder": "C:\\D_Drive\\Drone\\RC2Missions",
  "rc2_refresh_retry_interval_seconds": 30
}
```

## Usage

1. Start app.
2. Confirm `RC-2 Root` and `PC KMZ Folder`.
3. Wait for initialization and mission load.
4. Filter/select an RC-2 mission.
5. Select a PC KMZ.
6. Click `COPY`.

Copy behavior:
- If an RC-2 mission is selected: copy overwrites that slot silently.
- If no RC-2 mission is selected: copy is blocked with a log/status warning.

## Diagnostics

Use `Inspect Mission` in the Activity Log header to inspect:
- Slot files
- KMZ internal files
- Name/title-like XML fields
- Candidate metadata/history files near the waypoint root

## Tests

Run test suite:

```powershell
python -m pytest tests/test_sync_viewmodel.py tests/test_models.py -q
```

## Notes on Timestamps

- Local filesystem mode: uses file modified time.
- MTP mode: uses Explorer detail metadata when available.
- Invalid MTP sentinel dates (for example `12/30/1899 00:00:00`) are treated as unknown.

## License

No license file is currently included in this repository.