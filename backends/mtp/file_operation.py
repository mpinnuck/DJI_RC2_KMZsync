"""
file_operation.py
-----------------
Silent MTP file copy using Windows IFileOperation COM interface.

WHY THIS EXISTS
---------------
Shell.Application.CopyHere raises a "Replace or Skip Files" collision dialog
on MTP targets when a file with the same name already exists -- regardless of
FOF_NOCONFIRMATION. The WPD layer ignores that flag for collision handling
because CopyHere goes through the legacy SHFileOperation copy engine.

Windows Explorer avoids this because it uses IFileOperation directly through
the WPD stack. IFileOperation with FOF_NOCONFIRMATION silently overwrites on
MTP -- the same code path Explorer uses for drag-and-drop.

HOW WE GET THE MTP DESTINATION IShellItem
------------------------------------------
IFileOperation.CopyItem requires an IShellItem for the destination folder.
For MTP targets, the shell parsing path is not a filesystem path -- it is an
opaque WPD object ID string of the form:

    ::{20D04FE0-...}\\\\?\\usb#vid_2ca3&...\\SID-{...}\\{obj-id}\\...\\{slot-id}

We cannot construct this from the human-readable MTP path. Instead we:

    1. Navigate Shell namespace 17 (This PC) segment by segment by display name
       (same as the existing PowerShell CopyHere code does)
    2. Call folder.Self.Path on the final folder COM object to get the WPD
       parsing path for that specific folder -- confirmed working on DJI RC-2
    3. Pass that parsing path to SHCreateItemFromParsingName to get a proper
       IShellItem usable by IFileOperation

This is confirmed by live output from the DJI RC-2:
    Waypoint folder path:
      ::{20D04FE0-...}\\...\\SID-{10001,,23140716544}\\{D1508523-...}\\...
    Slot path:
      <waypoint path>\\{54CED4C1-...}

DEPENDENCIES
------------
    comtypes >= 1.4   -- pip install comtypes
    Windows only      -- do not import on macOS/Linux

USAGE
-----
    from backends.mtp.file_operation import mtp_copy_silent, is_available

    if is_available():
        ok, msg = mtp_copy_silent(
            source_path=r"C:\\temp\\mission.kmz",
            dest_mtp_path="mtp:DJI RC 2|Internal shared storage|...|<GUID>",
            dest_filename="<GUID>.kmz",
        )
"""

from __future__ import annotations

import ctypes
import os
import shutil
import tempfile
from typing import Tuple


# ---------------------------------------------------------------------------
# IFileOperation flags
# ---------------------------------------------------------------------------
FOF_SILENT          = 0x0004   # No progress dialog
FOF_NOCONFIRMATION  = 0x0010   # Overwrite without prompting (honoured by IFileOperation on MTP)
FOF_NOERRORUI       = 0x0400   # No error dialogs
FOF_NOCONFIRMMKDIR  = 0x0200   # No "create folder" prompt

_SILENT_FLAGS = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR

# IFileOperation CLSID / IID
_CLSID_FileOperation = "{3AD05575-8857-4850-9277-11B85BDB8E09}"
_IID_IFileOperation  = "{947AAB5F-0A5C-4C13-B4D6-4BF7836FC9F8}"
_IID_IShellItem      = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def is_available() -> bool:
    """Return True if IFileOperation is usable (Windows + comtypes installed)."""
    if os.name != "nt":
        return False
    try:
        import comtypes  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def mtp_copy_silent(
    source_path: str,
    dest_mtp_path: str,
    dest_filename: str,
) -> Tuple[bool, str]:
    """
    Copy source_path into an MTP destination folder silently.

    Uses IFileOperation with FOF_NOCONFIRMATION which -- unlike CopyHere --
    is honoured by the WPD layer and silently overwrites existing files.

    Parameters
    ----------
    source_path    : absolute local path to the source file
    dest_mtp_path  : MTP path to the destination FOLDER (pipe-separated)
                     e.g. "mtp:DJI RC 2|...|waypoint|<GUID>"
    dest_filename  : filename to write at the destination

    Returns (True, message) on success, (False, message) on failure.
    """
    if os.name != "nt":
        return False, "IFileOperation is only available on Windows."

    if not os.path.isfile(source_path):
        return False, f"Source file not found:\n{source_path}"

    # Stage a renamed copy if source basename differs from dest_filename.
    # IFileOperation.CopyItem preserves the source filename; we rename via
    # staging rather than the newName parameter to keep the code simple.
    staged_path = source_path
    temp_dir: str | None = None
    if os.path.basename(source_path) != dest_filename:
        temp_dir = tempfile.mkdtemp(prefix="djirc2kmzsync-ifo-")
        staged_path = os.path.join(temp_dir, dest_filename)
        shutil.copy2(source_path, staged_path)

    try:
        return _run_ifileoperation(staged_path, dest_mtp_path)
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Core IFileOperation implementation
# ---------------------------------------------------------------------------

def _run_ifileoperation(
    source_path: str,
    dest_mtp_path: str,
) -> Tuple[bool, str]:
    """
    Perform the IFileOperation copy. source_path must already have the
    correct destination filename as its basename.
    """
    try:
        import comtypes
        import comtypes.client
    except ImportError:
        return False, (
            "comtypes is not installed. "
            "Run: pip install comtypes"
        )

    try:
        # ------------------------------------------------------------------
        # Step 1: Get the WPD shell parsing path for the MTP destination folder
        #         by navigating Shell namespace 17 by display name, then
        #         reading folder.Self.Path on the final segment.
        # ------------------------------------------------------------------
        dest_shell_path = _resolve_mtp_shell_path(dest_mtp_path)

        # ------------------------------------------------------------------
        # Step 2: Create IShellItem objects for source (local) and destination
        #         (MTP) using SHCreateItemFromParsingName.
        #
        #         For the source this is a normal filesystem path.
        #         For the destination this is the WPD parsing path obtained
        #         in step 1 -- confirmed working on DJI RC-2.
        # ------------------------------------------------------------------
        source_item = _shell_item_from_parsing_name(source_path)
        dest_item   = _shell_item_from_parsing_name(dest_shell_path)

        # ------------------------------------------------------------------
        # Step 3: Create IFileOperation, set silent flags, queue copy, execute.
        # ------------------------------------------------------------------
        fo  = comtypes.client.CreateObject(_CLSID_FileOperation)
        ifo = fo.QueryInterface(comtypes.GUID(_IID_IFileOperation))

        ifo.SetOperationFlags(_SILENT_FLAGS)

        # CopyItem(source, destFolder, newName=None, sink=None)
        # newName=None preserves the source filename -- dest_filename was
        # already baked into source_path by the staging step above.
        ifo.CopyItem(source_item, dest_item, None, None)

        hr = ifo.PerformOperations()
        if hr != 0:
            return False, (
                f"IFileOperation.PerformOperations returned "
                f"HRESULT 0x{hr & 0xFFFFFFFF:08X}"
            )

        return True, f"Copied to {dest_mtp_path}"

    except Exception as exc:  # noqa: BLE001
        return False, f"IFileOperation failed:\n{exc}"


def _resolve_mtp_shell_path(mtp_path: str) -> str:
    """
    Navigate Shell namespace 17 segment by segment using the display names
    from the mtp: path, then return folder.Self.Path for the final segment.

    This gives us the opaque WPD object ID path that SHCreateItemFromParsingName
    accepts -- e.g.:
        ::{20D04FE0-...}\\\\?\\usb#...\\SID-{...}\\{obj}\\...\\{slot-obj}

    Confirmed working against DJI RC-2 (vid_2ca3, pid_1021).
    """
    try:
        import comtypes.client
    except ImportError as exc:
        raise RuntimeError("comtypes not available") from exc

    segments = _mtp_path_to_segments(mtp_path)
    if not segments:
        raise ValueError(f"No path segments in MTP path: {mtp_path!r}")

    shell_app = comtypes.client.CreateObject("Shell.Application")
    folder    = shell_app.Namespace(17)   # This PC
    if not folder:
        raise RuntimeError("Shell namespace 17 (This PC) is unavailable.")

    for segment in segments:
        matched = None
        for candidate in folder.Items():
            if candidate.Name == segment:
                matched = candidate
                break
        if matched is None:
            raise RuntimeError(
                f"MTP path segment not found in Shell namespace: {segment!r}\n"
                f"Full MTP path: {mtp_path!r}"
            )
        folder = matched.GetFolder
        if folder is None:
            raise RuntimeError(
                f"MTP path segment is not a folder: {segment!r}"
            )

    # folder.Self.Path returns the WPD shell parsing path for this folder.
    # Confirmed on DJI RC-2: returns "::{20D04FE0-...}\\...\\{slot-obj-id}"
    try:
        shell_path = folder.Self.Path
    except Exception as exc:
        raise RuntimeError(
            f"folder.Self.Path failed for MTP destination.\n"
            f"MTP path: {mtp_path!r}\n"
            f"Error: {exc}"
        ) from exc

    if not shell_path:
        raise RuntimeError(
            f"folder.Self.Path returned empty string for MTP path: {mtp_path!r}"
        )

    return shell_path


def _shell_item_from_parsing_name(path: str):
    """
    Create an IShellItem from a parsing name (local path or WPD shell path)
    using SHCreateItemFromParsingName.

    Works for both:
      - Local filesystem paths: C:\\temp\\mission.kmz
      - WPD shell paths:        ::{20D04FE0-...}\\...\\{obj-id}
    """
    try:
        import comtypes
    except ImportError as exc:
        raise RuntimeError("comtypes not available") from exc

    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    iid     = comtypes.GUID(_IID_IShellItem)
    item    = ctypes.POINTER(comtypes.IUnknown)()

    hr = shell32.SHCreateItemFromParsingName(
        ctypes.c_wchar_p(path),
        None,
        ctypes.byref(iid),
        ctypes.byref(item),
    )

    if hr != 0:
        raise RuntimeError(
            f"SHCreateItemFromParsingName failed for path {path!r}: "
            f"HRESULT 0x{hr & 0xFFFFFFFF:08X}"
        )

    return item


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _mtp_path_to_segments(mtp_path: str) -> list[str]:
    """
    "mtp:DJI RC 2|Internal shared storage|...|<GUID>"
    → ["DJI RC 2", "Internal shared storage", ..., "<GUID>"]
    """
    raw = mtp_path.strip()
    if raw.lower().startswith("mtp:"):
        raw = raw[4:].strip()
    return [s.strip() for s in raw.split("|") if s.strip()]
