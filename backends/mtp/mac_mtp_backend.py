"""
mac_mtp_backend.py
------------------
macOS RC-2 backend using native USB MTP via pymtp (libmtp).

Connects directly to the RC-2 over USB without requiring a mounted
filesystem or third-party bridge dylib.

Requirements:
    pip install pymtp>=0.0.6
    libmtp installed (brew install libmtp)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import List, Tuple

from backends.rc_backend import RCBackend
from config.config_manager import ConfigManager
from model.rc2_mission import RC2Mission

try:
    import pymtp as _pymtp
except Exception:  # pragma: no cover - handled at runtime
    _pymtp = None

if _pymtp is not None:
    # pymtp enables debug stack dumps by default, which can flood terminal output.
    _pymtp.__DEBUG__ = 0


_NON_MISSION_FOLDERS = frozenset({"capability", "map_preview"})
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _decode_native(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _walk_native_folder_tree(root_ptr):
    stack = []
    if root_ptr:
        stack.append(root_ptr)
    while stack:
        node_ptr = stack.pop()
        if not node_ptr:
            continue
        node = node_ptr.contents
        yield node
        if node.sibling:
            stack.append(node.sibling)
        if node.child:
            stack.append(node.child)


@contextmanager
def _suppress_native_stdio():
    """Temporarily silence native library writes to stdout/stderr."""
    try:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        saved_stdout_fd = os.dup(1)
        saved_stderr_fd = os.dup(2)
    except OSError:
        # Best effort only; if FD ops fail, continue without suppression.
        yield
        return

    try:
        os.dup2(devnull_fd, 1)
        os.dup2(devnull_fd, 2)
        yield
    finally:
        try:
            os.dup2(saved_stdout_fd, 1)
            os.dup2(saved_stderr_fd, 2)
        finally:
            for fd in (saved_stdout_fd, saved_stderr_fd, devnull_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass


class MacMTPBackend(RCBackend):
    """RC-2 backend for macOS using native USB MTP via pymtp (libmtp)."""

    DEFAULT_MTP_ROOT = (
        "mtp:DJI RC 2|Internal shared storage|Android|data"
        "|dji.go.v5|files|waypoint"
    )

    def __init__(self, config: ConfigManager) -> None:
        self._config = config
        self._pymtp_available: bool = _pymtp is not None
        # Keep one backend instance and one live device session for app lifetime.
        self._mtp = _pymtp.MTP() if _pymtp is not None else None
        self._connected = False
        self._connection_lock = threading.RLock()
        # Caches are per live connection and reset when the device reconnects.
        self._folder_cache: list | None = None
        self._file_cache: list | None = None

    # ------------------------------------------------------------------
    # Connection & mode
    # ------------------------------------------------------------------

    def is_connected(self, timeout_seconds: int | None = None) -> bool:
        if not self._pymtp_available:
            return False
        root = self._root()
        if not root:
            return False
        ok, _ = self._list_mtp_items(root)
        return ok

    def get_connection_mode(self) -> str:
        return "MTP"

    def probe_root(self, path: str) -> bool:
        ok, _ = self._list_mtp_items(path)
        return ok

    # ------------------------------------------------------------------
    # Mission listing
    # ------------------------------------------------------------------

    def list_missions(self, root: str) -> Tuple[List[RC2Mission], str | None]:
        if not self._pymtp_available:
            return [], self._unavailable_error()
        try:
            missions: List[RC2Mission] = []
            with self._native_connected():
                folder = self._native_resolve_folder(self._direct_waypoint_root(root))
                if folder is None:
                    return [], self._not_found_error(root)
                child_folders, _child_files = self._native_child_entries(int(folder.folder_id))
                for entry in child_folders:
                    guid = _decode_native(getattr(entry, "name", "")).strip()
                    if not guid or guid.lower() in _NON_MISSION_FOLDERS:
                        continue
                    if not _GUID_RE.match(guid):
                        continue
                    slot_folder = self._native_resolve_folder(
                        f"{self._direct_waypoint_root(root)}/{guid}"
                    )
                    kmz_name = ""
                    if slot_folder is not None:
                        _, child_files = self._native_child_entries(int(slot_folder.folder_id))
                        for file_entry in child_files:
                            candidate = _decode_native(getattr(file_entry, "filename", "")).strip()
                            if candidate.lower().endswith(".kmz"):
                                kmz_name = candidate
                                break
                    missions.append(
                        RC2Mission(
                            guid=guid,
                            kmz_name=kmz_name,
                            full_folder_path=self._mtp_join(root, guid),
                            last_modified="",
                        )
                    )
            missions.sort(key=lambda item: item.guid)
            return missions, None
        except Exception as exc:
            return [], f"[MacMTPBackend] Error listing missions: {self._format_exception(exc)}"

    # ------------------------------------------------------------------
    # Slot file operations
    # ------------------------------------------------------------------

    def list_slot_files(self, mission: RC2Mission) -> Tuple[bool, List[str] | str]:
        if not self._pymtp_available:
            return False, self._unavailable_error()
        try:
            with self._native_connected():
                folder = self._native_resolve_folder(mission.full_folder_path)
                if folder is None:
                    return False, self._not_found_error(mission.full_folder_path)
                _, child_files = self._native_child_entries(int(folder.folder_id))
                names = sorted(
                    _decode_native(getattr(item, "filename", "")).strip()
                    for item in child_files
                    if _decode_native(getattr(item, "filename", "")).strip()
                )
                return True, names
        except Exception as exc:
            return False, str(exc)

    def list_folder_items(
        self, path: str
    ) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        if not self._pymtp_available:
            return False, self._unavailable_error()
        try:
            with self._native_connected():
                folder = self._native_resolve_folder(path)
                if folder is None:
                    return False, self._not_found_error(path)
                child_folders, child_files = self._native_child_entries(int(folder.folder_id))
                rows: List[Tuple[str, bool, str]] = []
                for entry in child_folders:
                    name = _decode_native(getattr(entry, "name", "")).strip()
                    if name:
                        rows.append((name, True, ""))
                for entry in child_files:
                    filename = _decode_native(getattr(entry, "filename", "")).strip()
                    if not filename:
                        continue
                    modified = ""
                    try:
                        modified = datetime.fromtimestamp(
                            int(getattr(entry, "modificationdate", 0))
                        ).strftime("%d/%m/%Y %H:%M:%S")
                    except Exception:
                        pass
                    rows.append((filename, False, modified))
                rows.sort(key=lambda row: row[0].lower())
                return True, rows
        except Exception as exc:
            return False, str(exc)

    def read_file_bytes(
        self, mission: RC2Mission, filename: str
    ) -> Tuple[bool, bytes | str]:
        if not self._pymtp_available:
            return False, self._unavailable_error()
        try:
            with self._native_connected():
                file_entry = self._native_find_file(mission.full_folder_path, filename)
                if file_entry is None:
                    return False, f"MTP file not found: {filename}"
                fd, temp_path = tempfile.mkstemp(prefix="djirc2kmzsync-read-", suffix=".tmp")
                os.close(fd)
                try:
                    if not self._pull_file_to_path(int(file_entry.item_id), temp_path):
                        return False, f"MTP pull failed for {filename}"
                    with open(temp_path, "rb") as fh:
                        return True, fh.read()
                finally:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
        except Exception as exc:
            return False, str(exc)

    def read_file_bytes_from_path(
        self, folder: str, filename: str
    ) -> Tuple[bool, bytes | str]:
        if not self._pymtp_available:
            return False, self._unavailable_error()
        try:
            with self._native_connected():
                file_entry = self._native_find_file(folder, filename)
                if file_entry is None:
                    return False, f"MTP file not found: {filename}"
                fd, temp_path = tempfile.mkstemp(prefix="djirc2kmzsync-read-", suffix=".tmp")
                os.close(fd)
                try:
                    if not self._pull_file_to_path(int(file_entry.item_id), temp_path):
                        return False, f"MTP pull failed for {filename}"
                    with open(temp_path, "rb") as fh:
                        return True, fh.read()
                finally:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
        except Exception as exc:
            return False, str(exc)

    def delete_file(self, mission: RC2Mission, filename: str) -> Tuple[bool, str]:
        if not self._pymtp_available:
            return False, self._unavailable_error()
        try:
            with self._native_connected():
                file_entry = self._native_find_file(mission.full_folder_path, filename)
                if file_entry is None:
                    return False, f"File not found: {filename}"
                self._mtp_quiet_call(self._mtp.delete_object, int(file_entry.item_id))
            return True, f"Deleted {filename} from {mission.guid}"
        except Exception as exc:
            return False, f"MTP delete failed:\n{exc}"

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
        if not self._pymtp_available:
            return False, self._unavailable_error()
        try:
            with self._native_connected():
                folder = self._native_resolve_folder(dest_folder)
                if folder is None:
                    return False, self._not_found_error(dest_folder)
                metadata = _pymtp.LIBMTP_File(
                    filename=dest_filename,
                    filetype=self._mtp_quiet_call(self._mtp.find_filetype, local_source_path),
                    filesize=os.stat(local_source_path).st_size,
                )
                metadata.parent_id = int(folder.folder_id)
                metadata.storage_id = int(folder.storage_id)
                self._mtp_quiet_call(self._mtp.send_file_from_file, local_source_path, metadata)
            return True, f"Copied to {self._mtp_join(dest_folder, dest_filename)}"
        except Exception as exc:
            return False, f"MTP copy failed:\n{exc}"

    def write_text_file(
        self,
        dest_folder: str,
        filename: str,
        content: str,
    ) -> Tuple[bool, str]:
        if not self._pymtp_available:
            return False, self._unavailable_error()
        fd, temp_path = tempfile.mkstemp(prefix="djirc2kmzsync-txt-", suffix=".txt")
        os.close(fd)
        try:
            with open(temp_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            return self.copy_file_to_device(dest_folder, temp_path, filename)
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
        if not self._pymtp_available:
            return False, self._unavailable_error()
        try:
            with self._native_connected():
                file_entry = self._native_find_file(src_folder, filename)
                if file_entry is None:
                    return False, f"MTP file not found: {filename}"
                dest_dir = os.path.dirname(local_dest_path)
                if dest_dir:
                    os.makedirs(dest_dir, exist_ok=True)
                if os.path.exists(local_dest_path):
                    os.remove(local_dest_path)
                if not self._pull_file_to_path(int(file_entry.item_id), local_dest_path):
                    return False, f"MTP pull failed for {filename}"
            return True, local_dest_path
        except Exception as exc:
            return False, f"MTP pull failed:\n{exc}"

    # ------------------------------------------------------------------
    # Mission management
    # ------------------------------------------------------------------

    def create_slot_folder(self, root: str, guid: str) -> Tuple[bool, str]:
        if not self._pymtp_available:
            return False, self._unavailable_error()
        try:
            with self._native_connected():
                folder = self._native_resolve_folder(root)
                if folder is None:
                    return False, self._not_found_error(root)
                self._mtp_quiet_call(
                    self._mtp.create_folder,
                    guid,
                    parent=int(folder.folder_id),
                    storage=int(folder.storage_id),
                )
            return True, self._mtp_join(root, guid)
        except Exception as exc:
            return False, f"Failed to create MTP slot folder:\n{exc}"

    def delete_mission(self, mission: RC2Mission) -> Tuple[bool, str]:
        if not self._pymtp_available:
            return False, self._unavailable_error()
        try:
            with self._native_connected():
                folder = self._native_resolve_folder(mission.full_folder_path)
                if folder is None:
                    return False, self._not_found_error(mission.full_folder_path)
                self._native_delete_folder_recursive(int(folder.folder_id))
            return True, f"Deleted mission {mission.guid}"
        except Exception as exc:
            return False, f"MTP delete failed:\n{exc}"

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
        bulk = self.get_preview_paths_bulk(
            root,
            [guid],
            copy_timeout_seconds=copy_timeout_seconds,
            list_timeout_seconds=list_timeout_seconds,
        )
        return bulk.get(guid)

    def get_preview_paths_bulk(
        self,
        root: str,
        guids: list[str],
        copy_timeout_seconds: int | None = None,
        list_timeout_seconds: int | None = None,
    ) -> dict[str, str | None]:
        """Resolve preview images for many missions in one MTP connection."""
        result: dict[str, str | None] = {guid: None for guid in guids}
        if not self._pymtp_available or not guids:
            return result

        cache_root = os.path.join(tempfile.gettempdir(), "djirc2kmzsync-previews")
        os.makedirs(cache_root, exist_ok=True)

        pending_guids: list[str] = []
        for guid in guids:
            cached = self._cached_preview_path(cache_root, guid)
            if cached is not None:
                result[guid] = cached
                continue
            pending_guids.append(guid)

        if not pending_guids:
            return result

        # Fast-fail behavior: if we cannot refresh from device this cycle,
        # return existing cache entries immediately rather than blocking UI.
        def _fill_cache_fallback() -> dict[str, str | None]:
            for pending in pending_guids:
                cached_pending = self._cached_preview_path(cache_root, pending)
                if cached_pending is not None:
                    result[pending] = cached_pending
            return result

        download_plan: dict[str, tuple[int, str]] = {}

        try:
            with self._native_connected():
                waypoint_base = self._direct_waypoint_root(root)
                preview_root = self._native_resolve_folder(f"{waypoint_base}/map_preview")
                if preview_root is None:
                    return _fill_cache_fallback()

                for guid in pending_guids:
                    candidates = {
                        f"{guid.lower()}.jpg",
                        f"{guid.lower()}.jpeg",
                        f"{guid.lower()}.png",
                    }

                    guid_folder = self._native_resolve_folder(
                        f"{waypoint_base}/map_preview/{guid}"
                    )
                    if guid_folder is None:
                        continue

                    _, child_files = self._native_child_entries(int(guid_folder.folder_id))
                    for entry in child_files:
                        filename = _decode_native(getattr(entry, "filename", "")).strip()
                        if filename.lower() not in candidates:
                            continue

                        ext = os.path.splitext(filename)[1] or ".jpg"
                        download_plan[guid] = (int(entry.item_id), ext)
                        break
        except Exception:
            return _fill_cache_fallback()

        for guid, (item_id, ext) in download_plan.items():
            cache_path = os.path.join(cache_root, f"mac-mtp-{guid}{ext}")
            if self._pull_file_to_path_cli(item_id, cache_path):
                result[guid] = cache_path

        return result

    @staticmethod
    def _cached_preview_path(cache_root: str, guid: str) -> str | None:
        prefix = f"mac-mtp-{guid}".lower()
        try:
            names = os.listdir(cache_root)
        except OSError:
            return None

        for name in names:
            lowered = name.lower()
            if not lowered.startswith(prefix):
                continue
            if not (lowered.endswith(".jpg") or lowered.endswith(".jpeg") or lowered.endswith(".png")):
                continue

            candidate = os.path.join(cache_root, name)
            if not os.path.isfile(candidate):
                continue

            try:
                if os.path.getsize(candidate) > 0:
                    return candidate
            except OSError:
                continue
        return None

    def clear_preview_cache(self, root: str) -> None:
        return None

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_status(self) -> Tuple[bool, str]:
        if not self._pymtp_available:
            return False, "Native MTP unavailable: pymtp is not installed"
        root = self._root() or self.DEFAULT_MTP_ROOT
        ok, _ = self._list_mtp_items(root)
        if ok:
            return True, "MTP RC-2 waypoint path is reachable."
        return False, "MTP RC-2 waypoint path is not reachable. Ensure RC-2 is connected via USB."

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _root(self) -> str:
        return (self._config.rc2_folder or "").strip()

    def _mtp_quiet_call(self, method, *args, **kwargs):
        with _suppress_native_stdio():
            return method(*args, **kwargs)

    def _list_mtp_items(self, path: str) -> tuple[bool, list[dict[str, object]] | str]:
        """List items at an MTP path, mirroring Windows backend probe semantics."""
        if not self._pymtp_available:
            return False, self._unavailable_error()

        try:
            with self._native_connected():
                folder = self._native_resolve_folder(self._direct_waypoint_root(path))
                if folder is None:
                    return False, self._not_found_error(path)

                child_folders, child_files = self._native_child_entries(int(folder.folder_id))
                rows: list[dict[str, object]] = []

                for entry in child_folders:
                    name = _decode_native(getattr(entry, "name", "")).strip()
                    if not name:
                        continue
                    rows.append(
                        {
                            "Name": name,
                            "IsFolder": True,
                            "ModifyDate": "",
                            "ModifyDateDetail": "",
                        }
                    )

                for entry in child_files:
                    name = _decode_native(getattr(entry, "filename", "")).strip()
                    if not name:
                        continue
                    modified = ""
                    try:
                        modified = datetime.fromtimestamp(
                            int(getattr(entry, "modificationdate", 0))
                        ).strftime("%d/%m/%Y %H:%M:%S")
                    except Exception:
                        pass
                    rows.append(
                        {
                            "Name": name,
                            "IsFolder": False,
                            "ModifyDate": modified,
                            "ModifyDateDetail": modified,
                        }
                    )

                return True, rows
        except Exception as exc:
            return False, self._format_exception(exc)

    def close(self) -> None:
        """Close the persistent MTP session.

        Called on app shutdown or backend replacement."""
        with self._connection_lock:
            self._disconnect_locked()

    def _disconnect_locked(self) -> None:
        if self._mtp is None:
            self._connected = False
            self._folder_cache = None
            self._file_cache = None
            return
        try:
            self._mtp_quiet_call(self._mtp.disconnect)
        except Exception:
            pass
        self._connected = False
        self._folder_cache = None
        self._file_cache = None

    def _ensure_connected_locked(self) -> None:
        if self._mtp is None:
            raise RuntimeError("Native MTP backend unavailable")
        if self._connected:
            return

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                try:
                    self._mtp_quiet_call(self._mtp.detect_devices)
                except Exception:
                    pass
                self._mtp_quiet_call(self._mtp.connect)
                self._connected = True
                self._folder_cache = None
                self._file_cache = None
                return
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.2)

        self._connected = False
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Unable to connect to native MTP device")

    @staticmethod
    def _is_disconnect_error(exc: Exception) -> bool:
        text = (str(exc) or "").lower()
        name = type(exc).__name__.lower()
        return (
            "commandfailed" in name
            or "nodevice" in name
            or "notconnected" in name
            or "unable to initialize" in text
            or "open session" in text
            or "usb" in text
            or "connection" in text
        )

    def _ensure_session_healthy_locked(self) -> None:
        """Verify the persistent session is still alive; reconnect if stale."""
        if not self._connected or self._mtp is None:
            self._ensure_connected_locked()
            return

        try:
            # Lightweight sanity check; raises when device session is stale.
            # Avoid get_folder_list() because some pymtp builds still use
            # Python-2 has_key() internally.
            self._mtp_quiet_call(self._mtp.get_serialnumber)
        except Exception as exc:
            if self._is_disconnect_error(exc):
                self._disconnect_locked()
                self._ensure_connected_locked()
                return
            # Treat any health-check failure as stale session and rebuild.
            self._disconnect_locked()
            self._ensure_connected_locked()

    def _pull_file_to_path(self, item_id: int, local_dest_path: str) -> bool:
        """Pull a file by object id using pymtp, with mtp-getfile fallback."""
        try:
            self._mtp_quiet_call(self._mtp.get_file_to_file, int(item_id), local_dest_path)
            if os.path.isfile(local_dest_path) and os.path.getsize(local_dest_path) > 0:
                return True
        except Exception:
            pass

        return self._pull_file_to_path_cli(item_id, local_dest_path)

    def _pull_file_to_path_cli(self, item_id: int, local_dest_path: str) -> bool:
        """Pull a file by object id using mtp-getfile CLI."""

        mtp_getfile = shutil.which("mtp-getfile")
        if not mtp_getfile:
            return False

        try:
            result = subprocess.run(
                [mtp_getfile, str(int(item_id)), local_dest_path],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False

        if result.returncode != 0:
            return False
        return os.path.isfile(local_dest_path) and os.path.getsize(local_dest_path) > 0

    @contextmanager
    def _native_connected(self):
        with self._connection_lock:
            self._ensure_session_healthy_locked()
            try:
                yield
            except Exception as exc:
                # If the device disconnected mid-operation, drop session so the
                # next call performs a clean reconnect.
                if self._is_disconnect_error(exc):
                    self._disconnect_locked()
                raise
            finally:
                pass

    def _native_all_folders(self) -> list[object]:
        if self._folder_cache is not None:
            return self._folder_cache
        try:
            folders_map = self._mtp_quiet_call(self._mtp.get_folder_list)
            values = list(folders_map.values()) if hasattr(folders_map, "values") else list(folders_map)
        except Exception as exc:
            if self._is_disconnect_error(exc):
                self._disconnect_locked()
                self._ensure_connected_locked()
                folders_map = self._mtp_quiet_call(self._mtp.get_folder_list)
                values = list(folders_map.values()) if hasattr(folders_map, "values") else list(folders_map)
            else:
                with _suppress_native_stdio():
                    root_ptr = self._mtp.mtp.LIBMTP_Get_Folder_List(self._mtp.device)
                values = list(_walk_native_folder_tree(root_ptr))
        self._folder_cache = values
        return values

    def _native_all_files(self) -> list[object]:
        if self._file_cache is not None:
            return self._file_cache
        try:
            files = list(self._mtp_quiet_call(self._mtp.get_filelisting))
        except Exception as exc:
            if self._is_disconnect_error(exc):
                self._disconnect_locked()
                self._ensure_connected_locked()
                files = list(self._mtp_quiet_call(self._mtp.get_filelisting))
            else:
                raise
        self._file_cache = files
        return files

    def _native_root_folders(self) -> list[object]:
        return [
            folder
            for folder in self._native_all_folders()
            if int(getattr(folder, "parent_id", -1)) == 0
        ]

    def _native_resolve_folder_matches(self, path: str) -> list[object]:
        segments = self._mtp_segments(path)
        if not segments:
            return []

        all_folders = self._native_all_folders()
        current = self._native_root_folders()

        first_segment = segments[0].strip().lower()
        current = [
            folder
            for folder in current
            if _decode_native(getattr(folder, "name", "")).strip().lower() == first_segment
        ]
        if not current:
            return []

        for segment in segments[1:]:
            wanted = segment.strip().lower()
            next_level: list[object] = []
            for folder in current:
                folder_id = int(getattr(folder, "folder_id", -1))
                for child in all_folders:
                    if int(getattr(child, "parent_id", -1)) != folder_id:
                        continue
                    if _decode_native(getattr(child, "name", "")).strip().lower() == wanted:
                        next_level.append(child)
            if not next_level:
                break
            current = next_level

        return current

    def _native_resolve_folder(self, path: str):
        matches = self._native_resolve_folder_matches(path)
        return matches[0] if matches else None

    def _native_child_entries(self, parent_id: int) -> tuple[list[object], list[object]]:
        all_folders = self._native_all_folders()
        child_folders = [f for f in all_folders if int(getattr(f, "parent_id", -1)) == parent_id]
        all_files = self._native_all_files()
        child_files = [f for f in all_files if int(getattr(f, "parent_id", -1)) == parent_id]
        return child_folders, child_files

    def _native_find_file(self, folder_path: str, filename: str):
        folder = self._native_resolve_folder(folder_path)
        if folder is None:
            return None
        folder_id = int(getattr(folder, "folder_id", -1))
        _, child_files = self._native_child_entries(folder_id)

        wanted = filename.strip().lower()
        for entry in child_files:
            if _decode_native(getattr(entry, "filename", "")).strip().lower() == wanted:
                return entry
        return None

    def _native_delete_folder_recursive(self, folder_id: int) -> None:
        child_folders, child_files = self._native_child_entries(folder_id)
        for entry in child_files:
            self._mtp_quiet_call(self._mtp.delete_object, int(entry.item_id))
        for folder in child_folders:
            self._native_delete_folder_recursive(int(folder.folder_id))
            self._mtp_quiet_call(self._mtp.delete_object, int(folder.folder_id))

    def _dji_package_from_mtp(self, path: str) -> str:
        segments = self._mtp_segments(path)
        lowered = [segment.lower() for segment in segments]
        if "data" in lowered:
            idx = lowered.index("data")
            if idx + 1 < len(segments):
                candidate = segments[idx + 1].strip()
                if candidate:
                    return candidate
        return "dji.go.v5"

    def _direct_waypoint_root(self, path: str) -> str:
        package = self._dji_package_from_mtp(path)
        return f"Android/data/{package}/files/waypoint"

    @classmethod
    def _mtp_segments(cls, path: str) -> List[str]:
        raw = (path or "").strip()
        if raw.lower().startswith("mtp:"):
            raw = raw[4:].strip()
        if not raw:
            raw = cls.DEFAULT_MTP_ROOT[4:]
        raw = raw.replace("\\", "/")
        parts = [part for chunk in raw.split("|") for part in chunk.split("/")]
        return [segment.strip() for segment in parts if segment.strip()]

    @staticmethod
    def _mtp_join(path: str, name: str) -> str:
        prefix = (path or "").strip()
        sep = "" if prefix.endswith("|") else "|"
        return f"{prefix}{sep}{name}"

    def _unavailable_error(self) -> str:
        return "Native MTP unavailable: pymtp is not installed. Run: pip install pymtp"

    def _not_found_error(self, path: str) -> str:
        return (
            f"[MacMTPBackend] MTP path not found: {path}. "
            "Ensure RC-2 is connected and unlocked."
        )

    @staticmethod
    def _format_exception(exc: Exception) -> str:
        message = str(exc).strip() or repr(exc)
        return f"{exc.__class__.__name__}: {message}"
