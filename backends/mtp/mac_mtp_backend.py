"""
mac_mtp_backend.py
------------------
macOS MTP backend -- implements the nine _raw_* primitives via
pymtp (libmtp).

Requirements:
    brew install libmtp
    pip install pymtp>=0.0.6

All orchestration logic lives in RCBackend. This class only provides
the wire-level primitives that differ on macOS.

Thread safety:
    All pymtp calls are serialised through _connection_lock (RLock).
    pymtp is not thread-safe; one session is kept alive for the app
    lifetime and health-checked before each operation.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import List, Tuple

from backends.rc_backend import (
    RCBackend,
    _find_cached_preview,
    _is_usable_preview,
    _mtp_join,
    _mtp_segments,
    _preview_cache_base,
    _promote_preview,
)
from config.config_manager import ConfigManager
from model.rc2_mission import RC2Mission

# Suppress native libmtp debug output globally for this process.
os.environ.setdefault("LIBMTP_DEBUG", "0")

try:
    import pymtp as _pymtp
except Exception:
    _pymtp = None

if _pymtp is not None:
    # Silence pymtp debug output.
    _pymtp.__DEBUG__ = 0


def _decode(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


@contextmanager
def _suppress_stdio():
    """Suppress native library writes to stdout/stderr."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        devnull_fd        = os.open(os.devnull, os.O_WRONLY)
        saved_stdout_fd   = os.dup(1)
        saved_stderr_fd   = os.dup(2)
    except OSError:
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


def _walk_folder_tree(root_ptr):
    """Walk a native LIBMTP folder linked list (depth-first)."""
    stack = [root_ptr] if root_ptr else []
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


class MacMTPBackend(RCBackend):
    """
    macOS MTP backend using pymtp (libmtp).

    Implements the nine _raw_* primitives. All orchestration is in RCBackend.
    """

    DEFAULT_ROOT = (
        "mtp:DJI RC 2|Internal shared storage|Android|data"
        "|dji.go.v5|files|waypoint"
    )

    def __init__(self, config: ConfigManager) -> None:
        super().__init__(config)
        self._available: bool = _pymtp is not None
        self._mtp = _pymtp.MTP() if _pymtp is not None else None
        self._connected = False
        self._prefer_cli_pull = False
        self._connection_lock = threading.RLock()
        # Folder and file listing caches valid for the life of one connection.
        self._folder_cache: list | None = None
        self._file_cache: list | None = None

    # ==================================================================
    # _raw_* primitives
    # ==================================================================

    def _raw_list_folder(
        self, path: str
    ) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        if not self._available:
            return False, self._unavailable()
        try:
            with self._session():
                folder = self._resolve_folder(path)
                if folder is None:
                    return False, f"MTP path not found: {path}"
                child_folders, child_files = self._child_entries(int(folder.folder_id))
                rows: List[Tuple[str, bool, str]] = []
                for entry in child_folders:
                    name = _decode(getattr(entry, "name", "")).strip()
                    if name:
                        rows.append((name, True, ""))
                for entry in child_files:
                    name = _decode(getattr(entry, "filename", "")).strip()
                    if not name:
                        continue
                    modified = ""
                    try:
                        ts = int(getattr(entry, "modificationdate", 0))
                        if ts > 0:
                            modified = datetime.fromtimestamp(ts).strftime(
                                "%d/%m/%Y %H:%M:%S"
                            )
                    except Exception:
                        pass
                    rows.append((name, False, modified))
                rows.sort(key=lambda r: r[0].lower())
                return True, rows
        except Exception as exc:
            return False, self._fmt_exc(exc)

    def _raw_read_file(
        self, folder_path: str, filename: str, local_dest: str
    ) -> Tuple[bool, str]:
        if not self._available:
            return False, self._unavailable()
        try:
            with self._session():
                entry = self._find_file(folder_path, filename)
                if entry is None:
                    return False, f"MTP file not found: {filename}"
                if not self._pull_to_path(int(entry.item_id), local_dest):
                    return False, f"MTP pull failed for {filename}"
            return True, local_dest
        except Exception as exc:
            return False, f"MTP read failed:\n{self._fmt_exc(exc)}"

    def _raw_write_file(
        self, dest_folder: str, local_source: str, dest_filename: str
    ) -> Tuple[bool, str]:
        if not os.path.isfile(local_source):
            return False, f"Source file not found:\n{local_source}"
        if not self._available:
            return False, self._unavailable()

        try:
            with self._session():
                folder = self._resolve_folder(dest_folder)
                if folder is None:
                    return False, f"Destination folder not found: {dest_folder}"

                # Delete existing file silently before sending.
                existing = self._find_file_in_folder(
                    int(folder.folder_id), dest_filename
                )
                if existing is not None:
                    self._quiet(self._mtp.delete_object, int(existing.item_id))

                filename_bytes = dest_filename.encode("utf-8")
                filetype = self._quiet(self._mtp.find_filetype, local_source)
                filesize = os.stat(local_source).st_size
                metadata = _pymtp.LIBMTP_File(
                    filename=filename_bytes,
                    filetype=filetype,
                    filesize=filesize,
                )

                metadata.parent_id = int(folder.folder_id)
                metadata.storage_id = int(folder.storage_id)
                send_fn = self._mtp.mtp.LIBMTP_Send_File_From_File
                if hasattr(send_fn, "argtypes"):
                    send_fn.argtypes = [
                        ctypes.c_void_p,
                        ctypes.c_char_p,
                        ctypes.POINTER(_pymtp.LIBMTP_File),
                        ctypes.c_void_p,
                        ctypes.c_void_p,
                    ]
                if hasattr(send_fn, "restype"):
                    send_fn.restype = ctypes.c_int
                ret = self._quiet(
                    send_fn,
                    self._mtp.device,
                    local_source.encode("utf-8"),
                    ctypes.pointer(metadata),
                    None,
                    None,
                )
                if ret != 0:
                    self._quiet(self._mtp.debug_stack)
                    raise _pymtp.CommandFailed()
                # Invalidate caches -- file listing has changed.
                self._file_cache = None
            return True, f"Copied to {_mtp_join(dest_folder, dest_filename)}"
        except Exception as exc:
            return False, f"MTP write failed:\n{self._fmt_exc(exc)}"

    def get_file_size_from_path(
        self, folder: str, filename: str
    ) -> Tuple[bool, int | str]:
        if not self._available:
            return False, self._unavailable()
        try:
            with self._session():
                entry = self._find_file(folder, filename)
                if entry is None:
                    return False, f"MTP file not found: {filename}"
                size = int(getattr(entry, "filesize", 0))
                if size < 0:
                    size = 0
                return True, size
        except Exception as exc:
            return False, f"MTP size lookup failed:\n{self._fmt_exc(exc)}"

    def _raw_delete_file(
        self, folder_path: str, filename: str
    ) -> Tuple[bool, str]:
        if not self._available:
            return False, self._unavailable()
        try:
            with self._session():
                entry = self._find_file(folder_path, filename)
                if entry is None:
                    return True, "NOT_FOUND"
                self._quiet(self._mtp.delete_object, int(entry.item_id))
                self._file_cache = None
            return True, f"Deleted {filename}"
        except Exception as exc:
            return False, f"MTP delete failed:\n{self._fmt_exc(exc)}"

    def _raw_delete_folder(self, folder_path: str) -> Tuple[bool, str]:
        if not self._available:
            return False, self._unavailable()
        try:
            with self._session():
                folder = self._resolve_folder(folder_path)
                if folder is None:
                    return False, f"Folder not found: {folder_path}"
                self._delete_folder_recursive(int(folder.folder_id))
                self._folder_cache = None
                self._file_cache   = None
            segments = _mtp_segments(folder_path)
            name = segments[-1] if segments else folder_path
            return True, f"Deleted {name}"
        except Exception as exc:
            return False, f"MTP folder delete failed:\n{self._fmt_exc(exc)}"

    def _raw_create_folder(
        self, parent_path: str, name: str
    ) -> Tuple[bool, str]:
        if not self._available:
            return False, self._unavailable()
        try:
            with self._session():
                parent = self._resolve_folder(parent_path)
                if parent is None:
                    return False, f"Parent folder not found: {parent_path}"
                self._quiet(
                    self._mtp.create_folder,
                    name,
                    parent=int(parent.folder_id),
                    storage=int(parent.storage_id),
                )
                self._folder_cache = None
            return True, _mtp_join(parent_path, name)
        except Exception as exc:
            return False, f"MTP create folder failed:\n{self._fmt_exc(exc)}"

    def _raw_probe(self, root: str) -> bool:
        if not self._available:
            return False
        try:
            with self._session():
                # Explicitly verify a device is currently discoverable.
                devices = self._quiet(self._mtp.detect_devices)
                if not devices:
                    self._disconnect()
                    return False

                # Force a live folder read so disconnects are detected promptly.
                self._folder_cache = None
                # Keep probe lightweight: just confirm waypoint folder exists.
                return self._waypoint_folder_exists()
        except Exception:
            return False

    def _waypoint_folder_exists(self) -> bool:
        for folder in self._all_folders():
            name = _decode(getattr(folder, "name", "")).strip().lower()
            if name == "waypoint":
                return True
        return False

    def _raw_get_status(self) -> Tuple[bool, str]:
        if not self._available:
            return False, "Native MTP unavailable: install pymtp and libmtp."
        root = self._root() or self.DEFAULT_ROOT
        ok, _ = self._raw_list_folder(root)
        if ok:
            return True, "MTP RC-2 waypoint path is reachable."
        return False, (
            "MTP RC-2 waypoint path is not reachable. "
            "Ensure RC-2 is connected via USB and unlocked."
        )

    def _raw_connection_mode(self) -> str:
        return "MTP"

    def get_preview_paths_bulk(
        self,
        root: str,
        guids: list[str],
        copy_timeout_seconds: int | None = None,
        list_timeout_seconds: int | None = None,
    ) -> dict[str, str | None]:
        del copy_timeout_seconds, list_timeout_seconds  # not used by this backend path

        result: dict[str, str | None] = {}
        missing: list[str] = []
        for guid in guids:
            cache_base = _preview_cache_base(root, guid)
            cached = _find_cached_preview(cache_base, guid)
            result[guid] = cached
            if not cached:
                missing.append(guid)

        if not missing:
            return result

        preview_folder_path = _mtp_join(root, "map_preview")
        try:
            with self._session():
                preview_folder = self._resolve_folder(preview_folder_path)
                if preview_folder is None:
                    return result

                preview_folder_id = int(preview_folder.folder_id)
                child_folders, child_files = self._child_entries(preview_folder_id)

                top_files_by_name = {
                    _decode(getattr(entry, "filename", "")).strip().lower(): entry
                    for entry in child_files
                }
                subfolders_by_name = {
                    _decode(getattr(folder, "name", "")).strip().lower(): folder
                    for folder in child_folders
                }

                nested_files_by_guid: dict[str, dict[str, object]] = {}
                for guid in missing:
                    folder = subfolders_by_name.get(guid.strip().lower())
                    if folder is None:
                        continue
                    nested_id = int(getattr(folder, "folder_id", -1))
                    if nested_id < 0:
                        continue
                    _, nested_files = self._child_entries(nested_id)
                    nested_files_by_guid[guid] = {
                        _decode(getattr(entry, "filename", "")).strip().lower(): entry
                        for entry in nested_files
                    }

                selected_entries: dict[str, tuple[int, str]] = {}
                for guid in missing:
                    guid_lower = guid.strip().lower()
                    entry = None
                    selected_name = ""
                    for suffix in (".jpg", ".jpeg", ".png"):
                        candidate_name = f"{guid_lower}{suffix}"
                        top_match = top_files_by_name.get(candidate_name)
                        if top_match is not None:
                            entry = top_match
                            selected_name = candidate_name
                            break
                        nested_map = nested_files_by_guid.get(guid)
                        if nested_map is not None:
                            nested_match = nested_map.get(candidate_name)
                            if nested_match is not None:
                                entry = nested_match
                                selected_name = candidate_name
                                break

                    if entry is None:
                        result[guid] = None
                        continue

                    item_id = int(getattr(entry, "item_id", -1))
                    if item_id < 0:
                        result[guid] = None
                        continue

                    selected_entries[guid] = (item_id, selected_name)

                if not selected_entries:
                    return result

                unresolved: dict[str, tuple[int, str]] = {}

                # First pass: fast in-session pulls via pymtp, one call per preview.
                for guid, (item_id, selected_name) in selected_entries.items():
                    cache_base = _preview_cache_base(root, guid)
                    ext = os.path.splitext(selected_name)[1].lower() or ".jpg"
                    cache_path = f"{cache_base}{ext}"
                    temp_path = f"{cache_path}.{os.getpid()}.{guid}.tmp"
                    try:
                        direct_ok = False
                        if not self._prefer_cli_pull:
                            try:
                                self._quiet(self._mtp.get_file_to_file, int(item_id), temp_path)
                                direct_ok = _is_usable_preview(temp_path)
                            except Exception:
                                direct_ok = False

                        if direct_ok:
                            _promote_preview(temp_path, cache_path)
                            result[guid] = cache_path
                            continue

                        unresolved[guid] = (item_id, selected_name)
                    finally:
                        if os.path.exists(temp_path):
                            try:
                                os.remove(temp_path)
                            except OSError:
                                pass

                if not unresolved:
                    self._prefer_cli_pull = False
                    return result

                # Second pass: one disconnect, batched mtp-getfile pulls, one reconnect.
                mtp_getfile = shutil.which("mtp-getfile")
                if not mtp_getfile:
                    for guid in unresolved:
                        result[guid] = None
                    return result

                self._disconnect()
                any_cli_success = False
                try:
                    for guid, (item_id, selected_name) in unresolved.items():
                        cache_base = _preview_cache_base(root, guid)
                        ext = os.path.splitext(selected_name)[1].lower() or ".jpg"
                        cache_path = f"{cache_base}{ext}"
                        temp_path = f"{cache_path}.{os.getpid()}.{guid}.tmp"
                        try:
                            ok = self._pull_via_cli(item_id, temp_path)
                            if ok and _is_usable_preview(temp_path):
                                _promote_preview(temp_path, cache_path)
                                result[guid] = cache_path
                                any_cli_success = True
                            else:
                                result[guid] = None
                        finally:
                            if os.path.exists(temp_path):
                                try:
                                    os.remove(temp_path)
                                except OSError:
                                    pass
                finally:
                    try:
                        self._ensure_connected()
                    except Exception:
                        pass

                if any_cli_success:
                    self._prefer_cli_pull = True
        except Exception:
            # Preserve existing behavior: preview failures are non-fatal.
            return result

        return result

    # ==================================================================
    # Session management
    # ==================================================================

    def close(self) -> None:
        """Close the persistent MTP session (call on app shutdown)."""
        with self._connection_lock:
            self._disconnect()

    def _disconnect(self) -> None:
        if self._mtp is None:
            self._connected = False
            return
        try:
            self._quiet(self._mtp.disconnect)
        except Exception:
            pass
        self._connected   = False
        self._folder_cache = None
        self._file_cache   = None

    def _ensure_connected(self) -> None:
        if self._mtp is None:
            raise RuntimeError("Native MTP backend unavailable (pymtp not installed)")
        if self._connected:
            return
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                try:
                    self._quiet(self._mtp.detect_devices)
                except Exception:
                    pass
                self._quiet(self._mtp.connect)
                self._connected   = True
                self._folder_cache = None
                self._file_cache   = None
                return
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.2)
        self._connected = False
        raise last_exc or RuntimeError("Unable to connect to MTP device")

    def _ensure_healthy(self) -> None:
        """Verify session is alive; reconnect if stale."""
        if not self._connected or self._mtp is None:
            self._ensure_connected()
            return
        try:
            self._quiet(self._mtp.get_serialnumber)
        except Exception as exc:
            if self._is_disconnect_error(exc):
                self._disconnect()
                self._ensure_connected()
            else:
                # Keep the existing session for non-disconnect errors.
                raise

    @contextmanager
    def _session(self):
        """Acquire the connection lock, ensure session health, yield."""
        with self._connection_lock:
            self._ensure_healthy()
            try:
                yield
            except Exception as exc:
                if self._is_disconnect_error(exc):
                    self._disconnect()
                raise

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

    def _quiet(self, method, *args, **kwargs):
        with _suppress_stdio():
            return method(*args, **kwargs)

    # ==================================================================
    # Native device traversal
    # ==================================================================

    def _all_folders(self) -> list:
        if self._folder_cache is not None:
            return self._folder_cache
        try:
            folders_map = self._quiet(self._mtp.get_folder_list)
            values = (
                list(folders_map.values())
                if hasattr(folders_map, "values")
                else list(folders_map)
            )
        except Exception as exc:
            if self._is_disconnect_error(exc):
                self._disconnect()
                self._ensure_connected()
                folders_map = self._quiet(self._mtp.get_folder_list)
                values = (
                    list(folders_map.values())
                    if hasattr(folders_map, "values")
                    else list(folders_map)
                )
            else:
                # Fallback: walk native linked list directly.
                with _suppress_stdio():
                    root_ptr = self._mtp.mtp.LIBMTP_Get_Folder_List(
                        self._mtp.device
                    )
                values = list(_walk_folder_tree(root_ptr))
        self._folder_cache = values
        return values

    def _all_files(self) -> list:
        if self._file_cache is not None:
            return self._file_cache
        try:
            files = list(self._quiet(self._mtp.get_filelisting))
        except Exception as exc:
            if self._is_disconnect_error(exc):
                self._disconnect()
                self._ensure_connected()
                files = list(self._quiet(self._mtp.get_filelisting))
            else:
                raise
        self._file_cache = files
        return files

    def _root_folders(self) -> list:
        return [
            f for f in self._all_folders()
            if int(getattr(f, "parent_id", -1)) == 0
        ]

    def _child_entries(self, parent_id: int) -> tuple[list, list]:
        all_f = self._all_folders()
        all_fi = self._all_files()
        folders = [f for f in all_f  if int(getattr(f, "parent_id",  -1)) == parent_id]
        files   = [f for f in all_fi if int(getattr(f, "parent_id",  -1)) == parent_id]
        return folders, files

    def _resolve_folder(self, path: str):
        """
        Navigate to a folder by the pipe-separated MTP path segments,
        starting from the device root folders (parent_id == 0).
        Handles both mtp:DJI RC 2|...  and bare Android/data/... paths.
        """
        segments = self._path_segments(path)
        if not segments:
            return None

        all_folders = self._all_folders()
        # Start from device root (parent_id == 0).
        current = self._root_folders()

        for segment in segments:
            wanted = segment.strip().lower()
            next_level = []
            for folder in current:
                folder_id = int(getattr(folder, "folder_id", -1))
                for child in all_folders:
                    if int(getattr(child, "parent_id", -1)) != folder_id:
                        continue
                    if _decode(getattr(child, "name", "")).strip().lower() == wanted:
                        next_level.append(child)
            if not next_level:
                # Check current level itself for the first segment.
                matches = [
                    f for f in current
                    if _decode(getattr(f, "name", "")).strip().lower() == wanted
                ]
                if not matches:
                    return None
                current = matches
            else:
                current = next_level

        return current[0] if current else None

    def _find_file(self, folder_path: str, filename: str):
        folder = self._resolve_folder(folder_path)
        if folder is None:
            return None
        return self._find_file_in_folder(int(folder.folder_id), filename)

    def _find_file_in_folder(self, folder_id: int, filename: str):
        wanted = filename.strip().lower()
        _, child_files = self._child_entries(folder_id)
        for entry in child_files:
            if _decode(getattr(entry, "filename", "")).strip().lower() == wanted:
                return entry
        return None

    def _delete_folder_recursive(self, folder_id: int) -> None:
        child_folders, child_files = self._child_entries(folder_id)
        for entry in child_files:
            self._quiet(self._mtp.delete_object, int(entry.item_id))
        for folder in child_folders:
            self._delete_folder_recursive(int(folder.folder_id))
        self._quiet(self._mtp.delete_object, folder_id)

    # ==================================================================
    # File pull helpers
    # ==================================================================

    def _pull_to_path(self, item_id: int, local_dest: str) -> bool:
        """Pull by object id using pymtp, with mtp-getfile CLI fallback."""
        def _has_file(path: str) -> bool:
            return os.path.isfile(path) and os.path.getsize(path) > 0

        mtp_getfile = shutil.which("mtp-getfile")

        # If this session already proved pymtp pull is unreliable, skip the
        # expensive pymtp retry/disconnect cycle for every preview file.
        if self._prefer_cli_pull and mtp_getfile:
            self._disconnect()
            if self._pull_via_cli(item_id, local_dest):
                return True
            try:
                self._ensure_connected()
            except Exception:
                pass

        # First attempt: use the current session directly.
        try:
            self._quiet(self._mtp.get_file_to_file, int(item_id), local_dest)
            if _has_file(local_dest):
                self._prefer_cli_pull = False
                return True
        except Exception:
            pass

        # Second attempt: force session reset and retry once before fallback.
        try:
            self._disconnect()
            self._ensure_connected()
            self._quiet(self._mtp.get_file_to_file, int(item_id), local_dest)
            if _has_file(local_dest):
                self._prefer_cli_pull = False
                return True
        except Exception:
            pass

        # If mtp-getfile is not available, do not tear down the current
        # session just for a failed read attempt.
        if not mtp_getfile:
            return False

        # Some RC-2 / libmtp combinations fail reads via pymtp with CommandFailed
        # but succeed via mtp-getfile. Ensure pymtp releases its session before
        # invoking the CLI path to avoid competing device sessions.
        self._disconnect()
        if self._pull_via_cli(item_id, local_dest):
            self._prefer_cli_pull = True
            return True

        # Best-effort reconnection for subsequent operations in this process.
        try:
            self._ensure_connected()
        except Exception:
            pass
        return False

    @staticmethod
    def _pull_via_cli(item_id: int, local_dest: str) -> bool:
        """Pull using the mtp-getfile CLI tool (part of libmtp)."""
        mtp_getfile = shutil.which("mtp-getfile")
        if not mtp_getfile:
            return False
        try:
            result = subprocess.run(
                [mtp_getfile, str(int(item_id)), local_dest],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return (
            result.returncode == 0
            and os.path.isfile(local_dest)
            and os.path.getsize(local_dest) > 0
        )

    # ==================================================================
    # Path helpers
    # ==================================================================

    @staticmethod
    def _path_segments(path: str) -> List[str]:
        """
        Convert an mtp: or bare path to a list of folder name segments.

        mtp:DJI RC 2|Internal shared storage|Android|data|dji.go.v5|files|waypoint
        → ["DJI RC 2", "Internal shared storage", "Android", "data",
           "dji.go.v5", "files", "waypoint"]

        Android/data/dji.go.v5/files/waypoint
        → ["Android", "data", "dji.go.v5", "files", "waypoint"]
        """
        raw = (path or "").strip()
        if raw.lower().startswith("mtp:"):
            raw = raw[4:].strip()
        # Normalise: pipe and slash are both valid separators.
        raw = raw.replace("\\", "/")
        parts = []
        for chunk in raw.split("|"):
            for part in chunk.split("/"):
                s = part.strip()
                if s:
                    parts.append(s)

        # macOS libmtp folder traversal starts at storage roots (e.g. Android),
        # not at Explorer-style labels like "DJI RC 2|Internal shared storage".
        # If Android appears later in the path, trim everything before it.
        for idx, segment in enumerate(parts):
            if segment.lower() == "android":
                if idx > 0:
                    parts = parts[idx:]
                break
        return parts

    # ==================================================================
    # Helpers
    # ==================================================================

    def _unavailable(self) -> str:
        return (
            "Native MTP unavailable. "
            "Install pymtp (pip install pymtp) and libmtp (brew install libmtp)."
        )

    @staticmethod
    def _fmt_exc(exc: Exception) -> str:
        msg = str(exc).strip() or repr(exc)
        return f"{exc.__class__.__name__}: {msg}"
