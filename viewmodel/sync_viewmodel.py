import hashlib
import io
import json
import os
import re
import shutil
import subprocess
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

from backends.backend_factory import BackendFactory
from config.config_manager import ConfigManager
from model.kmz_file import KMZFile
from model.rc2_mission import RC2Mission
from services.copy_map_service import CopyMapService


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
    MTP_VERIFY_TIMEOUT_SECONDS = 5
    MTP_SIZE_TOLERANCE_PERCENT = 10.0
    MTP_SIZE_TOLERANCE_BYTES = 4096
    DEEP_INSPECT_TIME_BUDGET_SECONDS_MTP = 8.0
    DEEP_INSPECT_MAX_DEPTH_MTP = 1
    DEEP_INSPECT_MAX_FOLDERS_MTP = 24
    DEEP_INSPECT_MAX_SCAN_FOLDERS_MTP = 10
    DEEP_INSPECT_MAX_FILE_READS_MTP = 16
    DEEP_INSPECT_FOLDER_HINT_TOKENS = (
        "history", "record", "mission", "meta", "index", "db", "database", "sqlite",
    )
    DEEP_INSPECT_FOLDER_SKIP_TOKENS = (
        "mediacache", "media_cache", "cachevideo", "video", "thumb", "thumbnail", "image",
    )
    def __init__(self, config: ConfigManager, copy_map_path: str | None = None):
        self._config = config
        self._last_error: str | None = None
        self._preview_timestamp_cache: dict[str, str] = {}  # guid → device ModifyDate string
        self._preview_timestamps_loaded: bool = False
        self._mtp_operation_lock = threading.Lock()
        self._rc_backend = BackendFactory.create_rc(config.rc2_folder, self._config)
        self._pc_backend = BackendFactory.create_pc(config)
        self._copy_map_service = CopyMapService(copy_map_path=copy_map_path)

    @property
    def _copy_map_path(self) -> str:
        return self._copy_map_service.copy_map_path

    @_copy_map_path.setter
    def _copy_map_path(self, value: str) -> None:
        self._copy_map_service.copy_map_path = value

    @classmethod
    def _default_copy_map_filename(cls) -> str:
        return CopyMapService.default_copy_map_filename()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _default_copy_map_payload() -> dict[str, Any]:
        return CopyMapService.default_payload()

    def _ensure_copy_map_exists(self) -> None:
        self._copy_map_service.ensure_copy_map_exists()

    def _load_copy_map(self) -> dict[str, Any]:
        return self._copy_map_service.load_copy_map()

    def _save_copy_map(self, payload: dict[str, Any]) -> None:
        self._copy_map_service.save_copy_map(payload)

    def _record_copy_mapping(self, source: KMZFile, mission: RC2Mission, dest_filename: str) -> None:
        copied_at = self._now_iso()
        updated_at = self._now_iso()
        self._copy_map_service.record_mapping(
            source_filename=source.filename,
            source_full_path=source.full_path,
            target_mission_guid=mission.guid,
            target_kmz_filename=dest_filename,
            target_folder_path=mission.full_folder_path,
            connection_mode=self.get_rc2_connection_mode(),
            copied_at=copied_at,
            updated_at=updated_at,
        )

    def get_copy_mapping_summary(self) -> tuple[list[dict[str, str]], str, str]:
        return self._copy_map_service.get_summary()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _is_device_backend_active(self) -> bool:
        return self._rc_backend.get_connection_mode().strip().upper() in {"MTP", "ADB"}

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

    @staticmethod
    def _is_rc2_slot_name(name: str) -> bool:
        return name.strip().lower() not in {"capability", "map_preview"}

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

    def clear_all_preview_cache(self) -> Tuple[bool, str]:
        """Clear all locally cached RC preview images for the configured root."""
        root = (self._config.rc2_folder or "").strip()
        if not root:
            return False, "RC-2 root is not configured."

        # Clear legacy timestamp/cache artifacts used by older preview paths.
        self.clear_stale_preview_cache()

        try:
            self._rc_backend.clear_preview_cache(root)
            self._rc_backend.invalidate_cache()
        except Exception as exc:
            return False, f"Failed to clear preview cache:\n{exc}"

        return True, "Preview cache cleared. Next refresh will reload previews from RC-2."

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

    def _fetch_and_cache_preview(
        self,
        *,
        root: str,
        guid: str,
        source_folder: str,
        preview_name: str,
        device_ts: str,
        copy_timeout_seconds: int | None,
        cache_base: str,
    ) -> str | None:
        ext = os.path.splitext(preview_name)[1].lower() or ".jpg"
        cache_path = f"{cache_base}{ext}"
        temp_copy = self._cache_temp_copy_path(cache_path)
        try:
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
            return None
        finally:
            if os.path.exists(temp_copy):
                try:
                    os.remove(temp_copy)
                except OSError:
                    pass

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

        if not self._is_device_backend_active():
            return None

        return self._rc_backend.get_preview_path(
            root, guid,
            copy_timeout_seconds=copy_timeout_seconds,
            list_timeout_seconds=list_timeout_seconds,
            allow_live_fetch=allow_live_fetch,
        )

    def get_all_mission_preview_paths(
        self,
        missions: "List[RC2Mission]",
        copy_timeout_seconds: int | None = None,
        list_timeout_seconds: int | None = None,
        allow_live_fetch: bool = True,
    ) -> "dict[str, str | None]":
        """Return {guid: disk_cache_path | None} for every mission.

        For MTP paths a single PowerShell listing call is shared across all
        missions; timestamps determine whether each image needs re-fetching.
        For other path types each mission is looked up individually."""
        root = (self._config.rc2_folder or "").strip()
        if not root:
            return {m.guid: None for m in missions}

        if not self._is_device_backend_active():
            return {m.guid: None for m in missions}

        bulk_fetch = getattr(self._rc_backend, "get_preview_paths_bulk", None)
        if allow_live_fetch and callable(bulk_fetch):
            return bulk_fetch(
                root,
                [m.guid for m in missions],
                copy_timeout_seconds=copy_timeout_seconds,
                list_timeout_seconds=list_timeout_seconds,
            )
        return {
            m.guid: self._rc_backend.get_preview_path(
                root,
                m.guid,
                copy_timeout_seconds=copy_timeout_seconds,
                list_timeout_seconds=list_timeout_seconds,
                allow_live_fetch=allow_live_fetch,
            )
            for m in missions
        }

    def _detect_mtp_rc2_folder(self) -> str | None:
        if os.name != "nt":
            return None

        # Probe MTP reachability directly via PowerShell to avoid creating any
        # secondary backend instances; the ViewModel keeps exactly one RC backend.
        script = self._mtp_script(
            self.DEFAULT_MTP_RC2_ROOT,
            """
Write-Output "OK"
""",
        )
        ok, _ = self._run_powershell(script, timeout_seconds=10)
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

    def _set_last_error(self, message: str) -> None:
        self._last_error = message

    def consume_last_error(self) -> str | None:
        message = self._last_error
        self._last_error = None
        return message

    def diagnose_rc2_connection(self) -> Tuple[bool, str, str | None]:
        root = (self._config.rc2_folder or "").strip()
        scheme = BackendFactory.path_scheme(root)

        # If a configured transport is currently reachable, keep it.
        if root and scheme in {"adb", "mtp"}:
            ok, status = self._rc_backend.get_status()
            if ok:
                return True, f"Configured RC-2 root is reachable via {scheme.upper()}: {root}", root

        if root and BackendFactory.path_scheme(root) not in {"adb", "mtp"} and os.path.isdir(root):
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
        old_backend = self._rc_backend
        if BackendFactory.path_scheme(cleaned) in {"adb", "mtp"}:
            self._config.rc2_folder = cleaned
        else:
            self._config.rc2_folder = os.path.normpath(cleaned)
        self._preview_timestamp_cache.clear()
        self._preview_timestamps_loaded = False
        self._rc_backend = BackendFactory.create_rc(cleaned, self._config)
        close_old = getattr(old_backend, "close", None)
        if callable(close_old):
            try:
                close_old()
            except Exception:
                pass
        self._config.save()

    def shutdown(self) -> None:
        """Release backend resources on app close."""
        close_backend = getattr(self._rc_backend, "close", None)
        if callable(close_backend):
            try:
                close_backend()
            except Exception:
                pass

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
        adb_backend = BackendFactory.create_rc(self.DEFAULT_ADB_RC2_ROOT, self._config)
        try:
            return adb_backend.get_status()
        finally:
            close_backend = getattr(adb_backend, "close", None)
            if callable(close_backend):
                try:
                    close_backend()
                except Exception:
                    pass

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

        if not os.path.isfile(kmz_file.full_path):
            return False, f"Source file not found:\n{kmz_file.full_path}"

        ok, out = self._rc_backend.copy_file_to_device(
            target_mission.full_folder_path, kmz_file.full_path, dest_filename
        )
        if not ok:
            return False, f"Copy failed:\n{out}"

        if self._rc_backend.get_connection_mode() == "MTP":
            verified, verify_msg = self._verify_mtp_copy_via_pull(
                target_mission, kmz_file.full_path, dest_filename
            )
            if not verified:
                return False, f"MTP copy verification failed:\n{verify_msg}"

        msg = (
            f"Copied '{kmz_file.filename}'\n"
            f"  → mission  : {target_mission.guid}\n"
            f"  → saved as : {dest_filename}"
        )
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

        fd, temp_path = tempfile.mkstemp(prefix="djirc2kmzsync-copyback-", suffix=".kmz")
        os.close(fd)
        try:
            ok, out = self._rc_backend.copy_file_from_device(
                mission.full_folder_path, source_filename, temp_path
            )
            if not ok:
                return False, f"Copy from device failed:\n{out}"

            os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
            if os.path.exists(dest_path):
                os.remove(dest_path)
            shutil.copy2(temp_path, dest_path)
        except OSError as exc:
            return False, f"Copy-back failed:\n{exc}"
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

    def _prepare_new_mission_target(self) -> Tuple[bool, RC2Mission | str]:
        new_guid = str(uuid.uuid4()).upper()
        root = (self._config.rc2_folder or "").strip()
        ok, full_folder = self._rc_backend.create_slot_folder(root, new_guid)
        if not ok:
            return False, f"Failed to create slot folder:\n{full_folder}"
        return True, RC2Mission(guid=new_guid, kmz_name="", full_folder_path=full_folder)

    def _verify_mtp_copy_via_pull(
        self,
        mission: RC2Mission,
        source_path: str,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        expected_size = os.path.getsize(source_path)
        try:
            ok_size, out_size = self._rc_backend.get_file_size_from_path(
                mission.full_folder_path,
                dest_filename,
            )
            if not ok_size:
                return False, f"Unable to query destination size for verification:\n{out_size}"

            actual_size = int(out_size)
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

        return True, "ok"

    def _list_slot_files(self, mission: RC2Mission) -> Tuple[bool, List[str] | str]:
        return self._rc_backend.list_slot_files(mission)

    def _read_slot_file_bytes(self, mission: RC2Mission, filename: str) -> Tuple[bool, bytes | str]:
        return self._rc_backend.read_file_bytes(mission, filename)

    def _list_folder_items_with_type(self, folder_path: str) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        if not self._is_device_backend_active():
            return False, "RC-2 backend is not active. Waiting for device detection."

        return self._rc_backend.list_folder_items(folder_path)

    def _read_file_bytes_from_folder(self, folder_path: str, filename: str) -> Tuple[bool, bytes | str]:
        if not self._is_device_backend_active():
            return False, "RC-2 backend is not active. Waiting for device detection."

        return self._rc_backend.read_file_bytes_from_path(folder_path, filename)

    def _expand_inspect_folders(
        self,
        seeds: List[str],
        max_depth: int = 2,
        max_folders: int = 80,
        max_listings: int = 40,
        deadline: float | None = None,
        include_tokens: Tuple[str, ...] | None = None,
        skip_tokens: Tuple[str, ...] | None = None,
    ) -> List[str]:
        seen: set[str] = set()
        ordered: List[str] = []
        queue_items: List[Tuple[str, int]] = []
        listing_count = 0

        for path in seeds:
            cleaned = (path or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            ordered.append(cleaned)
            queue_items.append((cleaned, 0))

        index = 0
        while index < len(queue_items) and len(ordered) < max_folders:
            if deadline is not None and time.monotonic() > deadline:
                break
            if listing_count >= max_listings:
                break
            folder, depth = queue_items[index]
            index += 1
            if depth >= max_depth:
                continue

            ok, listed = self._list_folder_items_with_type(folder)
            listing_count += 1
            if not ok:
                continue

            items = listed if isinstance(listed, list) else []
            for name, is_folder, _ in items:
                if not is_folder:
                    continue
                lowered = name.lower()
                if skip_tokens and any(token in lowered for token in skip_tokens):
                    continue
                if include_tokens and not any(token in lowered for token in include_tokens):
                    continue
                child = self._rc_backend.join_folder_path(folder, name)
                if child in seen:
                    continue
                seen.add(child)
                ordered.append(child)
                if len(ordered) >= max_folders:
                    break
                queue_items.append((child, depth + 1))

        return ordered

    def _inspect_metadata_history_candidates(self, mission: RC2Mission, kmz_name: str) -> List[str]:
        return self._rc_backend.inspect_metadata_history_candidates(
            mission,
            kmz_name,
            time_budget_seconds_mtp=self.DEEP_INSPECT_TIME_BUDGET_SECONDS_MTP,
            max_depth_mtp=self.DEEP_INSPECT_MAX_DEPTH_MTP,
            max_folders_mtp=self.DEEP_INSPECT_MAX_FOLDERS_MTP,
            max_scan_folders_mtp=self.DEEP_INSPECT_MAX_SCAN_FOLDERS_MTP,
            max_file_reads_mtp=self.DEEP_INSPECT_MAX_FILE_READS_MTP,
            folder_hint_tokens=self.DEEP_INSPECT_FOLDER_HINT_TOKENS,
            folder_skip_tokens=self.DEEP_INSPECT_FOLDER_SKIP_TOKENS,
        )

    def _inspect_binary_metadata_candidates(self, mission: RC2Mission, kmz_name: str) -> List[str]:
        return self._rc_backend.inspect_binary_metadata_candidates(
            mission,
            kmz_name,
            time_budget_seconds_mtp=self.DEEP_INSPECT_TIME_BUDGET_SECONDS_MTP,
            max_depth_mtp=self.DEEP_INSPECT_MAX_DEPTH_MTP,
            max_folders_mtp=self.DEEP_INSPECT_MAX_FOLDERS_MTP,
            max_scan_folders_mtp=self.DEEP_INSPECT_MAX_SCAN_FOLDERS_MTP,
            folder_hint_tokens=self.DEEP_INSPECT_FOLDER_HINT_TOKENS,
            folder_skip_tokens=self.DEEP_INSPECT_FOLDER_SKIP_TOKENS,
        )

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

    @staticmethod
    def _count_waypoints_in_text(text: str) -> int:
        # DJI KMZ route points are represented as Placemark entries in KML/WPML.
        return len(re.findall(r"<\s*Placemark\b", text, flags=re.IGNORECASE))

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
                waypoint_count = 0
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
                    waypoint_count += self._count_waypoints_in_text(text)
                    for value in self._extract_name_like_fields_from_text(text):
                        if value not in discovered_names:
                            discovered_names.append(value)
                        if len(discovered_names) >= 20:
                            break

                lines.append(f"Waypoint count (Placemark): {waypoint_count}")

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



