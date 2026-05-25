"""
adb_backend.py
--------------
Abstract ADB backend -- shared logic for Windows and macOS.

Concrete subclasses override _adb_search_paths() to provide
platform-specific ADB executable locations. Everything else --
command execution, error formatting, mission listing, file
transfer, preview cache -- is identical on both platforms.

RC-2 waypoint root (Android filesystem):
    /sdcard/Android/data/dji.go.v5/files/waypoint
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from abc import abstractmethod
from datetime import datetime
from typing import List, Tuple

try:
    from PIL import Image
except ImportError:
    Image = None

from backends.mtp.windows_mtp_backend import (
    _choose_preview_folder,
    _choose_preview_name,
    _clear_preview_cache,
    _find_cached_preview,
    _is_usable_preview,
    _preview_cache_base,
    _promote_preview,
)
from backends.rc_backend import RCBackend
from config.config_manager import ConfigManager
from model.rc2_mission import RC2Mission

_NON_MISSION_FOLDERS = frozenset({"capability", "map_preview"})

DEFAULT_ADB_ROOT = "adb:/sdcard/Android/data/dji.go.v5/files/waypoint"


class ADBBackend(RCBackend):
    """
    Abstract ADB backend -- implements all ADB logic.

    Subclasses provide _adb_search_paths() for platform-specific
    executable discovery. No other overrides are required.
    """

    def __init__(self, config: ConfigManager) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Subclass contract -- executable discovery
    # ------------------------------------------------------------------

    @abstractmethod
    def _adb_search_paths(self) -> List[str]:
        """
        Return a list of candidate absolute paths to the ADB executable
        in platform-specific locations (SDK installs, package managers).

        The base resolver checks PATH and the ADB env var first; these
        candidates are consulted as a last resort.
        """

    # ------------------------------------------------------------------
    # Connection & mode
    # ------------------------------------------------------------------

    def is_connected(self, timeout_seconds: int | None = None) -> bool:
        ok, _ = self.get_status()
        return ok

    def get_connection_mode(self) -> str:
        return "ADB"

    def probe_root(self, path: str) -> bool:
        remote = _adb_remote_root(path)
        ok, _ = self._run(["shell", "ls", "-1", remote])
        return ok

    # ------------------------------------------------------------------
    # Mission listing
    # ------------------------------------------------------------------

    def list_missions(self, root: str) -> Tuple[List[RC2Mission], str | None]:
        remote_root = _adb_remote_root(root)
        ok, out = self._run(["shell", "ls", "-1", remote_root])
        if not ok:
            return [], f"[ADBBackend] Error listing missions: {out}"

        missions: List[RC2Mission] = []
        for entry in sorted(line.strip() for line in out.splitlines() if line.strip()):
            if entry.lower() in _NON_MISSION_FOLDERS:
                continue
            remote_slot = f"{remote_root.rstrip('/')}/{entry}"
            ok_slot, slot_out = self._run(["shell", "ls", "-1", remote_slot])
            if not ok_slot:
                continue
            kmz_files = sorted(
                line.strip()
                for line in slot_out.splitlines()
                if line.strip().lower().endswith(".kmz")
            )
            kmz_name = kmz_files[0] if kmz_files else ""
            missions.append(RC2Mission(
                guid=entry,
                kmz_name=kmz_name,
                full_folder_path=remote_slot,
                last_modified="",
            ))
        return missions, None

    # ------------------------------------------------------------------
    # Slot file operations
    # ------------------------------------------------------------------

    def list_slot_files(self, mission: RC2Mission) -> Tuple[bool, List[str] | str]:
        ok, out = self._run(["shell", "ls", "-1", mission.full_folder_path])
        if not ok:
            return False, out
        names = sorted(line.strip() for line in out.splitlines() if line.strip())
        return True, names

    def list_folder_items(
        self, path: str
    ) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        ok, out = self._run(["shell", "ls", "-1p", path])
        if not ok:
            return False, out
        output: List[Tuple[str, bool, str]] = []
        for line in out.splitlines():
            raw = line.strip()
            if not raw:
                continue
            is_folder = raw.endswith("/")
            name = raw[:-1] if is_folder else raw
            output.append((name, is_folder, ""))
        return True, output

    def read_file_bytes(
        self, mission: RC2Mission, filename: str
    ) -> Tuple[bool, bytes | str]:
        remote = f"{mission.full_folder_path.rstrip('/')}/{filename}"
        return self._pull_to_bytes(remote)

    def read_file_bytes_from_path(
        self, folder: str, filename: str
    ) -> Tuple[bool, bytes | str]:
        remote = f"{folder.rstrip('/')}/{filename}"
        return self._pull_to_bytes(remote)

    # ------------------------------------------------------------------
    # File transfer -- PC to RC-2
    # ------------------------------------------------------------------

    def copy_file_to_device(
        self,
        dest_folder: str,
        local_source_path: str,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        if not os.path.isfile(local_source_path):
            return False, f"Source file not found:\n{local_source_path}"

        remote_dest = f"{_adb_remote_root(dest_folder).rstrip('/')}/{dest_filename}"
        ok, out = self._run(["push", local_source_path, remote_dest])
        if not ok:
            return False, f"ADB push failed:\n{out}"
        return True, f"Copied to {remote_dest}"

    def write_text_file(
        self,
        dest_folder: str,
        filename: str,
        content: str,
    ) -> Tuple[bool, str]:
        fd, temp_path = tempfile.mkstemp(
            prefix="djirc2kmzsync-txt-", suffix=".txt"
        )
        os.close(fd)
        try:
            with open(temp_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            remote_dest = f"{_adb_remote_root(dest_folder).rstrip('/')}/{filename}"
            ok, out = self._run(["push", temp_path, remote_dest])
            if not ok:
                return False, f"ADB write failed:\n{out}"
            return True, f"Wrote {filename} to {remote_dest}"
        except OSError as exc:
            return False, f"File operation failed:\n{exc}"
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # File transfer -- RC-2 to PC
    # ------------------------------------------------------------------

    def copy_file_from_device(
        self,
        src_folder: str,
        filename: str,
        local_dest_path: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, str]:
        remote = f"{_adb_remote_root(src_folder).rstrip('/')}/{filename}"
        fd, temp_path = tempfile.mkstemp(
            prefix="djirc2kmzsync-pull-", suffix=".tmp"
        )
        os.close(fd)
        try:
            ok, out = self._run(["pull", remote, temp_path])
            if not ok:
                return False, f"ADB pull failed:\n{out}"
            dest_dir = os.path.dirname(local_dest_path)
            if dest_dir:
                os.makedirs(dest_dir, exist_ok=True)
            if os.path.exists(local_dest_path):
                os.remove(local_dest_path)
            shutil.move(temp_path, local_dest_path)
            return True, local_dest_path
        except OSError as exc:
            return False, str(exc)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Mission management
    # ------------------------------------------------------------------

    def create_slot_folder(self, root: str, guid: str) -> Tuple[bool, str]:
        remote_root = _adb_remote_root(root)
        remote_slot = f"{remote_root.rstrip('/')}/{guid}"
        ok, out = self._run(["shell", "mkdir", "-p", remote_slot])
        if not ok:
            return False, f"Failed to create ADB slot folder:\n{out}"
        return True, remote_slot

    def delete_mission(self, mission: RC2Mission) -> Tuple[bool, str]:
        ok, out = self._run(["shell", "rm", "-rf", mission.full_folder_path])
        if not ok:
            return False, f"ADB delete failed:\n{out}"
        return True, f"Deleted mission {mission.guid}"

    # ------------------------------------------------------------------
    # Preview images
    # ------------------------------------------------------------------

    def get_preview_path(
        self,
        root: str,
        guid: str,
        copy_timeout_seconds: int | None = None,
        list_timeout_seconds: int | None = None,
        allow_live_fetch: bool = True,
    ) -> str | None:
        cache_base = _preview_cache_base(root, guid)
        cached = _find_cached_preview(cache_base, guid)
        if cached:
            return cached

        if not allow_live_fetch:
            return None

        remote_root = _adb_remote_root(root)
        preview_dir = f"{remote_root.rstrip('/')}/map_preview"

        ok_ls, out_ls = self._run(["shell", "ls", "-1", preview_dir])
        if not ok_ls:
            return None

        names = [line.strip() for line in out_ls.splitlines() if line.strip()]
        preview_name: str | None = None
        source_dir = preview_dir

        # Flat layout: map_preview/<guid>.jpg
        for suffix in (".jpg", ".jpeg", ".png"):
            target = f"{guid.lower()}{suffix}"
            for name in names:
                if name.lower() == target:
                    preview_name = name
                    break
            if preview_name:
                break

        # Nested layout: map_preview/<guid>/<guid>.jpg
        if not preview_name:
            nested_folder = _choose_preview_folder(guid, names)
            if nested_folder:
                source_dir = f"{preview_dir.rstrip('/')}/{nested_folder}"
                ok_n, out_n = self._run(["shell", "ls", "-1", source_dir])
                if ok_n:
                    nested_names = [
                        line.strip() for line in out_n.splitlines() if line.strip()
                    ]
                    for suffix in (".jpg", ".jpeg", ".png"):
                        target = f"{guid.lower()}{suffix}"
                        for name in nested_names:
                            if name.lower() == target:
                                preview_name = name
                                break
                        if preview_name:
                            break

        # Direct probe fallback.
        if not preview_name:
            source_dir = f"{preview_dir.rstrip('/')}/{guid}"
            ok_p, out_p = self._run(["shell", "ls", "-1", source_dir])
            if ok_p:
                probe_names = [
                    line.strip() for line in out_p.splitlines() if line.strip()
                ]
                for suffix in (".jpg", ".jpeg", ".png"):
                    target = f"{guid.lower()}{suffix}"
                    for name in probe_names:
                        if name.lower() == target:
                            preview_name = name
                            break
                    if preview_name:
                        break

        if not preview_name:
            return None

        ext = os.path.splitext(preview_name)[1].lower() or ".jpg"
        cache_path = f"{cache_base}{ext}"
        remote_preview = f"{source_dir.rstrip('/')}/{preview_name}"
        temp_path = f"{cache_path}.{uuid.uuid4().hex}.tmp"

        ok, _ = self._run(["pull", remote_preview, temp_path])
        if ok and _is_usable_preview(temp_path):
            _promote_preview(temp_path, cache_path)
            return cache_path

        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return None

    def clear_preview_cache(self, root: str) -> None:
        _clear_preview_cache(root)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_status(self) -> Tuple[bool, str]:
        ok, out = self._run(["devices"])
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
            return False, (
                "No ADB devices detected. "
                "Connect the RC-2 and verify USB debugging is enabled."
            )
        for serial, state in rows:
            if state == "device":
                return True, f"ADB device connected: {serial}"

        first_serial, first_state = rows[0]
        if first_state == "offline":
            return False, (
                "ADB device is offline. "
                "Reconnect RC-2 USB, unlock/confirm USB debugging, and retry."
            )
        if first_state == "unauthorized":
            return False, (
                "ADB device is unauthorized. "
                "Accept USB debugging on RC-2 and retry."
            )
        return False, f"ADB device not ready: {first_serial} ({first_state})."

    # ------------------------------------------------------------------
    # Internal -- ADB execution
    # ------------------------------------------------------------------

    def _resolve_adb(self) -> str | None:
        exe = "adb.exe" if os.name == "nt" else "adb"

        # Active Python environment bin directory (useful in VS Code venvs).
        py_bin = os.path.join(os.path.dirname(sys.executable), exe)
        if os.path.isfile(py_bin):
            return py_bin

        # System PATH.
        from_path = shutil.which("adb")
        if from_path:
            return from_path

        # ADB environment variable.
        env_raw = (os.environ.get("ADB") or "").strip().strip('"')
        if env_raw:
            if os.path.isdir(env_raw):
                candidate = os.path.join(env_raw, exe)
            else:
                candidate = env_raw
            if os.path.isfile(candidate):
                return candidate

        # Platform-specific fallback paths from subclass.
        for candidate in self._adb_search_paths():
            if os.path.isfile(candidate):
                return candidate

        return None

    def _run(self, args: List[str]) -> Tuple[bool, str]:
        adb = self._resolve_adb()
        if not adb:
            return False, (
                "ADB executable not found. Install Android platform-tools "
                "and ensure 'adb' is on PATH, or set the ADB environment "
                "variable to the full adb executable path."
            )

        def _exec(cmd_args: List[str]) -> Tuple[int, str]:
            result = subprocess.run(
                [adb, *cmd_args],
                capture_output=True,
                text=True,
                check=False,
            )
            combined = ((result.stdout or "") + (result.stderr or "")).strip()
            return result.returncode, combined

        try:
            code, output = _exec(args)
        except OSError as exc:
            return False, f"Failed to run adb: {exc}"

        if code == 0:
            return True, output

        lower = output.lower()
        if any(
            token in lower
            for token in (
                "device offline",
                "unauthorized",
                "no devices/emulators found",
                "daemon not running",
            )
        ):
            # One restart attempt when ADB daemon was not yet running.
            _exec(["start-server"])
            retry_code, retry_output = _exec(args)
            if retry_code == 0:
                return True, retry_output
            return False, _format_adb_error(retry_output or output)

        return False, _format_adb_error(output)

    def _pull_to_bytes(self, remote_path: str) -> Tuple[bool, bytes | str]:
        fd, temp_path = tempfile.mkstemp(
            prefix="djirc2kmzsync-read-", suffix=".tmp"
        )
        os.close(fd)
        try:
            ok, out = self._run(["pull", remote_path, temp_path])
            if not ok:
                return False, out
            with open(temp_path, "rb") as fh:
                return True, fh.read()
        except OSError as exc:
            return False, str(exc)
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _adb_remote_root(path: str) -> str:
    raw = path.strip()
    if raw.lower().startswith("adb:"):
        raw = raw[4:].strip()
    if not raw:
        return DEFAULT_ADB_ROOT[4:]
    if not raw.startswith("/"):
        raw = f"/{raw}"
    return raw.replace("\\", "/")


def _format_adb_error(output: str) -> str:
    text = (output or "").strip()
    lower = text.lower()
    if "device offline" in lower:
        return (
            "ADB device is offline. Reconnect the RC-2 USB cable, "
            "unlock/confirm USB debugging on the RC-2, then verify with "
            "'adb devices' until it shows state 'device'."
        )
    if "unauthorized" in lower:
        return (
            "ADB device is unauthorized. Accept the USB debugging "
            "authorization prompt on the RC-2 and retry."
        )
    if "no devices/emulators found" in lower:
        return (
            "No ADB device detected. Connect the RC-2 via USB, "
            "enable USB debugging, and retry."
        )
    return text
