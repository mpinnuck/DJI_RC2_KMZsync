"""
adb_backend.py
--------------
Abstract ADB backend -- implements the nine _raw_* primitives via
adb subprocess commands.

Concrete subclasses override only _adb_search_paths() to provide
platform-specific ADB executable locations.

RC-2 waypoint root (Android filesystem):
    /sdcard/Android/data/dji.go.v5/files/waypoint
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from abc import abstractmethod
from typing import List, Tuple

from backends.rc_backend import RCBackend
from config.config_manager import ConfigManager
from model.rc2_mission import RC2Mission

DEFAULT_ADB_ROOT = "adb:/sdcard/Android/data/dji.go.v5/files/waypoint"


class ADBBackend(RCBackend):
    """
    Abstract ADB backend -- implements all nine _raw_* primitives.

    Subclasses provide only _adb_search_paths() for platform-specific
    executable discovery.
    """

    def __init__(self, config: ConfigManager) -> None:
        super().__init__(config)

    # ------------------------------------------------------------------
    # Subclass contract -- executable discovery only
    # ------------------------------------------------------------------

    @abstractmethod
    def _adb_search_paths(self) -> List[str]:
        """
        Return candidate absolute paths to the ADB executable for this
        platform (SDK installs, package managers).
        The base resolver checks PATH and the ADB env var first.
        """

    # ==================================================================
    # _raw_* primitives
    # ==================================================================

    def _raw_list_folder(
        self, path: str
    ) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        remote = _remote_path(path)
        ok, out = self._run(["shell", "ls", "-1p", remote])
        if not ok:
            return False, out
        rows: List[Tuple[str, bool, str]] = []
        for line in out.splitlines():
            raw = line.strip()
            if not raw:
                continue
            is_folder = raw.endswith("/")
            name = raw[:-1] if is_folder else raw
            rows.append((name, is_folder, ""))
        return True, rows

    def _raw_read_file(
        self, folder_path: str, filename: str, local_dest: str
    ) -> Tuple[bool, str]:
        remote = f"{_remote_path(folder_path).rstrip('/')}/{filename}"
        ok, out = self._run(["pull", remote, local_dest])
        if not ok:
            return False, f"ADB pull failed:\n{out}"
        return True, local_dest

    def _raw_write_file(
        self, dest_folder: str, local_source: str, dest_filename: str
    ) -> Tuple[bool, str]:
        remote = f"{_remote_path(dest_folder).rstrip('/')}/{dest_filename}"
        ok, out = self._run(["push", local_source, remote])
        if not ok:
            return False, f"ADB push failed:\n{out}"
        return True, remote

    def _raw_delete_file(
        self, folder_path: str, filename: str
    ) -> Tuple[bool, str]:
        remote = f"{_remote_path(folder_path).rstrip('/')}/{filename}"
        # Check existence first so we can return NOT_FOUND cleanly.
        ok_ls, ls_out = self._run(["shell", "ls", remote])
        if not ok_ls or filename not in ls_out:
            return True, "NOT_FOUND"
        ok, out = self._run(["shell", "rm", "-f", remote])
        if not ok:
            return False, f"ADB delete failed:\n{out}"
        return True, f"Deleted {filename}"

    def _raw_delete_folder(self, folder_path: str) -> Tuple[bool, str]:
        remote = _remote_path(folder_path)
        ok, out = self._run(["shell", "rm", "-rf", remote])
        if not ok:
            return False, f"ADB folder delete failed:\n{out}"
        return True, f"Deleted {remote}"

    def _raw_create_folder(
        self, parent_path: str, name: str
    ) -> Tuple[bool, str]:
        remote_parent = _remote_path(parent_path)
        remote_slot   = f"{remote_parent.rstrip('/')}/{name}"
        ok, out = self._run(["shell", "mkdir", "-p", remote_slot])
        if not ok:
            return False, f"ADB mkdir failed:\n{out}"
        return True, remote_slot

    def _raw_probe(self, root: str) -> bool:
        remote = _remote_path(root)
        ok, _ = self._run(["shell", "ls", "-1", remote])
        return ok

    def _raw_get_status(self) -> Tuple[bool, str]:
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

    def _raw_connection_mode(self) -> str:
        return "ADB"

    # ==================================================================
    # ADB execution layer
    # ==================================================================

    def _resolve_adb(self) -> str | None:
        exe = "adb.exe" if os.name == "nt" else "adb"

        # Active Python env bin (useful in VS Code venvs where PATH is stale).
        py_bin = os.path.join(os.path.dirname(sys.executable), exe)
        if os.path.isfile(py_bin):
            return py_bin

        from_path = shutil.which("adb")
        if from_path:
            return from_path

        env_raw = (os.environ.get("ADB") or "").strip().strip('"')
        if env_raw:
            candidate = (
                os.path.join(env_raw, exe) if os.path.isdir(env_raw) else env_raw
            )
            if os.path.isfile(candidate):
                return candidate

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


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _remote_path(path: str) -> str:
    """Strip the adb: prefix and normalise to a POSIX remote path."""
    raw = (path or "").strip()
    if raw.lower().startswith("adb:"):
        raw = raw[4:].strip()
    if not raw:
        return DEFAULT_ADB_ROOT[4:]
    if not raw.startswith("/"):
        raw = f"/{raw}"
    return raw.replace("\\", "/")


def _format_adb_error(output: str) -> str:
    text  = (output or "").strip()
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
