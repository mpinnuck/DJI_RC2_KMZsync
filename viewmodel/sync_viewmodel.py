import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime
from typing import Any, List, Tuple

try:
    from PIL import Image
except ImportError:
    Image = None

from backends.backend_factory import BackendFactory, UnsupportedBackendError
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
    MTP_LIST_TIMEOUT_SECONDS = 30
    MTP_COPY_TIMEOUT_SECONDS = 10
    MTP_WRITE_TIMEOUT_SECONDS = 10
    MTP_VERIFY_TIMEOUT_SECONDS = 5
    MTP_OVERWRITE_TIMEOUT_SECONDS = 15
    MTP_COPY_SETTLE_SECONDS = 0.3
    MTP_SIZE_TOLERANCE_PERCENT = 10.0
    MTP_SIZE_TOLERANCE_BYTES = 4096
    _RC2_NON_MISSION_FOLDERS = {"capability", "map_preview"}
    COPY_MAP_FILE = "kmz_copy_map.json"

    def __init__(self, config: ConfigManager, copy_map_path: str | None = None):
        self._config = config
        self._last_error: str | None = None
        self._mtp_preview_items_cache: dict[str, List[dict[str, Any]]] = {}
        self._preview_timestamp_cache: dict[str, str] = {}  # guid → device ModifyDate string
        self._preview_timestamps_loaded: bool = False
        self._mtp_operation_lock = threading.Lock()
        self._rc_backend = self._create_rc_backend(config.rc2_folder)
        self._pc_backend = BackendFactory.create_pc(config)
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

    def _create_rc_backend(self, path: str):
        """
        Build the RC backend for *path*, falling back to a disconnected ADB
        backend when the path uses no recognised protocol prefix.
        """
        try:
            return BackendFactory.create_rc(path, self._config)
        except UnsupportedBackendError:
            # Path is not a recognised device protocol (e.g. a bare filesystem
            # path entered before the adb:/mtp: prefix was configured).  Keep a
            # disconnected ADB backend so connectivity checks return False cleanly.
            return BackendFactory.create_rc("", self._config)

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
    def _format_display_datetime(dt: datetime) -> str:
        return dt.strftime("%d/%m/%Y %H:%M:%S")

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

    def _run_mtp_powershell(
        self,
        script: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, str]:
        # MTP COM access can hang when multiple PowerShell sessions query/copy
        # concurrently; serialize all MTP operations through one lock.
        with self._mtp_operation_lock:
            return self._run_powershell(script, timeout_seconds=timeout_seconds)

    def _run_mtp_powershell_json(
        self,
        script: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, List[dict[str, Any]] | str]:
        with self._mtp_operation_lock:
            return self._run_powershell_json(script, timeout_seconds=timeout_seconds)

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

    def _list_mtp_items(
        self,
        mtp_path: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, List[dict[str, Any]] | str]:
        # Delegated to WindowsMTPBackend. Note: the backend omits the 'Size'
        # field (DJI MTP always reports size=0 for all files anyway).
        return self._rc_backend._list_mtp_items(  # type: ignore[attr-defined]
            mtp_path, timeout_seconds=timeout_seconds or self.MTP_LIST_TIMEOUT_SECONDS
        )

    def _list_mtp_missions_bulk(self, mtp_path: str) -> Tuple[bool, List[dict[str, Any]] | str]:
        return self._rc_backend._list_missions_bulk(mtp_path)  # type: ignore[attr-defined]

    def _copy_file_to_mtp_folder(self, mtp_folder: str, local_source_path: str) -> Tuple[bool, str]:
        dest_filename = os.path.basename(local_source_path)
        return self._rc_backend.copy_file_to_device(mtp_folder, local_source_path, dest_filename)

    def _create_mtp_slot_folder(self, mtp_root: str, guid: str) -> Tuple[bool, str]:
        return self._rc_backend.create_slot_folder(mtp_root, guid)

    def _copy_file_from_mtp_folder(
        self,
        mtp_folder: str,
        filename: str,
        local_dest_path: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, str]:
        return self._rc_backend.copy_file_from_device(
            mtp_folder, filename, local_dest_path,
            timeout_seconds=timeout_seconds,
        )

    def _delete_file_from_mtp_folder(self, mtp_folder: str, filename: str) -> Tuple[bool, str]:
        script = self._mtp_script(
            mtp_folder,
            f"""
$filename = {self._ps_single_quote(filename)}
$item = $current.Items() | Where-Object {{ $_.Name -eq $filename -and -not $_.IsFolder }} | Select-Object -First 1
if (-not $item) {{
    Write-Output "NOT_FOUND"
    return
}}

try {{
    $item.InvokeVerb('delete')
}} catch {{
    throw "Failed to delete MTP file: $filename"
}}

$deadline = (Get-Date).AddSeconds(8)
do {{
    $remaining = $current.Items() | Where-Object {{ $_.Name -eq $filename -and -not $_.IsFolder }} | Select-Object -First 1
    if (-not $remaining) {{
        break
    }}
    [System.Threading.Thread]::Sleep(200)
}} while ((Get-Date) -lt $deadline)

$remaining = $current.Items() | Where-Object {{ $_.Name -eq $filename -and -not $_.IsFolder }} | Select-Object -First 1
if ($remaining) {{
    throw "MTP delete did not complete for $filename"
}}

Write-Output "DELETED"
""",
        )
        return self._run_mtp_powershell(
            script,
            timeout_seconds=self.MTP_COPY_TIMEOUT_SECONDS,
        )

    def _delete_file_from_mtp_folder_fast(self, mtp_folder: str, filename: str) -> Tuple[bool, str]:
        script = self._mtp_script(
            mtp_folder,
            f"""
$filename = {self._ps_single_quote(filename)}
$item = $current.Items() | Where-Object {{ $_.Name -eq $filename -and -not $_.IsFolder }} | Select-Object -First 1
if (-not $item) {{
    Write-Output "NOT_FOUND"
    return
}}

try {{
    $item.InvokeVerb('delete')
}} catch {{
    throw "Failed to delete MTP file: $filename"
}}

Write-Output "DELETE_REQUESTED"
""",
        )
        return self._run_mtp_powershell(
            script,
            timeout_seconds=10,
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

    def _preview_timestamps_path(self, root: str) -> str:
        cache_root = os.path.join(tempfile.gettempdir(), "djirc2kmzsync-previews")
        root_hash = hashlib.sha1(root.encode("utf-8", errors="ignore")).hexdigest()[:12]
        return os.path.join(cache_root, f"{root_hash}-timestamps.json")

    def _load_preview_timestamps(self, root: str) -> None:
        """Load the persisted timestamp map from disk once per session."""
        if self._preview_timestamps_loaded:
            return
        self._preview_timestamps_loaded = True
        path = self._preview_timestamps_path(root)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._preview_timestamp_cache = {str(k): str(v) for k, v in data.items()}
        except (OSError, json.JSONDecodeError):
            pass

    def _save_preview_timestamps(self, root: str) -> None:
        """Persist the timestamp map atomically so it survives app restarts."""
        path = self._preview_timestamps_path(root)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._preview_timestamp_cache, fh)
            os.replace(tmp, path)
        except OSError:
            pass

    def clear_mtp_listings_cache(self) -> None:
        """Clear the in-memory MTP folder-listing cache."""
        self._mtp_preview_items_cache.clear()

    def _list_mtp_preview_bulk(
        self,
        preview_folder: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, dict[str, dict[str, str]]]:
        """Single PowerShell process that lists map_preview/ and all its
        one-level-deep subfolders, returning
        {GUID_UPPER: {preview_name, parent_name, device_ts}}.
        Always performs a fresh MTP call — callers should invoke this
        once and reuse the result across missions."""
        script = self._mtp_script(
            preview_folder,
            r"""
# Discover the 'date modified' column index once for the top folder.
$modifiedIndex = $null
for ($i = 0; $i -lt 320; $i++) {
    $label = $current.GetDetailsOf($null, $i)
    if (-not $label) { continue }
    $normalized = ($label -as [string]).Trim().ToLowerInvariant()
    if ($normalized -in @('date modified','modified','modification date','date de modification')) {
        $modifiedIndex = $i
        break
    }
}

$result = [System.Collections.Generic.List[PSObject]]::new()
$topItems = @($current.Items())
foreach ($item in $topItems) {
    if (-not $item.IsFolder) {
        $ts = if ($modifiedIndex -ne $null) { [string]$current.GetDetailsOf($item, $modifiedIndex) } else { '' }
        $result.Add([PSCustomObject]@{
            ParentName      = ''
            Name            = [string]$item.Name
            ModifyDateDetail = $ts
            ModifyDate      = [string]$item.ModifyDate
        })
    } else {
        $subFolder = $item.GetFolder
        if (-not $subFolder) { continue }
        try {
            $subItems = @($subFolder.Items())
        } catch { continue }
        foreach ($subItem in $subItems) {
            if (-not $subItem.IsFolder) {
                $ts = ''
                if ($modifiedIndex -ne $null) {
                    try { $ts = [string]$subFolder.GetDetailsOf($subItem, $modifiedIndex) } catch {}
                }
                if (-not $ts) { $ts = [string]$subItem.ModifyDate }
                $result.Add([PSCustomObject]@{
                    ParentName      = [string]$item.Name
                    Name            = [string]$subItem.Name
                    ModifyDateDetail = $ts
                    ModifyDate      = [string]$subItem.ModifyDate
                })
            }
        }
    }
}
if ($result.Count -gt 0) { @($result) | ConvertTo-Json -Compress }
""",
        )
        ok, result = self._run_mtp_powershell_json(
            script, timeout_seconds=timeout_seconds or self.MTP_LIST_TIMEOUT_SECONDS
        )
        if not ok:
            return False, {}

        raw_items = (
            result if isinstance(result, list)
            else ([result] if isinstance(result, dict) else [])
        )
        preferred = {".jpg", ".jpeg", ".png"}
        info: dict[str, dict[str, str]] = {}
        for item in raw_items:
            name = str(item.get("Name") or "").strip()
            parent = str(item.get("ParentName") or "").strip()
            if not name:
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in preferred:
                continue
            guid_key = os.path.splitext(name)[0].upper()
            if guid_key and guid_key not in info:
                device_ts = (
                    str(item.get("ModifyDateDetail") or "").strip()
                    or str(item.get("ModifyDate") or "").strip()
                )
                info[guid_key] = {
                    "preview_name": name,
                    "parent_name": parent,
                    "device_ts": device_ts,
                }

        return True, info

    def clear_stale_preview_cache(self) -> None:
        self._mtp_preview_items_cache.clear()
        self._preview_timestamp_cache.clear()
        self._preview_timestamps_loaded = False

        root = (self._config.rc2_folder or "").strip()
        if not root:
            return

        # Delete the persisted timestamps file so stale entries don't survive.
        try:
            os.remove(self._preview_timestamps_path(root))
        except OSError:
            pass

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

    def clear_preview_cache_for_guid(self, guid: str) -> None:
        """Delete the locally-cached preview file and stored timestamp for
        *guid* so the next refresh re-fetches the image from the device."""
        root = (self._config.rc2_folder or "").strip()
        if not root or not guid:
            return
        self._preview_timestamp_cache.pop(guid, None)
        self._save_preview_timestamps(root)
        cache_base = self._preview_cache_path(root, guid)
        for candidate in self._preview_name_candidates(guid):
            ext = os.path.splitext(candidate)[1].lower()
            cache_path = f"{cache_base}{ext}"
            if os.path.exists(cache_path):
                try:
                    os.remove(cache_path)
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

    def _list_mtp_items_cached(
        self,
        mtp_path: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, List[dict[str, Any]] | str]:
        cached = self._mtp_preview_items_cache.get(mtp_path)
        if cached is not None:
            return True, cached

        if timeout_seconds is None:
            ok, result = self._list_mtp_items(mtp_path)
        else:
            ok, result = self._list_mtp_items(mtp_path, timeout_seconds=timeout_seconds)
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

    def get_mission_preview_path(
        self,
        guid: str,
        copy_timeout_seconds: int | None = None,
        list_timeout_seconds: int | None = None,
        allow_live_fetch: bool = True,
    ) -> str | None:
        root = (self._config.rc2_folder or "").strip()
        if not root:
            return None

        if self._is_mtp_path(root):
            cache_base = self._preview_cache_path(root, guid)

            # Load persisted timestamps once per session so restarts are fast.
            self._load_preview_timestamps(root)

            # One bulk PowerShell call lists map_preview/ AND all its
            # subfolders, covering all missions.  Shared across all missions
            # in this refresh cycle via _mtp_preview_bulk_cache.
            preview_folder = self._mtp_join(root, "map_preview")
            ok_bulk, bulk_info = self._list_mtp_preview_bulk(
                preview_folder,
                timeout_seconds=list_timeout_seconds,
            )

            # Listing failed — fall back to disk cache without timestamp check.
            if not ok_bulk:
                return self._find_usable_cached_preview(cache_base, guid)

            entry = bulk_info.get(guid.upper())

            # No preview on device; return disk cache if we have one.
            if not entry:
                return self._find_usable_cached_preview(cache_base, guid)

            preview_name = entry["preview_name"]
            parent_name = entry["parent_name"]
            device_ts = entry["device_ts"]
            source_folder = (
                self._mtp_join(preview_folder, parent_name)
                if parent_name else preview_folder
            )

            # Return disk cache immediately when timestamp is unchanged (or
            # unavailable — e.g. device returns empty string for this field).
            cached = self._find_usable_cached_preview(cache_base, guid)
            cached_ts = self._preview_timestamp_cache.get(guid, "")
            if cached and (not device_ts or device_ts == cached_ts):
                return cached

            if not allow_live_fetch:
                return cached  # Stale but can't re-fetch; return what we have.

            # Cache is absent or device timestamp is newer; copy fresh from device.
            if cached:
                try:
                    os.remove(cached)
                except OSError:
                    pass

            ext = os.path.splitext(preview_name)[1].lower() or ".jpg"
            cache_path = f"{cache_base}{ext}"
            temp_copy = self._cache_temp_copy_path(cache_path)
            if copy_timeout_seconds is None:
                ok, _ = self._copy_file_from_mtp_folder(source_folder, preview_name, temp_copy)
            else:
                ok, _ = self._copy_file_from_mtp_folder(
                    source_folder,
                    preview_name,
                    temp_copy,
                    timeout_seconds=copy_timeout_seconds,
                )
            if ok and self._is_usable_preview_file(temp_copy):
                self._promote_cache_copy(temp_copy, cache_path)
                if device_ts:
                    self._preview_timestamp_cache[guid] = device_ts
                    self._save_preview_timestamps(root)
                return cache_path
            if os.path.exists(temp_copy):
                try:
                    os.remove(temp_copy)
                except OSError:
                    pass
            return None

        if self._is_adb_path(root):
            return self._rc_backend.get_preview_path(
                root, guid,
                copy_timeout_seconds=copy_timeout_seconds,
                list_timeout_seconds=list_timeout_seconds,
                allow_live_fetch=allow_live_fetch,
            )

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

    def get_all_mission_preview_paths(
        self,
        missions: "List[RC2Mission]",
        copy_timeout_seconds: int | None = None,
        list_timeout_seconds: int | None = None,
    ) -> "dict[str, str | None]":
        """Return {guid: disk_cache_path | None} for every mission.

        For MTP paths a single PowerShell listing call is shared across all
        missions; timestamps determine whether each image needs re-fetching.
        For other path types each mission is looked up individually."""
        root = (self._config.rc2_folder or "").strip()
        if not root:
            return {m.guid: None for m in missions}

        if self._is_mtp_path(root):
            self._load_preview_timestamps(root)
            preview_folder = self._mtp_join(root, "map_preview")
            ok_bulk, bulk_info = self._list_mtp_preview_bulk(
                preview_folder, timeout_seconds=list_timeout_seconds
            )

            result: dict[str, str | None] = {}
            for mission in missions:
                guid = mission.guid
                cache_base = self._preview_cache_path(root, guid)

                if not ok_bulk:
                    result[guid] = self._find_usable_cached_preview(cache_base, guid)
                    continue

                entry = bulk_info.get(guid.upper())
                if not entry:
                    result[guid] = self._find_usable_cached_preview(cache_base, guid)
                    continue

                preview_name = entry["preview_name"]
                parent_name = entry["parent_name"]
                device_ts = entry["device_ts"]
                source_folder = (
                    self._mtp_join(preview_folder, parent_name)
                    if parent_name else preview_folder
                )

                cached = self._find_usable_cached_preview(cache_base, guid)
                cached_ts = self._preview_timestamp_cache.get(guid, "")
                if cached and (not device_ts or device_ts == cached_ts):
                    result[guid] = cached
                    continue

                # Device has a newer image — copy from device.
                if cached:
                    try:
                        os.remove(cached)
                    except OSError:
                        pass

                ext = os.path.splitext(preview_name)[1].lower() or ".jpg"
                cache_path = f"{cache_base}{ext}"
                temp_copy = self._cache_temp_copy_path(cache_path)
                if copy_timeout_seconds is None:
                    ok, _ = self._copy_file_from_mtp_folder(
                        source_folder, preview_name, temp_copy
                    )
                else:
                    ok, _ = self._copy_file_from_mtp_folder(
                        source_folder, preview_name, temp_copy,
                        timeout_seconds=copy_timeout_seconds,
                    )
                if ok and self._is_usable_preview_file(temp_copy):
                    self._promote_cache_copy(temp_copy, cache_path)
                    if device_ts:
                        self._preview_timestamp_cache[guid] = device_ts
                        self._save_preview_timestamps(root)
                    result[guid] = cache_path
                else:
                    if os.path.exists(temp_copy):
                        try:
                            os.remove(temp_copy)
                        except OSError:
                            pass
                    result[guid] = None
            return result

        # ADB / local filesystem — fall back to individual lookups.
        return {
            m.guid: self.get_mission_preview_path(
                m.guid,
                copy_timeout_seconds=copy_timeout_seconds,
                list_timeout_seconds=list_timeout_seconds,
            )
            for m in missions
        }

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

    def write_waypoint_text_file(self, filename: str = "temp.txt", content: str = "temp") -> Tuple[bool, str]:
        """Write a small text file into the configured waypoint root."""
        root = (self._config.rc2_folder or "").strip()
        name = (filename or "").strip()
        if not root:
            return False, "RC-2 root is not configured."
        if not name:
            return False, "Filename is required."
        if any(sep in name for sep in ("/", "\\")):
            return False, "Filename must not include path separators."

        return self._rc_backend.write_text_file(root, name, content)

    # ------------------------------------------------------------------
    # Properties (forwarded from config for convenience)
    # ------------------------------------------------------------------
    @property
    def rc2_folder(self) -> str:
        return self._config.rc2_folder

    def get_rc2_connection_mode(self) -> str:
        if not (self._config.rc2_folder or "").strip():
            return "Not Set"
        return self._rc_backend.get_connection_mode()

    def is_rc2_connected(self, timeout_seconds: int | None = None) -> bool:
        """Best-effort connectivity probe for the currently configured RC-2 root."""
        if not (self._config.rc2_folder or "").strip():
            return False
        return self._rc_backend.is_connected(timeout_seconds=timeout_seconds)

    @property
    def pc_folder(self) -> str:
        return self._config.pc_folder

    def get_rc2_refresh_retry_interval_seconds(self) -> int:
        return self._config.rc2_refresh_retry_interval_seconds

    def get_dummy_slot_guid(self) -> str:
        return self._config.dummy_slot_guid

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
        self._preview_timestamp_cache.clear()
        self._preview_timestamps_loaded = False
        self._rc_backend = self._create_rc_backend(cleaned)
        self._config.save()

    def set_pc_folder(self, path: str) -> None:
        self._config.pc_folder = os.path.normpath(path)
        self._config.save()

    def set_dummy_slot_guid(self, guid: str) -> None:
        self._config.dummy_slot_guid = guid
        self._config.save()

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------
    def load_rc2_missions(self) -> List[RC2Mission]:
        """
        Scan the RC-2 root folder. Each sub-directory is a GUID mission slot.
        Returns a sorted list of RC2Mission objects.
        """
        self._last_error = None
        root = self._config.rc2_folder
        missions, err = self._rc_backend.list_missions(root)
        if err:
            self._set_last_error(err)
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
        missions, err = self._rc_backend.list_missions(adb_path)
        if err:
            self._set_last_error(f"[SyncViewModel] Error scanning RC-2 ADB folder: {err}")
        return missions

    def load_pc_kmz_files(self) -> List[KMZFile]:
        """
        Recursively scan the PC source folder for .kmz files.
        Returns a sorted list of KMZFile objects with full paths.
        """
        self._last_error = None
        files, err = self._pc_backend.list_kmz_files()
        if err:
            self._set_last_error(err)
        return files

    def delete_rc2_mission(self, mission: RC2Mission) -> Tuple[bool, str]:
        ok, msg = self._rc_backend.delete_mission(mission)
        if ok:
            self.clear_preview_cache_for_guid(mission.guid)
        return ok, msg

    def delete_pc_kmz_file(self, kmz_file: KMZFile) -> Tuple[bool, str]:
        return self._pc_backend.delete_file(kmz_file)

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

        copy_fn = (
            self._execute_copy_mtp
            if self._rc_backend.get_connection_mode() == "MTP"
            else self._execute_copy_adb
        )
        ok, msg = copy_fn(target_mission, kmz_file, dest_filename)
        if ok:
            self._record_copy_mapping(kmz_file, target_mission, dest_filename)
            self.clear_preview_cache_for_guid(target_mission.guid)
        return ok, msg

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

        ok, out = self._rc_backend.copy_file_from_device(
            mission.full_folder_path, source_filename, dest_path
        )
        if not ok:
            return False, f"Copy from device failed:\n{out}"
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
        ok, full_folder = self._rc_backend.create_slot_folder(root, new_guid)
        if not ok:
            return False, f"Failed to create slot folder:\n{full_folder}"
        return True, RC2Mission(guid=new_guid, kmz_name="", full_folder_path=full_folder)

    def _execute_copy_mtp(
        self,
        mission: RC2Mission,
        kmz_file: KMZFile,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        if not os.path.isfile(kmz_file.full_path):
            return False, f"Source file not found:\n{kmz_file.full_path}"

        ok, out = self._rc_backend.copy_file_to_device(
            mission.full_folder_path, kmz_file.full_path, dest_filename
        )
        if not ok:
            return False, f"MTP copy failed:\n{out}"

        verified, verify_msg = self._verify_mtp_copy_via_pull(
            mission, kmz_file.full_path, dest_filename
        )
        if not verified:
            return False, f"MTP copy verification failed:\n{verify_msg}"

        return True, (
            f"Copied '{kmz_file.filename}'\n"
            f"  → mission  : {mission.guid}\n"
            f"  → saved as : {dest_filename}"
        )

    def _verify_mtp_copy_via_pull(
        self,
        mission: RC2Mission,
        source_path: str,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        expected_size = os.path.getsize(source_path)

        fd, temp_path = tempfile.mkstemp(prefix="djirc2kmzsync-verify-", suffix=".kmz")
        os.close(fd)
        try:
            ok_pull, out_pull = self._copy_file_from_mtp_folder(
                mission.full_folder_path,
                dest_filename,
                temp_path,
                timeout_seconds=self.MTP_VERIFY_TIMEOUT_SECONDS,
            )
            if not ok_pull:
                return False, f"Unable to pull destination for verification:\n{out_pull}"

            actual_size = os.path.getsize(temp_path)
            size_diff = abs(actual_size - expected_size)
            percent_diff = (size_diff / float(expected_size) * 100.0) if expected_size > 0 else 100.0
            if actual_size <= 0 or (
                size_diff > self.MTP_SIZE_TOLERANCE_BYTES
                and percent_diff > self.MTP_SIZE_TOLERANCE_PERCENT
            ):
                return False, (
                    "Destination size mismatch.\n"
                    f"Expected size: {expected_size} bytes\n"
                    f"Destination size: {actual_size} bytes\n"
                    f"Tolerance: {self.MTP_SIZE_TOLERANCE_PERCENT}% or {self.MTP_SIZE_TOLERANCE_BYTES} bytes"
                )
        except OSError as e:
            return False, f"Verification failed:\n{e}"
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

        return True, "ok"

    def _verify_mtp_copy_quick(
        self,
        mission: RC2Mission,
        kmz_file: KMZFile,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        deadline = time.monotonic() + 6.0
        last_size: int | None = None

        while True:
            ok_list, result = self._list_mtp_items(mission.full_folder_path, timeout_seconds=8)
            if ok_list and isinstance(result, list):
                match = next(
                    (
                        item for item in result
                        if not bool(item.get("IsFolder"))
                        and str(item.get("Name") or "").strip() == dest_filename
                    ),
                    None,
                )
                if match is not None:
                    try:
                        current_size = int(match.get("Size") or 0)
                    except (TypeError, ValueError):
                        current_size = 0
                    last_size = current_size
                    return True, "ok"

            if time.monotonic() >= deadline:
                return False, (
                    "MTP copy could not be confirmed at destination filename.\n"
                    f"Destination size: {last_size if last_size is not None else 'missing'} bytes"
                )
            time.sleep(0.2)

    def _verify_mtp_copied_bytes(
        self,
        mission: RC2Mission,
        kmz_file: KMZFile,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        expected_size = os.path.getsize(kmz_file.full_path)
        try:
            with open(kmz_file.full_path, "rb") as source_fh:
                source_hash = hashlib.sha256(source_fh.read()).hexdigest()
        except OSError as e:
            return False, f"MTP copy verification failed (unable to read source):\n{e}"

        # MTP can briefly report stale bytes right after CopyHere returns.
        deadline = time.monotonic() + 2.5
        last_detail = ""
        while True:
            ok_read, payload = self._read_slot_file_bytes(mission, dest_filename)
            if ok_read and isinstance(payload, bytes):
                copied_bytes = payload
                copied_size = len(copied_bytes)
                if copied_size == expected_size:
                    copied_hash = hashlib.sha256(copied_bytes).hexdigest()
                    if copied_hash == source_hash:
                        return True, "ok"
                    last_detail = (
                        "MTP copy verification failed (content hash mismatch).\n"
                        f"Source SHA256: {source_hash}\n"
                        f"Destination SHA256: {copied_hash}"
                    )
                else:
                    last_detail = (
                        "MTP copy verification failed (size mismatch).\n"
                        f"Source size: {expected_size} bytes\n"
                        f"Destination size: {copied_size} bytes"
                    )
            else:
                detail = payload if isinstance(payload, str) else "unknown read error"
                last_detail = f"MTP copy verification failed (unable to read destination):\n{detail}"

            if time.monotonic() >= deadline:
                return False, last_detail
            time.sleep(0.25)

        return False, "MTP copy verification failed (unknown state)."

    def _execute_copy_adb(
        self,
        mission: RC2Mission,
        kmz_file: KMZFile,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        if not os.path.isfile(kmz_file.full_path):
            return False, f"Source file not found:\n{kmz_file.full_path}"

        ok, out = self._rc_backend.copy_file_to_device(
            mission.full_folder_path, kmz_file.full_path, dest_filename
        )
        if not ok:
            return False, f"ADB copy failed:\n{out}"

        return True, (
            f"Copied '{kmz_file.filename}'\n"
            f"  → mission  : {mission.guid}\n"
            f"  → saved as : {dest_filename}"
        )

    def _list_slot_files(self, mission: RC2Mission) -> Tuple[bool, List[str] | str]:
        return self._rc_backend.list_slot_files(mission)

    def _read_slot_file_bytes(self, mission: RC2Mission, filename: str) -> Tuple[bool, bytes | str]:
        return self._rc_backend.read_file_bytes(mission, filename)

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

        if self._is_mtp_path(root) or self._is_adb_path(root):
            return self._rc_backend.list_folder_items(folder_path)

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

        if self._is_mtp_path(root) or self._is_adb_path(root):
            return self._rc_backend.read_file_bytes_from_path(folder_path, filename)

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



