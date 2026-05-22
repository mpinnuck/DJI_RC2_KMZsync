import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime
from typing import Any, List, Tuple

try:
    from PIL import Image
except ImportError:
    Image = None

from config.config_manager import ConfigManager, get_runtime_base_dir
from model.kmz_file import KMZFile
from model.rc2_mission import RC2Mission


class SyncViewModel:
    """
    All business logic for the KMZ sync operation.
    No dependency on tkinter — purely data and file operations.
    """

    DEFAULT_ADB_RC2_ROOT = "adb:/sdcard/Android/data/dji.go.v5/files/waypoint"
    DEFAULT_MTP_RC2_ROOT = (
        "mtp:DJI RC 2|Internal shared storage|Android|data|dji.go.v5|files|waypoint"
    )
    POWERSHELL_TIMEOUT_SECONDS = 30
    MTP_LIST_TIMEOUT_SECONDS = 120
    MTP_COPY_TIMEOUT_SECONDS = 30
    _RC2_NON_MISSION_FOLDERS = {"capability", "map_preview"}
    COPY_MAP_FILE = "kmz_copy_map.json"

    def __init__(self, config: ConfigManager, copy_map_path: str | None = None):
        self._config = config
        self._last_error: str | None = None
        self._mtp_preview_items_cache: dict[str, List[dict[str, Any]]] = {}
        if copy_map_path:
            self._copy_map_path = copy_map_path
        else:
            base_dir = get_runtime_base_dir()
            self._copy_map_path = os.path.join(base_dir, self.COPY_MAP_FILE)
        self._ensure_copy_map_exists()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _default_copy_map_payload() -> dict[str, Any]:
        return {
            "updated_at": "",
            "note": (
                "This map tracks file-level copy operations only. If RC-2 is opened with "
                "'adjust/open as new', DJI app metadata may create a new mission record that "
                "diverges from this mapping."
            ),
            "by_source": {},
        }

    def _ensure_copy_map_exists(self) -> None:
        if os.path.isfile(self._copy_map_path):
            return
        self._save_copy_map(self._default_copy_map_payload())

    def _load_copy_map(self) -> dict[str, Any]:
        if not os.path.isfile(self._copy_map_path):
            return self._default_copy_map_payload()

        try:
            with open(self._copy_map_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                loaded.setdefault("updated_at", "")
                loaded.setdefault("note", "")
                loaded.setdefault("by_source", {})
                if not isinstance(loaded.get("by_source"), dict):
                    loaded["by_source"] = {}
                return loaded
        except (OSError, json.JSONDecodeError):
            pass

        return self._default_copy_map_payload()

    def _save_copy_map(self, payload: dict[str, Any]) -> None:
        try:
            with open(self._copy_map_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=4)
        except OSError:
            # Copy must still be considered successful even if map persistence fails.
            return

    def _record_copy_mapping(self, source: KMZFile, mission: RC2Mission, dest_filename: str) -> None:
        payload = self._load_copy_map()
        by_source = payload.get("by_source") if isinstance(payload.get("by_source"), dict) else {}
        payload["by_source"] = by_source

        source_key = source.filename
        entry = by_source.get(source_key)
        if not isinstance(entry, dict):
            entry = {"history": []}

        history = entry.get("history") if isinstance(entry.get("history"), list) else []
        row = {
            "copied_at": self._now_iso(),
            "source_filename": source.filename,
            "source_full_path": source.full_path,
            "target_mission_guid": mission.guid,
            "target_kmz_filename": dest_filename,
            "target_folder_path": mission.full_folder_path,
            "connection_mode": self.get_rc2_connection_mode(),
        }
        history.append(row)
        entry["history"] = history[-25:]
        entry["last"] = row
        by_source[source_key] = entry

        payload["updated_at"] = self._now_iso()
        self._save_copy_map(payload)

    def get_copy_mapping_summary(self) -> tuple[list[dict[str, str]], str, str]:
        payload = self._load_copy_map()
        by_source = payload.get("by_source") if isinstance(payload.get("by_source"), dict) else {}

        rows: list[dict[str, str]] = []
        for source_name, value in by_source.items():
            if not isinstance(value, dict):
                continue

            last = value.get("last") if isinstance(value.get("last"), dict) else None
            if not last:
                history = value.get("history") if isinstance(value.get("history"), list) else []
                if history:
                    candidate = history[-1]
                    if isinstance(candidate, dict):
                        last = candidate
            if not last:
                continue

            rows.append(
                {
                    "source_filename": str(last.get("source_filename") or source_name or ""),
                    "source_full_path": str(last.get("source_full_path") or ""),
                    "target_mission_guid": str(last.get("target_mission_guid") or ""),
                    "target_kmz_filename": str(last.get("target_kmz_filename") or ""),
                    "target_folder_path": str(last.get("target_folder_path") or ""),
                    "connection_mode": str(last.get("connection_mode") or ""),
                    "copied_at": str(last.get("copied_at") or ""),
                }
            )

        rows.sort(key=lambda row: row.get("copied_at") or "", reverse=True)
        updated_at = str(payload.get("updated_at") or "")
        note = str(payload.get("note") or "")
        return rows, updated_at, note

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_adb_path(path: str) -> bool:
        return path.strip().lower().startswith("adb:")

    @staticmethod
    def _is_mtp_path(path: str) -> bool:
        return path.strip().lower().startswith("mtp:")

    @staticmethod
    def _adb_remote_root(path: str) -> str:
        raw = path.strip()[4:].strip()
        if not raw:
            return SyncViewModel.DEFAULT_ADB_RC2_ROOT[4:]
        if not raw.startswith("/"):
            raw = f"/{raw}"
        return raw.replace("\\", "/")

    @staticmethod
    def _adb_remote_join(root: str, name: str) -> str:
        return f"{root.rstrip('/')}/{name}"

    @staticmethod
    def _format_display_datetime(dt: datetime) -> str:
        return dt.strftime("%d/%m/%Y %H:%M:%S")

    @classmethod
    def _format_local_mtime(cls, path: str) -> str:
        try:
            ts = os.path.getmtime(path)
            return cls._format_display_datetime(datetime.fromtimestamp(ts))
        except OSError:
            return ""

    @classmethod
    def _normalize_mtp_modify_date(cls, raw_value: str) -> str:
        raw = (raw_value or "").strip()
        if not raw:
            return ""

        if raw.startswith("12/30/1899") or raw.startswith("30/12/1899") or raw.startswith("1899-12-30"):
            return ""

        parse_formats = [
            "%m/%d/%Y %I:%M %p",
            "%m/%d/%Y %H:%M:%S",
            "%d/%m/%Y %I:%M %p",
            "%d/%m/%Y %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ]

        for fmt in parse_formats:
            try:
                parsed = datetime.strptime(raw, fmt)
            except ValueError:
                continue

            if parsed.year <= 1900:
                return ""
            return cls._format_display_datetime(parsed)

        return raw

    @classmethod
    def _mtp_segments(cls, path: str) -> List[str]:
        raw = path.strip()[4:].strip()
        if not raw:
            raw = cls.DEFAULT_MTP_RC2_ROOT[4:]
        return [segment.strip() for segment in raw.split("|") if segment.strip()]

    @staticmethod
    def _mtp_join(path: str, name: str) -> str:
        prefix = path.strip()
        separator = "" if prefix.endswith("|") else "|"
        return f"{prefix}{separator}{name}"

    @classmethod
    def _is_rc2_slot_name(cls, name: str) -> bool:
        return name.strip().lower() not in cls._RC2_NON_MISSION_FOLDERS

    @staticmethod
    def _adb_from_env() -> str | None:
        raw = (os.environ.get("ADB") or "").strip().strip('"')
        if not raw:
            return None
        if os.path.isdir(raw):
            exe = "adb.exe" if os.name == "nt" else "adb"
            return os.path.join(raw, exe)
        return raw

    @staticmethod
    def _adb_common_candidates() -> List[str]:
        exe = "adb.exe" if os.name == "nt" else "adb"
        roots = [
            os.environ.get("ANDROID_SDK_ROOT", ""),
            os.environ.get("ANDROID_HOME", ""),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk"),
            os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "Android", "Sdk"),
        ]
        candidates: List[str] = []
        for root in roots:
            cleaned = (root or "").strip().strip('"')
            if not cleaned:
                continue
            candidates.append(os.path.join(cleaned, "platform-tools", exe))
            candidates.append(os.path.join(cleaned, exe))
        return candidates

    @classmethod
    def _resolve_adb_executable(cls) -> str | None:
        exe = "adb.exe" if os.name == "nt" else "adb"

        # In VS Code debug sessions PATH can be stale; prefer the active Python
        # environment's scripts/bin directory when adb is dropped there.
        py_dir = os.path.dirname(sys.executable)
        py_dir_adb = os.path.join(py_dir, exe)
        if os.path.isfile(py_dir_adb):
            return py_dir_adb

        from_path = shutil.which("adb")
        if from_path:
            return from_path

        env_path = cls._adb_from_env()
        if env_path and os.path.isfile(env_path):
            return env_path

        for candidate in cls._adb_common_candidates():
            if os.path.isfile(candidate):
                return candidate

        return None

    @staticmethod
    def _format_adb_error(output: str) -> str:
        text = (output or "").strip()
        lower = text.lower()

        if "device offline" in lower:
            return (
                "ADB device is offline. Reconnect the RC-2 USB cable, unlock/confirm USB debugging "
                "on the RC-2, then verify with 'adb devices' until it shows state 'device'."
            )
        if "unauthorized" in lower:
            return (
                "ADB device is unauthorized. Accept the USB debugging authorization prompt on the RC-2 "
                "and retry."
            )
        if "no devices/emulators found" in lower:
            return (
                "No ADB device detected. Connect the RC-2 via USB, enable USB debugging, and retry."
            )
        return text

    @staticmethod
    def _powershell_executable() -> str | None:
        return shutil.which("powershell") or shutil.which("pwsh")

    @classmethod
    def _run_powershell(
        cls,
        script: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, str]:
        powershell = cls._powershell_executable()
        if not powershell:
            return False, "PowerShell executable not found."

        effective_timeout = timeout_seconds or cls.POWERSHELL_TIMEOUT_SECONDS

        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            result = subprocess.run(
                [powershell, "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                check=False,
                timeout=effective_timeout,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except KeyboardInterrupt:
            return False, "PowerShell command was interrupted."
        except subprocess.TimeoutExpired:
            return False, f"PowerShell command timed out after {effective_timeout} seconds."
        except OSError:
            return False, "Failed to launch PowerShell."

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode != 0:
            return False, stderr or stdout or "PowerShell command failed."

        return True, stdout

    @classmethod
    def _run_powershell_json(
        cls,
        script: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, List[dict[str, Any]] | str]:
        ok, payload = cls._run_powershell(script, timeout_seconds=timeout_seconds)
        if not ok:
            return False, payload

        if not payload:
            return True, []

        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            return False, payload

        if isinstance(decoded, list):
            return True, [item for item in decoded if isinstance(item, dict)]
        if isinstance(decoded, dict):
            return True, [decoded]
        return True, []

    @staticmethod
    def _ps_single_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @classmethod
    def _ps_array_literal(cls, values: List[str]) -> str:
        if not values:
            return "@()"
        quoted = ", ".join(cls._ps_single_quote(value) for value in values)
        return f"@({quoted})"

    @classmethod
    def _mtp_script(cls, mtp_path: str, body: str) -> str:
        segments = cls._ps_array_literal(cls._mtp_segments(mtp_path))
        return f"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject Shell.Application
$current = $shell.Namespace(17)
if (-not $current) {{
    throw 'This PC shell namespace is unavailable.'
}}

$segments = {segments}
foreach ($segment in $segments) {{
    $item = $current.Items() | Where-Object {{ $_.Name -eq $segment }} | Select-Object -First 1
    if (-not $item) {{
        throw "MTP path segment not found: $segment"
    }}
    $current = $item.GetFolder
    if (-not $current) {{
        throw "MTP path segment is not a folder: $segment"
    }}
}}

{body}
"""

    def _list_mtp_items(self, mtp_path: str) -> Tuple[bool, List[dict[str, Any]] | str]:
        script = self._mtp_script(
            mtp_path,
            """
$modifiedIndex = $null
for ($i = 0; $i -lt 320; $i++) {
    $label = $current.GetDetailsOf($null, $i)
    if (-not $label) {
        continue
    }
    $normalized = ($label -as [string]).Trim().ToLowerInvariant()
    if ($normalized -in @('date modified', 'modified', 'modification date', 'date de modification')) {
        $modifiedIndex = $i
        break
    }
}

$items = @(
    $current.Items() | Select-Object Name,
        @{Name='IsFolder'; Expression={ [bool]$_.IsFolder }},
        @{Name='ModifyDate'; Expression={ [string]$_.ModifyDate }},
        @{Name='ModifyDateDetail'; Expression={ if ($modifiedIndex -ne $null) { [string]$current.GetDetailsOf($_, $modifiedIndex) } else { '' } }}
)
if ($items) {
    $items | ConvertTo-Json -Compress
}
""",
        )
        return self._run_powershell_json(
            script,
            timeout_seconds=self.MTP_LIST_TIMEOUT_SECONDS,
        )

    def _list_mtp_missions_bulk(self, mtp_path: str) -> Tuple[bool, List[dict[str, Any]] | str]:
        script = self._mtp_script(
            mtp_path,
            """
$result = @()

$slotFolders = @(
    $current.Items() |
        Where-Object {
            $_.IsFolder -and
            $_.Name -ne 'capability' -and
            $_.Name -ne 'map_preview'
        } |
        Sort-Object Name
)

foreach ($slot in $slotFolders) {
    $slotFolder = $slot.GetFolder
    if (-not $slotFolder) {
        continue
    }

    $modifiedIndex = $null
    for ($i = 0; $i -lt 320; $i++) {
        $label = $slotFolder.GetDetailsOf($null, $i)
        if (-not $label) {
            continue
        }
        $normalized = ($label -as [string]).Trim().ToLowerInvariant()
        if ($normalized -in @('date modified', 'modified', 'modification date', 'date de modification')) {
            $modifiedIndex = $i
            break
        }
    }

    $kmzItems = @(
        $slotFolder.Items() |
            Where-Object {
                (-not $_.IsFolder) -and
                ($_.Name -match '(?i)\\.kmz$')
            } |
            Sort-Object Name
    )

    $kmzName = ''
    $modifyDate = ''
    $modifyDateDetail = ''
    if ($kmzItems.Count -gt 0) {
        $first = $kmzItems[0]
        $kmzName = [string]$first.Name
        $modifyDate = [string]$first.ModifyDate
        if ($modifiedIndex -ne $null) {
            $modifyDateDetail = [string]$slotFolder.GetDetailsOf($first, $modifiedIndex)
        }
    }

    $result += [PSCustomObject]@{
        Name = [string]$slot.Name
        KMZName = $kmzName
        ModifyDate = $modifyDate
        ModifyDateDetail = $modifyDateDetail
    }
}

if ($result) {
    $result | ConvertTo-Json -Compress
}
""",
        )
        return self._run_powershell_json(
            script,
            timeout_seconds=self.MTP_LIST_TIMEOUT_SECONDS,
        )

    def _copy_file_to_mtp_folder(self, mtp_folder: str, local_source_path: str) -> Tuple[bool, str]:
        script = self._mtp_script(
            mtp_folder,
            f"""
$sourcePath = {self._ps_single_quote(local_source_path)}
if (-not (Test-Path -LiteralPath $sourcePath)) {{
    throw "Source file not found: $sourcePath"
}}

$sourceName = [System.IO.Path]::GetFileName($sourcePath)
$existing = $current.Items() | Where-Object {{ $_.Name -eq $sourceName }} | Select-Object -First 1
if ($existing) {{
    $existing.InvokeVerb('delete')

    $deleteDeadline = (Get-Date).AddSeconds(10)
    do {{
        $remaining = $current.Items() | Where-Object {{ $_.Name -eq $sourceName }} | Select-Object -First 1
        if (-not $remaining) {{
            break
        }}
        [System.Threading.Thread]::Sleep(200)
    }} while ((Get-Date) -lt $deleteDeadline)

    $remaining = $current.Items() | Where-Object {{ $_.Name -eq $sourceName }} | Select-Object -First 1
    if ($remaining) {{
        throw "MTP overwrite failed to remove existing file: $sourceName"
    }}
}}

$current.CopyHere($sourcePath, 0x614)

$copyDeadline = (Get-Date).AddSeconds(15)
$copied = $null
do {{
    $copied = $current.Items() | Where-Object {{ $_.Name -eq $sourceName }} | Select-Object -First 1
    if ($copied) {{
        break
    }}
    [System.Threading.Thread]::Sleep(200)
}} while ((Get-Date) -lt $copyDeadline)

if (-not $copied) {{
    throw "MTP copy did not complete for $sourceName"
}}

Write-Output $sourceName
""",
        )
        return self._run_powershell(
            script,
            timeout_seconds=self.MTP_COPY_TIMEOUT_SECONDS,
        )

    def _create_mtp_slot_folder(self, mtp_root: str, guid: str) -> Tuple[bool, str]:
        script = self._mtp_script(
            mtp_root,
            f"""
$folderName = {self._ps_single_quote(guid)}
$existing = $current.Items() | Where-Object {{ $_.Name -eq $folderName -and $_.IsFolder }} | Select-Object -First 1
if (-not $existing) {{
    $current.NewFolder($folderName)
}}

$deadline = (Get-Date).AddSeconds(10)
do {{
    $existing = $current.Items() | Where-Object {{ $_.Name -eq $folderName -and $_.IsFolder }} | Select-Object -First 1
    if ($existing) {{
        break
    }}
    [System.Threading.Thread]::Sleep(200)
}} while ((Get-Date) -lt $deadline)

if (-not $existing) {{
    throw "MTP slot folder creation failed: $folderName"
}}

Write-Output $folderName
""",
        )
        return self._run_powershell(
            script,
            timeout_seconds=self.MTP_COPY_TIMEOUT_SECONDS,
        )

    def _copy_file_from_mtp_folder(
        self,
        mtp_folder: str,
        filename: str,
        local_dest_path: str,
    ) -> Tuple[bool, str]:
        script = self._mtp_script(
            mtp_folder,
            f"""
$filename = {self._ps_single_quote(filename)}
$destPath = {self._ps_single_quote(local_dest_path)}
$destDir = Split-Path -Parent $destPath

if (-not (Test-Path -LiteralPath $destDir)) {{
    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
}}

$item = $current.Items() | Where-Object {{ $_.Name -eq $filename }} | Select-Object -First 1
if (-not $item) {{
    throw "MTP file not found: $filename"
}}

if (Test-Path -LiteralPath $destPath) {{
    Remove-Item -LiteralPath $destPath -Force
}}

$stageDir = Join-Path $destDir ([Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $stageDir | Out-Null

$shell = New-Object -ComObject Shell.Application
$destination = $shell.Namespace($stageDir)
if (-not $destination) {{
    throw "Local staging folder unavailable: $stageDir"
}}

$destination.CopyHere($item, 0x614)

$copyDeadline = (Get-Date).AddSeconds(15)
$stagedPath = Join-Path $stageDir $filename
do {{
    if (Test-Path -LiteralPath $stagedPath) {{
        break
    }}
    [System.Threading.Thread]::Sleep(200)
}} while ((Get-Date) -lt $copyDeadline)

if (-not (Test-Path -LiteralPath $stagedPath)) {{
    throw "MTP preview copy did not complete for $filename"
}}

Move-Item -LiteralPath $stagedPath -Destination $destPath -Force

if (Test-Path -LiteralPath $stageDir) {{
    Remove-Item -LiteralPath $stageDir -Recurse -Force
}}

Write-Output $destPath
""",
        )
        return self._run_powershell(
            script,
            timeout_seconds=self.MTP_COPY_TIMEOUT_SECONDS,
        )

    @staticmethod
    def _preview_name_candidates(guid: str) -> List[str]:
        return [
            f"{guid}.jpg",
            f"{guid}.jpeg",
            f"{guid}.png",
        ]

    @classmethod
    def _is_preview_name_for_guid(cls, guid: str, name: str) -> bool:
        lowered = (name or "").strip().lower()
        guid_lower = guid.strip().lower()
        return lowered in {candidate.lower() for candidate in cls._preview_name_candidates(guid_lower)}

    @classmethod
    def _choose_preview_name_from_items(cls, guid: str, items: List[dict[str, Any]]) -> str | None:
        # Prefer jpg/jpeg because this is what RC-2 currently emits.
        preferred = [".jpg", ".jpeg", ".png"]
        names = [
            str(item.get("Name") or "").strip()
            for item in items
            if not bool(item.get("IsFolder"))
        ]
        for suffix in preferred:
            for name in names:
                lowered = name.lower()
                if lowered == f"{guid.lower()}{suffix}":
                    return name
        return None

    @staticmethod
    def _choose_preview_folder_name(guid: str, names: List[str]) -> str | None:
        target = guid.strip().lower()
        for name in names:
            if str(name).strip().lower() == target:
                return str(name).strip()
        return None

    @staticmethod
    def _find_case_insensitive_child_dir(parent: str, target_name: str) -> str | None:
        try:
            entries = os.scandir(parent)
        except OSError:
            return None

        target = target_name.strip().lower()
        with entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                if entry.name.strip().lower() == target:
                    return entry.path
        return None

    def _preview_cache_path(self, root: str, guid: str) -> str:
        cache_root = os.path.join(tempfile.gettempdir(), "djirc2kmzsync-previews")
        os.makedirs(cache_root, exist_ok=True)
        root_hash = hashlib.sha1(root.encode("utf-8", errors="ignore")).hexdigest()[:12]
        return os.path.join(cache_root, f"{root_hash}-{guid}")

    def clear_stale_preview_cache(self) -> None:
        root = (self._config.rc2_folder or "").strip()
        if not root:
            return

        cache_base = self._preview_cache_path(root, "")
        cache_root = os.path.dirname(cache_base)
        prefix = os.path.basename(cache_base).lower()

        try:
            names = os.listdir(cache_root)
        except OSError:
            return

        for name in names:
            lowered = name.lower()
            if not lowered.startswith(prefix):
                continue
            full_path = os.path.join(cache_root, name)
            if not os.path.isfile(full_path):
                continue
            try:
                os.remove(full_path)
            except OSError:
                pass

    @staticmethod
    def _cache_temp_copy_path(cache_path: str) -> str:
        return f"{cache_path}.{uuid.uuid4().hex}.tmp"

    @staticmethod
    def _promote_cache_copy(temp_path: str, cache_path: str) -> None:
        cache_base, _ = os.path.splitext(cache_path)
        for suffix in (".jpg", ".jpeg", ".png"):
            candidate = f"{cache_base}{suffix}"
            if candidate == cache_path:
                continue
            if os.path.exists(candidate):
                try:
                    os.remove(candidate)
                except OSError:
                    pass
        os.replace(temp_path, cache_path)

    def _list_mtp_items_cached(self, mtp_path: str) -> Tuple[bool, List[dict[str, Any]] | str]:
        cached = self._mtp_preview_items_cache.get(mtp_path)
        if cached is not None:
            return True, cached

        ok, result = self._list_mtp_items(mtp_path)
        if ok:
            items = result if isinstance(result, list) else []
            self._mtp_preview_items_cache[mtp_path] = items
            return True, items
        return False, result

    @staticmethod
    def _is_usable_preview_file(path: str) -> bool:
        if not path or not os.path.isfile(path):
            return False

        try:
            if os.path.getsize(path) <= 0:
                return False
        except OSError:
            return False

        # If Pillow is available, verify decode to reject truncated cache files.
        if Image is not None:
            try:
                with Image.open(path) as img:
                    img.verify()
            except Exception:
                return False

        return True

    @classmethod
    def _find_usable_cached_preview(cls, cache_base: str, guid: str) -> str | None:
        for candidate in cls._preview_name_candidates(guid):
            ext = os.path.splitext(candidate)[1].lower()
            cache_path = f"{cache_base}{ext}"
            if cls._is_usable_preview_file(cache_path):
                return cache_path
            if os.path.exists(cache_path):
                try:
                    os.remove(cache_path)
                except OSError:
                    pass
        return None

    def get_mission_preview_path(self, guid: str) -> str | None:
        root = (self._config.rc2_folder or "").strip()
        if not root:
            return None

        if self._is_mtp_path(root):
            cache_base = self._preview_cache_path(root, guid)
            cached = self._find_usable_cached_preview(cache_base, guid)
            if cached:
                return cached

            preview_folder = self._mtp_join(root, "map_preview")
            ok_items, item_result = self._list_mtp_items_cached(preview_folder)
            if not ok_items:
                return None

            items = item_result if isinstance(item_result, list) else []
            preview_name = self._choose_preview_name_from_items(guid, items)
            source_folder = preview_folder

            if not preview_name:
                folder_names = [
                    str(item.get("Name") or "").strip()
                    for item in items
                    if bool(item.get("IsFolder"))
                ]
                nested_folder = self._choose_preview_folder_name(guid, folder_names)
                if nested_folder:
                    source_folder = self._mtp_join(preview_folder, nested_folder)
                    ok_nested, nested_result = self._list_mtp_items_cached(source_folder)
                    if ok_nested:
                        nested_items = nested_result if isinstance(nested_result, list) else []
                        preview_name = self._choose_preview_name_from_items(guid, nested_items)

            # Some MTP providers return unreliable IsFolder metadata; probe
            # map_preview|<guid> directly as a fallback for nested previews.
            if not preview_name:
                source_folder = self._mtp_join(preview_folder, guid)
                ok_nested, nested_result = self._list_mtp_items_cached(source_folder)
                if ok_nested:
                    nested_items = nested_result if isinstance(nested_result, list) else []
                    preview_name = self._choose_preview_name_from_items(guid, nested_items)

            if not preview_name:
                return None

            ext = os.path.splitext(preview_name)[1].lower() or ".jpg"
            cache_path = f"{cache_base}{ext}"
            temp_copy = self._cache_temp_copy_path(cache_path)
            ok, _ = self._copy_file_from_mtp_folder(source_folder, preview_name, temp_copy)
            if ok and self._is_usable_preview_file(temp_copy):
                self._promote_cache_copy(temp_copy, cache_path)
                return cache_path
            if os.path.exists(temp_copy):
                try:
                    os.remove(temp_copy)
                except OSError:
                    pass
            return None

        if self._is_adb_path(root):
            cache_base = self._preview_cache_path(root, guid)
            cached = self._find_usable_cached_preview(cache_base, guid)
            if cached:
                return cached

            remote_root = self._adb_remote_root(root)
            preview_dir = self._adb_remote_join(remote_root, "map_preview")
            ok_ls, out_ls = self._run_adb(["shell", "ls", "-1", preview_dir])
            if not ok_ls:
                return None

            names = [line.strip() for line in out_ls.splitlines() if line.strip()]
            preview_name: str | None = None
            source_dir = preview_dir
            for suffix in (".jpg", ".jpeg", ".png"):
                target = f"{guid.lower()}{suffix}"
                for name in names:
                    if name.lower() == target:
                        preview_name = name
                        break
                if preview_name:
                    break

            if not preview_name:
                nested_folder = self._choose_preview_folder_name(guid, names)
                if nested_folder:
                    source_dir = self._adb_remote_join(preview_dir, nested_folder)
                    ok_nested_ls, out_nested_ls = self._run_adb(["shell", "ls", "-1", source_dir])
                    if ok_nested_ls:
                        nested_names = [line.strip() for line in out_nested_ls.splitlines() if line.strip()]
                        for suffix in (".jpg", ".jpeg", ".png"):
                            target = f"{guid.lower()}{suffix}"
                            for name in nested_names:
                                if name.lower() == target:
                                    preview_name = name
                                    break
                            if preview_name:
                                break

            if not preview_name:
                source_dir = self._adb_remote_join(preview_dir, guid)
                ok_nested_ls, out_nested_ls = self._run_adb(["shell", "ls", "-1", source_dir])
                if ok_nested_ls:
                    nested_names = [line.strip() for line in out_nested_ls.splitlines() if line.strip()]
                    for suffix in (".jpg", ".jpeg", ".png"):
                        target = f"{guid.lower()}{suffix}"
                        for name in nested_names:
                            if name.lower() == target:
                                preview_name = name
                                break
                        if preview_name:
                            break

            if not preview_name:
                return None

            ext = os.path.splitext(preview_name)[1].lower() or ".jpg"
            cache_path = f"{cache_base}{ext}"
            remote_preview = self._adb_remote_join(source_dir, preview_name)
            temp_copy = self._cache_temp_copy_path(cache_path)
            ok, _ = self._run_adb(["pull", remote_preview, temp_copy])
            if ok and self._is_usable_preview_file(temp_copy):
                self._promote_cache_copy(temp_copy, cache_path)
                return cache_path
            if os.path.exists(temp_copy):
                try:
                    os.remove(temp_copy)
                except OSError:
                    pass
            return None

        preview_dir = os.path.join(root, "map_preview")
        if not os.path.isdir(preview_dir):
            return None

        for candidate in self._preview_name_candidates(guid):
            preview_path = os.path.join(preview_dir, candidate)
            if os.path.isfile(preview_path):
                return preview_path

        nested_preview_dir = self._find_case_insensitive_child_dir(preview_dir, guid)
        if not nested_preview_dir:
            return None

        for candidate in self._preview_name_candidates(guid):
            preview_path = os.path.join(nested_preview_dir, candidate)
            if os.path.isfile(preview_path):
                return preview_path
        return None

    def _detect_mtp_rc2_folder(self) -> str | None:
        if os.name != "nt":
            return None
        ok, result = self._list_mtp_items(self.DEFAULT_MTP_RC2_ROOT)
        if ok:
            return self.DEFAULT_MTP_RC2_ROOT
        return None

    @classmethod
    def _probe_windows_present_rc2_devices(cls) -> List[str]:
        if os.name != "nt":
            return []

        script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$devices = Get-PnpDevice -PresentOnly |
    Where-Object {
        ($_.FriendlyName -match 'DJI|RC 2|ADB|MTP|Android') -or
        ($_.InstanceId -match 'VID_2CA3')
    } |
    Select-Object FriendlyName, Class, Status

if ($devices) {
    $devices | ConvertTo-Json -Compress
}
"""
        ok, payload = cls._run_powershell_json(script)
        if not ok:
            return []

        records = payload if isinstance(payload, list) else []
        names: List[str] = []
        for record in records:
            friendly_name = str(record.get("FriendlyName") or "").strip()
            device_class = str(record.get("Class") or "").strip()
            status = str(record.get("Status") or "").strip()
            label = friendly_name or device_class or "Unknown device"
            if status:
                label = f"{label} [{status}]"
            names.append(label)
        return names

    @classmethod
    def _run_adb(cls, args: List[str]) -> Tuple[bool, str]:
        adb_executable = cls._resolve_adb_executable()
        if not adb_executable:
            return False, (
                "ADB executable not found. Install Android platform-tools and ensure 'adb' "
                "is on PATH, or set environment variable ADB to the full adb executable path."
            )

        def _exec(cmd_args: List[str]) -> Tuple[int, str]:
            result = subprocess.run(
                [adb_executable, *cmd_args],
                capture_output=True,
                text=True,
                check=False,
            )
            combined = ((result.stdout or "") + (result.stderr or "")).strip()
            return result.returncode, combined

        try:
            code, output = _exec(args)
        except OSError as e:
            return False, f"Failed to run adb: {e}"

        if code == 0:
            return True, output

        lower = output.lower()
        if (
            "device offline" in lower
            or "unauthorized" in lower
            or "no devices/emulators found" in lower
            or "daemon not running" in lower
        ):
            # One recovery attempt helps when ADB daemon just started.
            _exec(["start-server"])
            retry_code, retry_output = _exec(args)
            if retry_code == 0:
                return True, retry_output
            return False, cls._format_adb_error(retry_output or output)

        return False, cls._format_adb_error(output)

    def _set_last_error(self, message: str) -> None:
        self._last_error = message

    def consume_last_error(self) -> str | None:
        message = self._last_error
        self._last_error = None
        return message

    def diagnose_rc2_connection(self) -> Tuple[bool, str, str | None]:
        root = (self._config.rc2_folder or "").strip()

        if root and not self._is_adb_path(root) and not self._is_mtp_path(root) and os.path.isdir(root):
            return True, f"RC-2 folder is reachable on disk: {root}", root

        mtp_root = self._detect_mtp_rc2_folder()
        if mtp_root:
            return True, f"RC-2 is reachable via Explorer-style MTP access. Use {mtp_root} as the RC-2 root.", mtp_root

        adb_ready, adb_message = self.get_adb_status()
        if adb_ready:
            return True, f"{adb_message}. Use {self.DEFAULT_ADB_RC2_ROOT} as the RC-2 root.", self.DEFAULT_ADB_RC2_ROOT

        present_devices = self._probe_windows_present_rc2_devices()
        if present_devices:
            device_list = ", ".join(present_devices[:3])
            return False, (
                "Windows sees RC-2 related device entries but no mounted RC-2 folder is available and ADB is not ready. "
                f"Present devices: {device_list}. If this is a VM, reattach the controller to Windows and enable USB debugging if you want to use ADB."
            ), None

        return False, (
            "RC-2 is not reachable from this Windows session via filesystem, Portable Devices, or ADB. "
            "If Windows is running in a VM, attach the controller to the VM first."
        ), None

    def auto_detect_rc2_folder(self) -> Tuple[bool, str]:
        ok, message, detected_path = self.diagnose_rc2_connection()
        if ok and detected_path and detected_path != self._config.rc2_folder:
            self.set_rc2_folder(detected_path)
            return True, f"{message} RC-2 root updated to {detected_path}."
        return ok, message

    # ------------------------------------------------------------------
    # Properties (forwarded from config for convenience)
    # ------------------------------------------------------------------
    @property
    def rc2_folder(self) -> str:
        return self._config.rc2_folder

    def get_rc2_connection_mode(self) -> str:
        root = (self._config.rc2_folder or "").strip()
        if self._is_mtp_path(root):
            return "MTP"
        if self._is_adb_path(root):
            return "ADB"
        if root and os.path.isdir(root):
            return "Filesystem"
        if root:
            return "Unavailable"
        return "Not Set"

    @property
    def pc_folder(self) -> str:
        return self._config.pc_folder

    def get_rc2_refresh_retry_interval_seconds(self) -> int:
        return self._config.rc2_refresh_retry_interval_seconds

    # ------------------------------------------------------------------
    # Folder update & persistence
    # ------------------------------------------------------------------
    def set_rc2_folder(self, path: str) -> None:
        cleaned = path.strip()
        if self._is_adb_path(cleaned) or self._is_mtp_path(cleaned):
            self._config.rc2_folder = cleaned
        else:
            self._config.rc2_folder = os.path.normpath(cleaned)
        self._mtp_preview_items_cache.clear()
        self._config.save()

    def set_pc_folder(self, path: str) -> None:
        self._config.pc_folder = os.path.normpath(path)
        self._config.save()

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------
    def load_rc2_missions(self) -> List[RC2Mission]:
        """
        Scan the RC-2 root folder. Each sub-directory is a GUID mission slot.
        Returns a sorted list of RC2Mission objects.
        """
        missions: List[RC2Mission] = []
        self._last_error = None
        root = self._config.rc2_folder
        if self._is_mtp_path(root):
            return self._load_rc2_missions_mtp(root)
        if self._is_adb_path(root):
            return self._load_rc2_missions_adb(root)
        if not root or not os.path.isdir(root):
            return missions

        try:
            for entry in sorted(os.scandir(root), key=lambda e: e.name):
                if not entry.is_dir() or not self._is_rc2_slot_name(entry.name):
                    continue
                kmz_files = sorted(
                    [
                        f for f in os.listdir(entry.path)
                        if f.lower().endswith(".kmz")
                    ]
                )
                kmz_name = kmz_files[0] if kmz_files else ""
                last_modified = ""
                if kmz_name:
                    kmz_path = os.path.join(entry.path, kmz_name)
                    last_modified = self._format_local_mtime(kmz_path)
                missions.append(RC2Mission(
                    guid=entry.name,
                    kmz_name=kmz_name,
                    full_folder_path=entry.path,
                    last_modified=last_modified,
                ))
        except OSError as e:
            msg = f"[SyncViewModel] Error scanning RC-2 folder: {e}"
            self._set_last_error(msg)

        return missions

    def _load_rc2_missions_mtp(self, mtp_path: str) -> List[RC2Mission]:
        missions: List[RC2Mission] = []

        # Prefer a single MTP query that enumerates all slots and KMZ metadata
        # in one PowerShell process to reduce startup latency.
        list_mtp_items_bound_self = getattr(self._list_mtp_items, "__self__", None)
        list_mtp_items_func = getattr(self._list_mtp_items, "__func__", None)
        use_bulk_query = list_mtp_items_bound_self is self and list_mtp_items_func is SyncViewModel._list_mtp_items

        ok_bulk = False
        bulk_result: List[dict[str, Any]] | str = []
        if use_bulk_query:
            ok_bulk, bulk_result = self._list_mtp_missions_bulk(mtp_path)
            if ok_bulk:
                rows = bulk_result if isinstance(bulk_result, list) else []
                for row in rows:
                    slot_name = str(row.get("Name") or "").strip()
                    if not slot_name:
                        continue

                    kmz_name = str(row.get("KMZName") or "").strip()
                    last_modified = self._normalize_mtp_modify_date(
                        str(row.get("ModifyDateDetail") or "")
                        or str(row.get("ModifyDate") or "")
                    )
                    missions.append(RC2Mission(
                        guid=slot_name,
                        kmz_name=kmz_name,
                        full_folder_path=self._mtp_join(mtp_path, slot_name),
                        last_modified=last_modified,
                    ))
                return missions

        ok, result = self._list_mtp_items(mtp_path)
        if not ok:
            bulk_error = bulk_result if isinstance(bulk_result, str) and bulk_result.strip() else ""
            suffix = f" | Bulk query failed: {bulk_error}" if bulk_error else ""
            msg = f"[SyncViewModel] Error scanning RC-2 MTP folder: {result}{suffix}"
            self._set_last_error(msg)
            return missions

        items = result if isinstance(result, list) else []
        folders = [
            item for item in items
            if bool(item.get("IsFolder")) and self._is_rc2_slot_name(str(item.get("Name") or ""))
        ]

        for item in sorted(folders, key=lambda value: str(value.get("Name") or "")):
            slot_name = str(item.get("Name") or "").strip()
            if not slot_name:
                continue

            remote_slot = self._mtp_join(mtp_path, slot_name)
            ok_slot, slot_result = self._list_mtp_items(remote_slot)
            if not ok_slot:
                continue

            slot_items = slot_result if isinstance(slot_result, list) else []
            kmz_files = [
                child
                for child in slot_items
                if not bool(child.get("IsFolder")) and str(child.get("Name") or "").strip().lower().endswith(".kmz")
            ]
            kmz_files_sorted = sorted(kmz_files, key=lambda c: str(c.get("Name") or ""))
            kmz_name = str(kmz_files_sorted[0].get("Name") or "").strip() if kmz_files_sorted else ""
            last_modified = (
                self._normalize_mtp_modify_date(
                    str(kmz_files_sorted[0].get("ModifyDateDetail") or "")
                    or str(kmz_files_sorted[0].get("ModifyDate") or "")
                )
                if kmz_files_sorted else ""
            )
            missions.append(RC2Mission(
                guid=slot_name,
                kmz_name=kmz_name,
                full_folder_path=remote_slot,
                last_modified=last_modified,
            ))

        return missions

    def _load_rc2_missions_adb(self, adb_path: str) -> List[RC2Mission]:
        missions: List[RC2Mission] = []
        remote_root = self._adb_remote_root(adb_path)

        ok, out = self._run_adb(["shell", "ls", "-1", remote_root])
        if not ok:
            msg = f"[SyncViewModel] Error scanning RC-2 ADB folder: {out}"
            self._set_last_error(msg)
            return missions

        entries = [line.strip() for line in out.splitlines() if line.strip()]
        for entry in sorted(entries):
            remote_slot = self._adb_remote_join(remote_root, entry)
            ok_slot, slot_out = self._run_adb(["shell", "ls", "-1", remote_slot])
            if not ok_slot:
                continue

            kmz_files = [
                line.strip()
                for line in slot_out.splitlines()
                if line.strip().lower().endswith(".kmz")
            ]
            kmz_name = kmz_files[0] if kmz_files else ""
            missions.append(RC2Mission(
                guid=entry,
                kmz_name=kmz_name,
                full_folder_path=remote_slot,
                last_modified="",
            ))

        return missions

    def load_pc_kmz_files(self) -> List[KMZFile]:
        """
           Recursively scan the PC source folder for .kmz files.
           Returns a sorted list of KMZFile objects with full paths.
        """
        files: List[KMZFile] = []
        self._last_error = None
        root = self._config.pc_folder
        if not root or not os.path.isdir(root):
            return files

        try:
               for entry in sorted(os.walk(root)):
                   folder_path, _, filenames = entry
                   for filename in sorted(filenames):
                       if filename.lower().endswith(".kmz"):
                           full_path = os.path.join(folder_path, filename)
                           # Store relative path for display
                           rel_path = os.path.relpath(full_path, root)
                           files.append(KMZFile(filename=rel_path, full_path=full_path))
        except OSError as e:
            msg = f"[SyncViewModel] Error scanning PC folder: {e}"
            self._set_last_error(msg)

        return files

    def delete_rc2_mission(self, mission: RC2Mission) -> Tuple[bool, str]:
        root = (self._config.rc2_folder or "").strip()

        if self._is_mtp_path(root):
            script = self._mtp_script(
                root,
                f"""
$folderName = {self._ps_single_quote(mission.guid)}
$item = $current.Items() | Where-Object {{ $_.Name -eq $folderName -and $_.IsFolder }} | Select-Object -First 1
if (-not $item) {{
    throw "Mission folder not found: $folderName"
}}

$item.InvokeVerb('delete')

$deadline = (Get-Date).AddSeconds(15)
do {{
    $remaining = $current.Items() | Where-Object {{ $_.Name -eq $folderName -and $_.IsFolder }} | Select-Object -First 1
    if (-not $remaining) {{
        break
    }}
    [System.Threading.Thread]::Sleep(200)
}} while ((Get-Date) -lt $deadline)

$remaining = $current.Items() | Where-Object {{ $_.Name -eq $folderName -and $_.IsFolder }} | Select-Object -First 1
if ($remaining) {{
    throw "Mission folder delete did not complete: $folderName"
}}

Write-Output $folderName
""",
            )
            ok, out = self._run_powershell(script, timeout_seconds=self.MTP_COPY_TIMEOUT_SECONDS)
            if not ok:
                return False, f"MTP delete failed:\n{out}"
            return True, f"Deleted mission {mission.guid}"

        if self._is_adb_path(root):
            remote_root = self._adb_remote_root(root)
            remote_slot = self._adb_remote_join(remote_root, mission.guid)
            ok, out = self._run_adb(["shell", "rm", "-rf", remote_slot])
            if not ok:
                return False, f"ADB delete failed:\n{out}"
            return True, f"Deleted mission {mission.guid}"

        if not os.path.isdir(mission.full_folder_path):
            return False, f"Mission folder not found:\n{mission.full_folder_path}"

        try:
            shutil.rmtree(mission.full_folder_path)
        except OSError as e:
            return False, f"File operation failed:\n{e}"

        return True, f"Deleted mission {mission.guid}"

    def delete_pc_kmz_file(self, kmz_file: KMZFile) -> Tuple[bool, str]:
        pc_root = (self._config.pc_folder or "").strip()
        if not pc_root or not os.path.isdir(pc_root):
            return False, f"PC KMZ folder not found:\n{pc_root}"

        file_path = kmz_file.full_path
        if not os.path.isfile(file_path):
            return False, f"KMZ file not found:\n{file_path}"

        try:
            os.remove(file_path)
        except OSError as e:
            return False, f"File operation failed:\n{e}"

        return True, f"Deleted KMZ file {kmz_file.filename}"

    def get_adb_status(self) -> Tuple[bool, str]:
        """
        Return (ready, message) for current ADB device state.
        """
        ok, out = self._run_adb(["devices"])
        if not ok:
            return False, out

        rows = []
        for line in out.splitlines():
            text = line.strip()
            if not text or text.lower().startswith("list of devices attached"):
                continue
            parts = text.split()
            if len(parts) >= 2:
                rows.append((parts[0], parts[1]))

        if not rows:
            return False, "No ADB devices detected. Connect the RC-2 and verify USB debugging is enabled."

        for serial, state in rows:
            if state == "device":
                return True, f"ADB device connected: {serial}"

        first_serial, first_state = rows[0]
        if first_state == "offline":
            return False, (
                "ADB device is offline. Reconnect RC-2 USB, unlock/confirm USB debugging, "
                "and retry."
            )
        if first_state == "unauthorized":
            return False, "ADB device is unauthorized. Accept USB debugging on RC-2 and retry."

        return False, f"ADB device not ready: {first_serial} ({first_state})."

    # ------------------------------------------------------------------
    # Core sync operation
    # ------------------------------------------------------------------
    def execute_copy(
        self,
        mission: RC2Mission,
        kmz_file: KMZFile,
    ) -> Tuple[bool, str]:
        """
        Copy kmz_file into the mission's GUID slot, preserving the existing
        destination filename (or defaulting to <GUID>.kmz for empty slots).

        Returns (success: bool, message: str).
        """
        target_mission = mission
        dest_filename = mission.kmz_name if mission.kmz_name else f"{mission.guid}.kmz"

        if self._is_mtp_path(self._config.rc2_folder):
            ok, msg = self._execute_copy_mtp(target_mission, kmz_file, dest_filename)
            if ok:
                self._record_copy_mapping(kmz_file, target_mission, dest_filename)
            return ok, msg

        if self._is_adb_path(self._config.rc2_folder):
            ok, msg = self._execute_copy_adb(target_mission, kmz_file, dest_filename)
            if ok:
                self._record_copy_mapping(kmz_file, target_mission, dest_filename)
            return ok, msg

        dest_path = os.path.join(target_mission.full_folder_path, dest_filename)

        if not os.path.isfile(kmz_file.full_path):
            return False, f"Source file not found:\n{kmz_file.full_path}"

        if not os.path.isdir(target_mission.full_folder_path):
            return False, f"Destination slot folder not found:\n{target_mission.full_folder_path}"

        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            shutil.copy2(kmz_file.full_path, dest_path)
        except OSError as e:
            return False, f"File operation failed:\n{e}"

        self._record_copy_mapping(kmz_file, target_mission, dest_filename)

        return True, (
            f"Copied '{kmz_file.filename}'\n"
            f"  → mission  : {target_mission.guid}\n"
            f"  → saved as : {dest_filename}"
        )

    def execute_copy_from_mission(
        self,
        mission: RC2Mission,
        target_kmz_file: KMZFile | None = None,
    ) -> Tuple[bool, str]:
        """
        Copy the selected RC-2 mission KMZ back to the PC folder and save it
        using the selected target filename.

        Returns (success: bool, message: str).
        """
        pc_root = (self._config.pc_folder or "").strip()
        if not pc_root or not os.path.isdir(pc_root):
            return False, f"PC KMZ folder not found:\n{pc_root}"

        source_filename = (mission.kmz_name or "").strip()
        if not source_filename:
            ok_list, listed = self._list_slot_files(mission)
            if not ok_list:
                return False, f"Failed to list mission files:\n{listed}"
            names = listed if isinstance(listed, list) else []
            kmz_candidates = sorted([name for name in names if name.lower().endswith(".kmz")])
            if not kmz_candidates:
                return False, "No KMZ found in selected RC-2 mission."
            source_filename = kmz_candidates[0]

        target_filename = target_kmz_file.filename if target_kmz_file is not None else f"{mission.guid}.kmz"
        dest_path = os.path.join(pc_root, target_filename)
        root = (self._config.rc2_folder or "").strip()

        if self._is_mtp_path(root):
            fd, temp_path = tempfile.mkstemp(prefix="djirc2kmzsync-copyback-", suffix=".kmz")
            os.close(fd)
            try:
                ok, out = self._copy_file_from_mtp_folder(mission.full_folder_path, source_filename, temp_path)
                if not ok:
                    return False, f"MTP copy back failed:\n{out}"
                if os.path.exists(dest_path):
                    os.remove(dest_path)
                shutil.copy2(temp_path, dest_path)
            except OSError as e:
                return False, f"File operation failed:\n{e}"
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

            self._record_copy_mapping(
                KMZFile(filename=target_filename, full_path=dest_path),
                mission,
                source_filename,
            )

            return True, (
                f"Copied mission '{source_filename}'\n"
                f"  → target file: {target_filename}\n"
                f"  → location   : {dest_path}"
            )

        if self._is_adb_path(root):
            fd, temp_path = tempfile.mkstemp(prefix="djirc2kmzsync-copyback-", suffix=".kmz")
            os.close(fd)
            remote_file = self._adb_remote_join(mission.full_folder_path, source_filename)
            try:
                ok, out = self._run_adb(["pull", remote_file, temp_path])
                if not ok:
                    return False, f"ADB copy back failed:\n{out}"
                if os.path.exists(dest_path):
                    os.remove(dest_path)
                shutil.copy2(temp_path, dest_path)
            except OSError as e:
                return False, f"File operation failed:\n{e}"
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

            self._record_copy_mapping(
                KMZFile(filename=target_filename, full_path=dest_path),
                mission,
                source_filename,
            )

            return True, (
                f"Copied mission '{source_filename}'\n"
                f"  → target file: {target_filename}\n"
                f"  → location   : {dest_path}"
            )

        source_path = os.path.join(mission.full_folder_path, source_filename)
        if not os.path.isfile(source_path):
            return False, f"Mission source file not found:\n{source_path}"

        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            shutil.copy2(source_path, dest_path)
        except OSError as e:
            return False, f"File operation failed:\n{e}"

        self._record_copy_mapping(
            KMZFile(filename=target_filename, full_path=dest_path),
            mission,
            source_filename,
        )

        return True, (
            f"Copied mission '{source_filename}'\n"
            f"  → target file: {target_filename}\n"
            f"  → location   : {dest_path}"
        )

    def _prepare_new_mission_target(self) -> Tuple[bool, RC2Mission | str]:
        new_guid = str(uuid.uuid4()).upper()
        root = (self._config.rc2_folder or "").strip()

        if self._is_mtp_path(root):
            ok, out = self._create_mtp_slot_folder(root, new_guid)
            if not ok:
                return False, f"Failed to create MTP slot folder:\n{out}"
            full_folder = self._mtp_join(root, new_guid)
            return True, RC2Mission(guid=new_guid, kmz_name="", full_folder_path=full_folder)

        if self._is_adb_path(root):
            remote_root = self._adb_remote_root(root)
            remote_slot = self._adb_remote_join(remote_root, new_guid)
            ok, out = self._run_adb(["shell", "mkdir", "-p", remote_slot])
            if not ok:
                return False, f"Failed to create ADB slot folder:\n{out}"
            return True, RC2Mission(guid=new_guid, kmz_name="", full_folder_path=remote_slot)

        if not root or not os.path.isdir(root):
            return False, f"RC-2 root folder not found:\n{root}"

        full_folder = os.path.join(root, new_guid)
        try:
            os.makedirs(full_folder, exist_ok=False)
        except OSError as e:
            return False, f"Failed to create destination slot folder:\n{e}"

        return True, RC2Mission(guid=new_guid, kmz_name="", full_folder_path=full_folder)

    def _execute_copy_mtp(
        self,
        mission: RC2Mission,
        kmz_file: KMZFile,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        if not os.path.isfile(kmz_file.full_path):
            return False, f"Source file not found:\n{kmz_file.full_path}"

        source_to_copy = kmz_file.full_path
        temp_dir: str | None = None
        if os.path.basename(kmz_file.full_path) != dest_filename:
            temp_dir = tempfile.mkdtemp(prefix="djirc2kmzsync-")
            source_to_copy = os.path.join(temp_dir, dest_filename)
            shutil.copy2(kmz_file.full_path, source_to_copy)

        try:
            ok, out = self._copy_file_to_mtp_folder(mission.full_folder_path, source_to_copy)
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

        if not ok:
            return False, f"MTP copy failed:\n{out}"

        return True, (
            f"Copied '{kmz_file.filename}'\n"
            f"  → mission  : {mission.guid}\n"
            f"  → saved as : {dest_filename}"
        )

    def _execute_copy_adb(
        self,
        mission: RC2Mission,
        kmz_file: KMZFile,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        if not os.path.isfile(kmz_file.full_path):
            return False, f"Source file not found:\n{kmz_file.full_path}"

        remote_root = self._adb_remote_root(self._config.rc2_folder)
        remote_slot = self._adb_remote_join(remote_root, mission.guid)
        remote_dest = self._adb_remote_join(remote_slot, dest_filename)

        ok, out = self._run_adb(["push", kmz_file.full_path, remote_dest])
        if not ok:
            return False, f"ADB push failed:\n{out}"

        return True, (
            f"Copied '{kmz_file.filename}'\n"
            f"  → mission  : {mission.guid}\n"
            f"  → saved as : {dest_filename}"
        )

    def _list_slot_files(self, mission: RC2Mission) -> Tuple[bool, List[str] | str]:
        root = (self._config.rc2_folder or "").strip()

        if self._is_mtp_path(root):
            ok, result = self._list_mtp_items(mission.full_folder_path)
            if not ok:
                return False, str(result)
            items = result if isinstance(result, list) else []
            names = [
                str(item.get("Name") or "").strip()
                for item in items
                if str(item.get("Name") or "").strip()
            ]
            return True, sorted(names)

        if self._is_adb_path(root):
            ok, out = self._run_adb(["shell", "ls", "-1", mission.full_folder_path])
            if not ok:
                return False, out
            names = [line.strip() for line in out.splitlines() if line.strip()]
            return True, sorted(names)

        if not os.path.isdir(mission.full_folder_path):
            return False, f"Slot folder not found: {mission.full_folder_path}"

        try:
            names = [entry.name for entry in os.scandir(mission.full_folder_path)]
        except OSError as e:
            return False, str(e)
        return True, sorted(names)

    def _read_slot_file_bytes(self, mission: RC2Mission, filename: str) -> Tuple[bool, bytes | str]:
        root = (self._config.rc2_folder or "").strip()

        if self._is_mtp_path(root):
            fd, temp_path = tempfile.mkstemp(prefix="djirc2kmzsync-inspect-", suffix=".kmz")
            os.close(fd)
            try:
                ok, out = self._copy_file_from_mtp_folder(mission.full_folder_path, filename, temp_path)
                if not ok:
                    return False, out
                with open(temp_path, "rb") as fh:
                    return True, fh.read()
            except OSError as e:
                return False, str(e)
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

        if self._is_adb_path(root):
            fd, temp_path = tempfile.mkstemp(prefix="djirc2kmzsync-inspect-", suffix=".kmz")
            os.close(fd)
            remote_file = self._adb_remote_join(mission.full_folder_path, filename)
            try:
                ok, out = self._run_adb(["pull", remote_file, temp_path])
                if not ok:
                    return False, out
                with open(temp_path, "rb") as fh:
                    return True, fh.read()
            except OSError as e:
                return False, str(e)
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

        local_file = os.path.join(mission.full_folder_path, filename)
        try:
            with open(local_file, "rb") as fh:
                return True, fh.read()
        except OSError as e:
            return False, str(e)

    @classmethod
    def _mtp_parent_path(cls, mtp_path: str, levels: int = 1) -> str | None:
        if not cls._is_mtp_path(mtp_path):
            return None
        segments = cls._mtp_segments(mtp_path)
        if len(segments) <= levels:
            return None
        parent_segments = segments[:-levels]
        return "mtp:" + "|".join(parent_segments)

    def _list_folder_items_with_type(self, folder_path: str) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        root = (self._config.rc2_folder or "").strip()

        if self._is_mtp_path(root):
            ok, result = self._list_mtp_items(folder_path)
            if not ok:
                return False, str(result)
            items = result if isinstance(result, list) else []
            output: List[Tuple[str, bool, str]] = []
            for item in items:
                name = str(item.get("Name") or "").strip()
                if not name:
                    continue
                modified = self._normalize_mtp_modify_date(
                    str(item.get("ModifyDateDetail") or "")
                    or str(item.get("ModifyDate") or "")
                )
                output.append((name, bool(item.get("IsFolder")), modified))
            return True, output

        if self._is_adb_path(root):
            ok, out = self._run_adb(["shell", "ls", "-1p", folder_path])
            if not ok:
                return False, out
            output = []
            for line in out.splitlines():
                raw = line.strip()
                if not raw:
                    continue
                is_folder = raw.endswith("/")
                name = raw[:-1] if is_folder else raw
                output.append((name, is_folder, ""))
            return True, output

        if not os.path.isdir(folder_path):
            return False, f"Folder not found: {folder_path}"

        try:
            output = []
            for entry in os.scandir(folder_path):
                modified = ""
                if not entry.is_dir():
                    try:
                        modified = self._format_display_datetime(datetime.fromtimestamp(entry.stat().st_mtime))
                    except OSError:
                        modified = ""
                output.append((entry.name, entry.is_dir(), modified))
        except OSError as e:
            return False, str(e)
        return True, output

    def _read_file_bytes_from_folder(self, folder_path: str, filename: str) -> Tuple[bool, bytes | str]:
        root = (self._config.rc2_folder or "").strip()

        if self._is_mtp_path(root):
            fd, temp_path = tempfile.mkstemp(prefix="djirc2kmzsync-meta-", suffix=".tmp")
            os.close(fd)
            try:
                ok, out = self._copy_file_from_mtp_folder(folder_path, filename, temp_path)
                if not ok:
                    return False, out
                with open(temp_path, "rb") as fh:
                    return True, fh.read()
            except OSError as e:
                return False, str(e)
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

        if self._is_adb_path(root):
            fd, temp_path = tempfile.mkstemp(prefix="djirc2kmzsync-meta-", suffix=".tmp")
            os.close(fd)
            remote_file = self._adb_remote_join(folder_path, filename)
            try:
                ok, out = self._run_adb(["pull", remote_file, temp_path])
                if not ok:
                    return False, out
                with open(temp_path, "rb") as fh:
                    return True, fh.read()
            except OSError as e:
                return False, str(e)
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

        local_file = os.path.join(folder_path, filename)
        try:
            with open(local_file, "rb") as fh:
                return True, fh.read()
        except OSError as e:
            return False, str(e)

    def _inspect_metadata_history_candidates(self, mission: RC2Mission, kmz_name: str) -> List[str]:
        lines: List[str] = []
        root = (self._config.rc2_folder or "").strip()
        candidates: List[str] = []

        if self._is_mtp_path(root):
            candidates.append(root)
            parent_files = self._mtp_parent_path(root, levels=1)
            parent_app = self._mtp_parent_path(root, levels=2)
            if parent_files:
                candidates.append(parent_files)
                ok, items = self._list_folder_items_with_type(parent_files)
                if ok and isinstance(items, list):
                    for name, is_folder, _ in items:
                        lowered = name.lower()
                        if is_folder and any(token in lowered for token in ("history", "record", "mission", "meta", "index", "cache")):
                            candidates.append(self._mtp_join(parent_files, name))
            if parent_app:
                candidates.append(parent_app)
        elif self._is_adb_path(root):
            remote_root = self._adb_remote_root(root)
            candidates.append(remote_root)
            if "/" in remote_root:
                parent_files = remote_root.rsplit("/", 1)[0]
                candidates.append(parent_files)
                if "/" in parent_files:
                    candidates.append(parent_files.rsplit("/", 1)[0])
        else:
            candidates.append(root)
            if os.path.isdir(root):
                parent_files = os.path.dirname(root)
                candidates.append(parent_files)
                for name in os.listdir(parent_files) if os.path.isdir(parent_files) else []:
                    lowered = name.lower()
                    full = os.path.join(parent_files, name)
                    if os.path.isdir(full) and any(token in lowered for token in ("history", "record", "mission", "meta", "index", "cache")):
                        candidates.append(full)

        seen: set[str] = set()
        unique_candidates: List[str] = []
        for path in candidates:
            if not path or path in seen:
                continue
            seen.add(path)
            unique_candidates.append(path)

        lines.append("Metadata/history probe:")
        lines.append(f"Candidate folders: {len(unique_candidates)}")

        targets = [mission.guid.lower(), kmz_name.lower()]
        checked_files = 0
        hits: List[str] = []
        meta_exts = (".json", ".txt", ".xml", ".db", ".sqlite")

        for folder in unique_candidates[:8]:
            ok, listed = self._list_folder_items_with_type(folder)
            if not ok:
                continue
            items = listed if isinstance(listed, list) else []
            files = [(name, modified) for name, is_folder, modified in items if not is_folder]
            interesting = [
                (name, modified) for name, modified in files
                if name.lower().endswith(meta_exts)
                or any(token in name.lower() for token in ("history", "mission", "meta", "index", "title", "record"))
            ]

            if interesting:
                preview = [
                    f"{name} [{modified or 'Unknown'}]"
                    for name, modified in interesting[:8]
                ]
                lines.append(f"- {folder}: {', '.join(preview)}{' ...' if len(interesting) > 8 else ''}")

            for filename, modified in interesting[:10]:
                checked_files += 1
                ok_bytes, payload = self._read_file_bytes_from_folder(folder, filename)
                if not ok_bytes or not isinstance(payload, bytes):
                    continue

                if filename.lower().endswith((".db", ".sqlite")):
                    # Binary DBs are reported as candidates; we don't parse them inline.
                    continue

                text = ""
                for encoding in ("utf-8", "utf-16", "latin-1"):
                    try:
                        text = payload.decode(encoding, errors="strict")
                        break
                    except UnicodeDecodeError:
                        continue

                if not text:
                    continue

                lowered = text.lower()
                if any(target in lowered for target in targets if target):
                    hits.append(f"{folder} | {filename} [{modified or 'Unknown'}]")

        lines.append(f"Metadata files checked: {checked_files}")
        if hits:
            dedup_hits = []
            seen_hits = set()
            for hit in hits:
                if hit in seen_hits:
                    continue
                seen_hits.add(hit)
                dedup_hits.append(hit)
            lines.append("GUID/KMZ references found in:")
            for hit in dedup_hits[:12]:
                lines.append(f"  * {hit}")
        else:
            lines.append("No GUID/KMZ references found in inspected text metadata files.")
            lines.append("Likely source is a binary DJI app database/index not directly readable via this path.")

        return lines

    def _inspect_binary_metadata_candidates(self, mission: RC2Mission, kmz_name: str) -> List[str]:
        lines: List[str] = []
        root = (self._config.rc2_folder or "").strip()
        candidates: List[str] = []

        def add_candidate(path: str | None) -> None:
            cleaned = (path or "").strip()
            if cleaned:
                candidates.append(cleaned)

        if self._is_mtp_path(root):
            add_candidate(root)
            add_candidate(self._mtp_parent_path(root, levels=1))
            add_candidate(self._mtp_parent_path(root, levels=2))
        elif self._is_adb_path(root):
            remote_root = self._adb_remote_root(root)
            add_candidate(remote_root)
            if "/" in remote_root:
                parent_files = remote_root.rsplit("/", 1)[0]
                add_candidate(parent_files)
                if "/" in parent_files:
                    add_candidate(parent_files.rsplit("/", 1)[0])
        else:
            add_candidate(root)
            if os.path.isdir(root):
                add_candidate(os.path.dirname(root))

        seen: set[str] = set()
        unique_candidates: List[str] = []
        for path in candidates:
            if not path or path in seen:
                continue
            seen.add(path)
            unique_candidates.append(path)

        lines.append("Binary metadata/index search:")
        lines.append(f"Candidate folders: {len(unique_candidates)}")

        meta_exts = (".db", ".sqlite", ".sqlite3", ".db3", ".dat", ".idx", ".bin")
        name_tokens = ("database", "index", "metadata", "mission", "history", "record", "cache", "dji")
        hits: List[str] = []

        for folder in unique_candidates[:8]:
            ok, listed = self._list_folder_items_with_type(folder)
            if not ok:
                continue

            items = listed if isinstance(listed, list) else []
            matches = [
                (name, modified) for name, is_folder, modified in items
                if not is_folder and (
                    name.lower().endswith(meta_exts)
                    or any(token in name.lower() for token in name_tokens)
                )
            ]

            if matches:
                preview = [
                    f"{name} [{modified or 'Unknown'}]"
                    for name, modified in matches[:8]
                ]
                lines.append(f"- {folder}: {', '.join(preview)}{' ...' if len(matches) > 8 else ''}")

            for name, modified in matches[:10]:
                hits.append(f"{folder} | {name} [{modified or 'Unknown'}]")

        if hits:
            lines.append(f"Best candidate: {hits[0]}")
            lines.append("Potential binary metadata/index files:")
            for hit in hits[:12]:
                lines.append(f"  * {hit}")
        else:
            lines.append("No obvious binary database/index filenames found in candidate folders.")

        return lines

    @staticmethod
    def _extract_name_like_fields_from_text(text: str) -> List[str]:
        patterns = [
            r"<name>\s*([^<]{1,200})\s*</name>",
            r"<[^>]*missionName[^>]*>\s*([^<]{1,200})\s*</[^>]+>",
            r"<[^>]*title[^>]*>\s*([^<]{1,200})\s*</[^>]+>",
        ]
        found: List[str] = []
        for pattern in patterns:
            for match in re.findall(pattern, text, flags=re.IGNORECASE):
                value = (match or "").strip()
                if value and value not in found:
                    found.append(value)
                if len(found) >= 20:
                    return found
        return found

    def inspect_mission_storage(self, mission: RC2Mission, deep: bool = False) -> Tuple[bool, str]:
        lines: List[str] = []
        lines.append(f"Inspecting mission: {mission.guid}")
        lines.append(f"Mission path: {mission.full_folder_path}")

        ok_list, listing = self._list_slot_files(mission)
        if not ok_list:
            return False, f"Failed to list slot files: {listing}"

        slot_files = listing if isinstance(listing, list) else []
        lines.append(f"Mission files ({len(slot_files)}): {', '.join(slot_files) if slot_files else '[none]'}")

        kmz_name = mission.kmz_name
        if not kmz_name:
            for name in slot_files:
                if name.lower().endswith(".kmz"):
                    kmz_name = name
                    break

        if not kmz_name:
            lines.append("No KMZ file found in selected mission.")
            lines.append("RC-2 display name is likely managed outside this slot by DJI app metadata.")
            return True, "\n".join(lines)

        lines.append(f"Inspecting KMZ: {kmz_name}")
        ok_bytes, payload = self._read_slot_file_bytes(mission, kmz_name)
        if not ok_bytes:
            return False, f"Failed to read KMZ from slot: {payload}"

        kmz_bytes = payload if isinstance(payload, bytes) else b""
        try:
            with zipfile.ZipFile(io.BytesIO(kmz_bytes)) as archive:
                entries = archive.namelist()
                lines.append(f"KMZ entries ({len(entries)}): {', '.join(entries[:12])}{' ...' if len(entries) > 12 else ''}")

                xml_entries = [
                    name for name in entries
                    if name.lower().endswith((".kml", ".wpml", ".xml"))
                ]

                discovered_names: List[str] = []
                for name in xml_entries[:8]:
                    raw = archive.read(name)
                    text = ""
                    for encoding in ("utf-8", "utf-16", "latin-1"):
                        try:
                            text = raw.decode(encoding)
                            break
                        except UnicodeDecodeError:
                            continue
                    if not text:
                        continue
                    for value in self._extract_name_like_fields_from_text(text):
                        if value not in discovered_names:
                            discovered_names.append(value)
                        if len(discovered_names) >= 20:
                            break

                if discovered_names:
                    preview = ", ".join(discovered_names[:10])
                    suffix = " ..." if len(discovered_names) > 10 else ""
                    lines.append(f"Name-like fields found in KMZ XML: {preview}{suffix}")
                else:
                    lines.append("No obvious name/title fields found inside KMZ XML files.")
                    lines.append("RC-2 edited mission display names are likely stored in DJI app index/database metadata.")

                if not deep:
                    lines.append(
                        "Quick inspect summary: display-name metadata is likely external to the KMZ. "
                        "Use Deep Inspect to search DJI metadata/index files."
                    )
                    return True, "\n".join(lines)

                lines.extend(self._inspect_metadata_history_candidates(mission, kmz_name))
                lines.extend(self._inspect_binary_metadata_candidates(mission, kmz_name))
                lines.append(
                    "Deep inspect summary: if the edited display name is still missing, it is likely "
                    "stored in a DJI binary database/index file outside the slot and outside the KMZ."
                )
        except zipfile.BadZipFile:
            return False, "Selected mission KMZ is not a valid zip archive."

        return True, "\n".join(lines)

    # ------------------------------------------------------------------
    # Confirmation text helper (for UI dialogs)
    # ------------------------------------------------------------------
    def confirm_copy_message(self, mission: RC2Mission, kmz_file: KMZFile) -> str:
        dest_filename = mission.kmz_name if mission.kmz_name else f"{mission.guid}.kmz"
        return (
            f"Overwrite mission:\n"
            f"  {mission.guid}\n\n"
            f"With source file:\n"
            f"  {kmz_file.filename}\n\n"
            f"Destination filename will be:\n"
            f"  {dest_filename}"
        )



