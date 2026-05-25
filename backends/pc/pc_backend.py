"""
pc_backend.py
-------------
Concrete PC-side file access backend.

Handles all local filesystem operations for KMZ files stored on the
operator's PC or Mac. Uses Python stdlib (os, shutil, pathlib) which
abstracts Windows/macOS path differences transparently -- no platform
subclasses are needed here.
"""

from __future__ import annotations

import os
import shutil
from typing import List, Tuple

from config.config_manager import ConfigManager
from model.kmz_file import KMZFile


class PCBackend:
    """
    Local filesystem access for PC-side KMZ mission files.

    Scans the configured pc_folder recursively for .kmz files.
    All path handling is delegated to Python stdlib, which normalises
    Windows drive paths and POSIX paths transparently.
    """

    def __init__(self, config: ConfigManager) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # KMZ file listing
    # ------------------------------------------------------------------

    def list_kmz_files(self) -> Tuple[List[KMZFile], str | None]:
        """
        Recursively scan pc_folder for .kmz files.

        Returns (files, error_message).
        - files: sorted list of KMZFile objects with relative display paths
        - error_message: non-None if the scan failed
        """
        root = (self._config.pc_folder or "").strip()
        if not root or not os.path.isdir(root):
            return [], None

        files: List[KMZFile] = []
        error: str | None = None

        try:
            for folder_path, _dirs, filenames in sorted(os.walk(root)):
                for filename in sorted(filenames):
                    if not filename.lower().endswith(".kmz"):
                        continue
                    full_path = os.path.join(folder_path, filename)
                    rel_path = os.path.relpath(full_path, root)
                    files.append(KMZFile(filename=rel_path, full_path=full_path))
        except OSError as exc:
            error = f"[PCBackend] Error scanning PC folder: {exc}"

        return files, error

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def delete_file(self, kmz_file: KMZFile) -> Tuple[bool, str]:
        """
        Delete a KMZ file from the local filesystem.

        Returns (success, message).
        """
        pc_root = (self._config.pc_folder or "").strip()
        if not pc_root or not os.path.isdir(pc_root):
            return False, f"PC KMZ folder not found:\n{pc_root}"

        if not os.path.isfile(kmz_file.full_path):
            return False, f"KMZ file not found:\n{kmz_file.full_path}"

        try:
            os.remove(kmz_file.full_path)
        except OSError as exc:
            return False, f"File operation failed:\n{exc}"

        return True, f"Deleted KMZ file {kmz_file.filename}"

    def file_exists(self, path: str) -> bool:
        """Return True if the given absolute path exists as a file."""
        return os.path.isfile(path)

    def read_file_bytes(self, path: str) -> Tuple[bool, bytes | str]:
        """
        Read a local file into memory.

        Returns (success, result) where result is bytes or error string.
        """
        try:
            with open(path, "rb") as fh:
                return True, fh.read()
        except OSError as exc:
            return False, str(exc)

    def write_file(
        self, local_source_path: str, dest_path: str
    ) -> Tuple[bool, str]:
        """
        Copy a file to dest_path on the local filesystem.

        Overwrites any existing file at dest_path.
        Returns (success, message).
        """
        if not os.path.isfile(local_source_path):
            return False, f"Source file not found:\n{local_source_path}"

        dest_dir = os.path.dirname(dest_path)
        if dest_dir and not os.path.isdir(dest_dir):
            try:
                os.makedirs(dest_dir, exist_ok=True)
            except OSError as exc:
                return False, f"Failed to create destination directory:\n{exc}"

        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            shutil.copy2(local_source_path, dest_path)
        except OSError as exc:
            return False, f"File operation failed:\n{exc}"

        return True, f"Written to {dest_path}"

    def ensure_dir(self, path: str) -> Tuple[bool, str]:
        """
        Ensure a directory exists, creating it if necessary.

        Returns (success, message).
        """
        try:
            os.makedirs(path, exist_ok=True)
            return True, path
        except OSError as exc:
            return False, str(exc)
