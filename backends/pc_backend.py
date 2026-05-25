"""
pc_backend.py
-------------
Concrete backend for accessing KMZ files on the local PC or Mac.

Python's stdlib (os, shutil, pathlib) abstracts all OS differences,
so no platform subclasses are needed here.
"""

from __future__ import annotations

import os
import shutil
from typing import List, Tuple

from model.kmz_file import KMZFile


class PCBackend:
    """
    Local filesystem access for PC-side KMZ mission files.

    Recursively scans the configured PC folder and provides read,
    write, and delete operations.  All paths are plain filesystem paths.
    """

    def __init__(self) -> None:
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def get_last_error(self) -> str | None:
        return self._last_error

    def clear_last_error(self) -> None:
        self._last_error = None

    def _set_error(self, message: str) -> None:
        self._last_error = message

    # ------------------------------------------------------------------
    # KMZ file listing
    # ------------------------------------------------------------------

    def list_kmz_files(self, root: str) -> List[KMZFile]:
        """
        Recursively scan *root* for .kmz files.

        Returns a list of KMZFile objects sorted by relative path.
        """
        self._last_error = None
        files: List[KMZFile] = []

        if not root or not os.path.isdir(root):
            return files

        try:
            for folder_path, _, filenames in sorted(os.walk(root)):
                for filename in sorted(filenames):
                    if filename.lower().endswith(".kmz"):
                        full_path = os.path.join(folder_path, filename)
                        rel_path  = os.path.relpath(full_path, root)
                        files.append(KMZFile(filename=rel_path, full_path=full_path))
        except OSError as e:
            self._set_error(f"[PCBackend] Error scanning PC folder: {e}")

        return files

    # ------------------------------------------------------------------
    # File existence / metadata
    # ------------------------------------------------------------------

    def file_exists(self, path: str) -> bool:
        return os.path.isfile(path)

    def folder_exists(self, path: str) -> bool:
        return os.path.isdir(path)

    # ------------------------------------------------------------------
    # File read
    # ------------------------------------------------------------------

    def read_file_bytes(self, path: str) -> Tuple[bool, bytes | str]:
        """
        Read a local file into memory.

        Returns (True, bytes) or (False, error_message).
        """
        try:
            with open(path, "rb") as fh:
                return True, fh.read()
        except OSError as e:
            return False, f"Failed to read file:\n{e}"

    # ------------------------------------------------------------------
    # File write
    # ------------------------------------------------------------------

    def copy_file(self, src_path: str, dest_path: str) -> Tuple[bool, str]:
        """
        Copy *src_path* to *dest_path*, overwriting if present.

        Returns (True, message) or (False, error_message).
        """
        if not os.path.isfile(src_path):
            return False, f"Source file not found:\n{src_path}"

        dest_dir = os.path.dirname(dest_path)
        if dest_dir and not os.path.isdir(dest_dir):
            return False, f"Destination folder not found:\n{dest_dir}"

        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            shutil.copy2(src_path, dest_path)
        except OSError as e:
            return False, f"File copy failed:\n{e}"

        return True, f"Copied to {dest_path}"

    def write_text_file(self, path: str, content: str) -> Tuple[bool, str]:
        """
        Write a UTF-8 string to *path*, overwriting if present.

        Returns (True, message) or (False, error_message).
        """
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as e:
            return False, f"File write failed:\n{e}"

        return True, f"Written to {path}"

    # ------------------------------------------------------------------
    # File delete
    # ------------------------------------------------------------------

    def delete_file(self, path: str) -> Tuple[bool, str]:
        """
        Delete a local file.

        Returns (True, message) or (False, error_message).
        """
        if not os.path.isfile(path):
            return False, f"File not found:\n{path}"

        try:
            os.remove(path)
        except OSError as e:
            return False, f"Delete failed:\n{e}"

        return True, f"Deleted {os.path.basename(path)}"
