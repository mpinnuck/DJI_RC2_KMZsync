"""
rc_backend.py
-------------
Abstract base class defining the RC-2 device I/O contract.

All concrete backends (WindowsMTPBackend, WindowsADBBackend, MacADBBackend)
implement this interface. SyncViewModel depends only on this abstraction —
it never imports a concrete backend directly.

Thread safety:
    All methods must be safe to call from worker threads. Concrete backends
    are responsible for any internal locking they require (e.g. MTP COM
    serialisation).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Tuple

from model.kmz_file import KMZFile
from model.rc2_mission import RC2Mission


class RCBackend(ABC):
    """
    Abstract interface for all RC-2 device I/O operations.

    Concrete subclasses implement one of:
        - WindowsMTPBackend  -- PowerShell Shell.Application COM (Windows only)
        - WindowsADBBackend  -- ADB subprocess with Windows SDK paths
        - MacADBBackend      -- ADB subprocess with macOS Homebrew/SDK paths
    """

    # ------------------------------------------------------------------
    # Connection & mode
    # ------------------------------------------------------------------

    @abstractmethod
    def is_connected(self, timeout_seconds: int | None = None) -> bool:
        """
        Best-effort connectivity probe for the configured RC-2 root.

        Returns True if the device is reachable, False otherwise.
        Must not raise -- all errors should be caught and return False.
        """

    @abstractmethod
    def get_connection_mode(self) -> str:
        """
        Return the connection mode label for UI display.

        Returns one of: "MTP" | "ADB" | "Unavailable" | "Not Set"
        """

    @abstractmethod
    def probe_root(self, path: str) -> bool:
        """
        Probe whether the given path is a reachable RC-2 waypoint root.

        Used by BackendFactory and auto-detection to confirm a path is valid
        before committing it to config. Must not raise.
        """

    # ------------------------------------------------------------------
    # Mission listing
    # ------------------------------------------------------------------

    @abstractmethod
    def list_missions(self, root: str) -> Tuple[List[RC2Mission], str | None]:
        """
        Enumerate all mission slots under the given RC-2 waypoint root.

        Returns (missions, error_message).
        - missions: sorted list of RC2Mission objects (excludes capability,
          map_preview, and other non-slot folders).
        - error_message: non-None if any error occurred during enumeration;
          partial results may still be returned alongside an error.
        """

    # ------------------------------------------------------------------
    # Slot file operations
    # ------------------------------------------------------------------

    @abstractmethod
    def list_slot_files(self, mission: RC2Mission) -> Tuple[bool, List[str] | str]:
        """
        List all filenames inside a mission GUID slot folder.

        Returns (success, result) where result is either:
        - List[str]: sorted filenames on success
        - str: error message on failure
        """

    @abstractmethod
    def list_folder_items(
        self, path: str
    ) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        """
        List items in an arbitrary device folder.

        Returns (success, result) where result is either:
        - List[Tuple[str, bool, str]]: (name, is_folder, modified_display) on success
        - str: error message on failure

        Used by inspect_mission_storage for metadata probing.
        """

    @abstractmethod
    def read_file_bytes(
        self, mission: RC2Mission, filename: str
    ) -> Tuple[bool, bytes | str]:
        """
        Read a file from a mission GUID slot into memory.

        Returns (success, result) where result is either:
        - bytes: file contents on success
        - str: error message on failure
        """

    @abstractmethod
    def read_file_bytes_from_path(
        self, folder: str, filename: str
    ) -> Tuple[bool, bytes | str]:
        """
        Read a file from an arbitrary device folder path into memory.

        Returns (success, result) where result is either:
        - bytes: file contents on success
        - str: error message on failure

        Used by metadata inspection (deep inspect) to read JSON/XML/DB
        candidates from parent folders of the waypoint root.
        """

    @abstractmethod
    def delete_file(self, mission: RC2Mission, filename: str) -> Tuple[bool, str]:
        """
        Delete a single file from a mission GUID slot folder.

        Returns (success, message).
        """

    # ------------------------------------------------------------------
    # File transfer -- PC to RC-2
    # ------------------------------------------------------------------

    @abstractmethod
    def copy_file_to_device(
        self,
        dest_folder: str,
        local_source_path: str,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        """
        Copy a local file onto the RC-2, renaming it to dest_filename.

        The source file is read from local_source_path. The destination
        filename on the device is dest_filename (not the source basename).
        This rename-on-copy is required because DJI Fly expects the KMZ
        to match the existing slot filename (e.g. <UUID>.kmz).

        Returns (success, message).
        """

    @abstractmethod
    def write_text_file(
        self,
        dest_folder: str,
        filename: str,
        content: str,
    ) -> Tuple[bool, str]:
        """
        Write a UTF-8 text file to a device folder.

        Used for diagnostic write tests (write_waypoint_text_file).
        Returns (success, message).
        """

    # ------------------------------------------------------------------
    # File transfer -- RC-2 to PC
    # ------------------------------------------------------------------

    @abstractmethod
    def copy_file_from_device(
        self,
        src_folder: str,
        filename: str,
        local_dest_path: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, str]:
        """
        Copy a file from the RC-2 to a local destination path.

        Uses a stage -> move pattern to avoid partial writes at
        local_dest_path. The caller is responsible for ensuring the
        destination directory exists.

        Returns (success, message).
        """

    # ------------------------------------------------------------------
    # Mission management
    # ------------------------------------------------------------------

    @abstractmethod
    def create_slot_folder(self, root: str, guid: str) -> Tuple[bool, str]:
        """
        Create a new GUID slot folder under the RC-2 waypoint root.

        Polls for completion before returning. Returns (success, message).
        Used by _prepare_new_mission_target in the ViewModel.
        """

    @abstractmethod
    def delete_mission(self, mission: RC2Mission) -> Tuple[bool, str]:
        """
        Permanently delete a mission GUID slot folder and all its contents.

        This operation is irreversible. Callers must confirm with the user
        before invoking. Returns (success, message).
        """

    # ------------------------------------------------------------------
    # Preview images
    # ------------------------------------------------------------------

    @abstractmethod
    def get_preview_path(
        self,
        root: str,
        guid: str,
        copy_timeout_seconds: int | None = None,
        list_timeout_seconds: int | None = None,
        allow_live_fetch: bool = True,
    ) -> str | None:
        """
        Return a local filesystem path to the cached preview image for a
        mission GUID, fetching it from the device if not already cached.

        Preview images live at:
            <waypoint_root>/map_preview/<guid>/<guid>.jpg   (nested)
            <waypoint_root>/map_preview/<guid>.jpg          (flat)

        Returns None if no preview is available or the fetch fails.
        """

    @abstractmethod
    def clear_preview_cache(self, root: str) -> None:
        """
        Delete all locally cached preview images for the given RC-2 root.

        Called before each refresh so stale previews are not shown for
        slots that have been deleted or replaced on the device.
        """

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @abstractmethod
    def get_status(self) -> Tuple[bool, str]:
        """
        Return (ready, message) describing the current connection state.

        For ADB backends this wraps 'adb devices' output.
        For MTP backends this probes the default MTP path.
        Used by the ADB Status and Detect RC-2 UI actions.
        """
