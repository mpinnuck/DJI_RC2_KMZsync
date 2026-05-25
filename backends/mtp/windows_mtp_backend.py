"""
windows_mtp_backend.py
----------------------
Windows MTP backend using PowerShell Shell.Application COM.

Architecture rationale (from README):
    Explorer and the Windows Shell already own the active MTP session.
    Injector-style tools using raw/libmtp open a competing session and
    fail -- especially in VM environments where MTP passthrough is partial.

    This backend uses Shell.Application COM, which operates through the
    same Shell MTP layer as Explorer. It works WITH the session Windows
    already has, never competing for exclusive access.

    Practical result: reliable behaviour in Parallels VM setups where
    all injector tools failed.

Thread safety:
    All MTP COM calls are serialised through _mtp_operation_lock.
    PowerShell processes are launched with CREATE_NO_WINDOW and
    STARTF_USESHOWWINDOW to suppress console flicker.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime
from typing import Any, List, Tuple

try:
    from PIL import Image
except ImportError:
    Image = None

from backends.rc_backend import RCBackend
from config.config_manager import ConfigManager
from model.rc2_mission import RC2Mission


# Folders that exist in the waypoint root but are not mission slots.
_NON_MISSION_FOLDERS = frozenset({"capability", "map_preview"})

_POWERSHELL_TIMEOUT  = 30
_MTP_LIST_TIMEOUT    = 120
_MTP_COPY_TIMEOUT    = 30

# ---------------------------------------------------------------------------
# PowerShell C# helper: IFileOperation silent delete via PIDL.
#
# SHCreateItemFromParsingName fails for WPD MTP paths (E_INVALIDARG).
# Instead we obtain an IShellItem via PIDL using SHGetIDListFromObject on the
# Shell FolderItem COM object that PowerShell already holds, then convert with
# SHCreateItemFromIDList.  Delete via IFileOperation is silent (no Explorer
# confirmation dialog); copy is left to CopyHere which -- with no existing
# file present -- raises no collision dialog and writes data correctly.
#
# Using a single-quoted PS here-string (@'...'@) so C# double-quotes and
# any $ characters require no escaping.
# ---------------------------------------------------------------------------
_MTP_SILENT_DELETE_ADDTYPE = r"""
if (-not ([System.Management.Automation.PSTypeName]'MtpFileOp').Type) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IShellItem {
    void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
    void GetParent(out IShellItem ppsi);
    void GetDisplayName(uint sigdnName, [MarshalAs(UnmanagedType.LPWStr)] out string ppszName);
    void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
    void Compare(IShellItem psi, uint hint, out int piOrder);
}
[ComImport, Guid("947AAB5F-0A5C-4C13-B4D6-4BF7836FC9F8"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IFileOperation {
    void Advise(IntPtr pfops, out uint pdwCookie);
    void Unadvise(uint dwCookie);
    void SetOperationFlags(uint dwOperationFlags);
    void SetProgressMessage([MarshalAs(UnmanagedType.LPWStr)] string pszMessage);
    void SetProgressDialog(IntPtr popd);
    void SetProperties(IntPtr pproparray);
    void SetOwnerWindow(IntPtr hwndOwner);
    void ApplyPropertiesToItem(IShellItem psiItem);
    void ApplyPropertiesToItems(object punkItems);
    void RenameItem(IShellItem psiItem, [MarshalAs(UnmanagedType.LPWStr)] string pszNewName, IntPtr pfopsItem);
    void RenameItems(object pUnkItems, [MarshalAs(UnmanagedType.LPWStr)] string pszNewName);
    void MoveItem(IShellItem psiItem, IShellItem psiDestinationFolder, [MarshalAs(UnmanagedType.LPWStr)] string pszNewName, IntPtr pfopsItem);
    void MoveItems(object punkItems, IShellItem psiDestinationFolder);
    void CopyItem(IShellItem psiItem, IShellItem psiDestinationFolder, [MarshalAs(UnmanagedType.LPWStr)] string pszCopyName, IntPtr pfopsItem);
    void CopyItems(object punkItems, IShellItem psiDestinationFolder);
    void DeleteItem(IShellItem psiItem, IntPtr pfopsItem);
    void DeleteItems(object punkItems);
    void NewItem(IShellItem psiDestinationFolder, uint dwFileAttributes, [MarshalAs(UnmanagedType.LPWStr)] string pszName, [MarshalAs(UnmanagedType.LPWStr)] string pszTemplateName, IntPtr pfopsItem);
    void PerformOperations();
    void GetAnyOperationsAborted(out bool pfAnyOperationsAborted);
}
public static class MtpFileOp {
    private static readonly Guid CLSID_FileOperation = new Guid("3AD05575-8857-4850-9277-11B85BDB8E09");
    private static readonly Guid IID_IShellItem      = new Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE");
    [DllImport("shell32.dll", PreserveSig = true)]
    private static extern int SHGetIDListFromObject(
        [MarshalAs(UnmanagedType.IUnknown)] object punk, out IntPtr ppidl);
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    private static extern void SHCreateItemFromIDList(
        IntPtr pidl, ref Guid riid, [MarshalAs(UnmanagedType.Interface)] out IShellItem ppv);
    private static IShellItem ShellItemFromComObject(object comObj) {
        IntPtr pidl;
        int hr = SHGetIDListFromObject(comObj, out pidl);
        if (hr != 0) Marshal.ThrowExceptionForHR(hr);
        try {
            var iid = IID_IShellItem;
            IShellItem item;
            SHCreateItemFromIDList(pidl, ref iid, out item);
            return item;
        } finally { Marshal.FreeCoTaskMem(pidl); }
    }
    public static void SilentDelete(object existingFolderItem) {
        IShellItem itemToDelete = ShellItemFromComObject(existingFolderItem);
        var foType = Type.GetTypeFromCLSID(CLSID_FileOperation);
        var fo = (IFileOperation)Activator.CreateInstance(foType);
        fo.SetOperationFlags(0x0004 | 0x0010 | 0x0200 | 0x0400);
        fo.DeleteItem(itemToDelete, IntPtr.Zero);
        fo.PerformOperations();
    }
}
'@ -Language CSharp
}
"""


class WindowsMTPBackend(RCBackend):
    """
    RC-2 backend for Windows MTP access via PowerShell Shell.Application.

    Uses namespace 17 (This PC / Portable Devices) to traverse the MTP
    device tree and CopyHere/InvokeVerb for file transfer and deletion.
    All PowerShell processes are hidden and serialised through a lock.
    """

    DEFAULT_MTP_ROOT = (
        "mtp:DJI RC 2|Internal shared storage|Android|data"
        "|dji.go.v5|files|waypoint"
    )

    def __init__(self, config: ConfigManager) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._preview_items_cache: dict[str, List[dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Connection & mode
    # ------------------------------------------------------------------

    def is_connected(self, timeout_seconds: int | None = None) -> bool:
        root = self._root()
        if not root:
            return False
        ok, _ = self._list_mtp_items(root, timeout_seconds=timeout_seconds or 8)
        return ok

    def get_connection_mode(self) -> str:
        return "MTP"

    def probe_root(self, path: str) -> bool:
        ok, _ = self._list_mtp_items(path, timeout_seconds=10)
        return ok

    # ------------------------------------------------------------------
    # Mission listing
    # ------------------------------------------------------------------

    def list_missions(self, root: str) -> Tuple[List[RC2Mission], str | None]:
        # Prefer the bulk query (single PowerShell process for all slots)
        # to reduce per-slot process startup overhead.
        ok_bulk, bulk_result = self._list_missions_bulk(root)
        if ok_bulk:
            missions = []
            rows = bulk_result if isinstance(bulk_result, list) else []
            for row in rows:
                slot_name = str(row.get("Name") or "").strip()
                if not slot_name:
                    continue
                kmz_name = str(row.get("KMZName") or "").strip()
                last_modified = _normalize_mtp_date(
                    str(row.get("ModifyDateDetail") or "")
                    or str(row.get("ModifyDate") or "")
                )
                missions.append(RC2Mission(
                    guid=slot_name,
                    kmz_name=kmz_name,
                    full_folder_path=_mtp_join(root, slot_name),
                    last_modified=last_modified,
                ))
            return missions, None

        # Fallback: per-slot enumeration
        ok, result = self._list_mtp_items(root)
        if not ok:
            bulk_error = (
                bulk_result if isinstance(bulk_result, str) and bulk_result.strip()
                else ""
            )
            suffix = f" | Bulk query failed: {bulk_error}" if bulk_error else ""
            return [], f"[WindowsMTPBackend] Error listing missions: {result}{suffix}"

        items = result if isinstance(result, list) else []
        folders = [
            item for item in items
            if bool(item.get("IsFolder"))
            and str(item.get("Name") or "").strip().lower()
            not in _NON_MISSION_FOLDERS
        ]

        missions = []
        for item in sorted(folders, key=lambda v: str(v.get("Name") or "")):
            slot_name = str(item.get("Name") or "").strip()
            if not slot_name:
                continue
            remote_slot = _mtp_join(root, slot_name)
            ok_slot, slot_result = self._list_mtp_items(remote_slot)
            if not ok_slot:
                continue
            slot_items = slot_result if isinstance(slot_result, list) else []
            kmz_files = sorted(
                [
                    child for child in slot_items
                    if not bool(child.get("IsFolder"))
                    and str(child.get("Name") or "").strip().lower().endswith(".kmz")
                ],
                key=lambda c: str(c.get("Name") or ""),
            )
            kmz_name = str(kmz_files[0].get("Name") or "").strip() if kmz_files else ""
            last_modified = (
                _normalize_mtp_date(
                    str(kmz_files[0].get("ModifyDateDetail") or "")
                    or str(kmz_files[0].get("ModifyDate") or "")
                )
                if kmz_files else ""
            )
            missions.append(RC2Mission(
                guid=slot_name,
                kmz_name=kmz_name,
                full_folder_path=remote_slot,
                last_modified=last_modified,
            ))

        return missions, None

    # ------------------------------------------------------------------
    # Slot file operations
    # ------------------------------------------------------------------

    def list_slot_files(self, mission: RC2Mission) -> Tuple[bool, List[str] | str]:
        ok, result = self._list_mtp_items(mission.full_folder_path)
        if not ok:
            return False, str(result)
        items = result if isinstance(result, list) else []
        names = sorted(
            str(item.get("Name") or "").strip()
            for item in items
            if str(item.get("Name") or "").strip()
        )
        return True, names

    def list_folder_items(
        self, path: str
    ) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        ok, result = self._list_mtp_items(path)
        if not ok:
            return False, str(result)
        items = result if isinstance(result, list) else []
        output: List[Tuple[str, bool, str]] = []
        for item in items:
            name = str(item.get("Name") or "").strip()
            if not name:
                continue
            modified = _normalize_mtp_date(
                str(item.get("ModifyDateDetail") or "")
                or str(item.get("ModifyDate") or "")
            )
            output.append((name, bool(item.get("IsFolder")), modified))
        return True, output

    def read_file_bytes(
        self, mission: RC2Mission, filename: str
    ) -> Tuple[bool, bytes | str]:
        return self._read_bytes_from_mtp_folder(mission.full_folder_path, filename)

    def read_file_bytes_from_path(
        self, folder: str, filename: str
    ) -> Tuple[bool, bytes | str]:
        return self._read_bytes_from_mtp_folder(folder, filename)

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

        # CopyHere uses the source basename as the destination filename.
        # If the required dest_filename differs from the source basename,
        # stage a renamed copy in a temp directory first.
        source_to_copy = local_source_path
        temp_dir: str | None = None
        if os.path.basename(local_source_path) != dest_filename:
            temp_dir = tempfile.mkdtemp(prefix="djirc2kmzsync-")
            source_to_copy = os.path.join(temp_dir, dest_filename)
            shutil.copy2(local_source_path, source_to_copy)

        try:
            ok, out = self._copy_to_mtp_folder(dest_folder, source_to_copy)
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

        if not ok:
            return False, f"MTP copy failed:\n{out}"
        return True, f"Copied to {_mtp_join(dest_folder, dest_filename)}"

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
        temp_dir: str | None = None
        try:
            with open(temp_path, "w", encoding="utf-8") as fh:
                fh.write(content)

            staged = temp_path
            if os.path.basename(temp_path) != filename:
                temp_dir = tempfile.mkdtemp(prefix="djirc2kmzsync-txt-stage-")
                staged = os.path.join(temp_dir, filename)
                shutil.copy2(temp_path, staged)

            ok, out = self._copy_to_mtp_folder(dest_folder, staged)
            if not ok:
                return False, f"MTP write failed:\n{out}"
            return True, f"Wrote {filename} to {_mtp_join(dest_folder, filename)}"
        except OSError as exc:
            return False, f"File operation failed:\n{exc}"
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

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
        return self._copy_from_mtp_folder(
            src_folder, filename, local_dest_path,
            timeout_seconds=timeout_seconds or _MTP_COPY_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Mission management
    # ------------------------------------------------------------------

    def create_slot_folder(self, root: str, guid: str) -> Tuple[bool, str]:
        script = self._mtp_script(
            root,
            f"""
$folderName = {_ps_quote(guid)}
$existing = $current.Items() | Where-Object {{ $_.Name -eq $folderName -and $_.IsFolder }} | Select-Object -First 1
if (-not $existing) {{
    $current.NewFolder($folderName)
}}
$deadline = (Get-Date).AddSeconds(10)
do {{
    $existing = $current.Items() | Where-Object {{ $_.Name -eq $folderName -and $_.IsFolder }} | Select-Object -First 1
    if ($existing) {{ break }}
    [System.Threading.Thread]::Sleep(200)
}} while ((Get-Date) -lt $deadline)
if (-not $existing) {{
    throw "MTP slot folder creation failed: $folderName"
}}
Write-Output $folderName
""",
        )
        ok, out = self._run_ps(script, timeout_seconds=_MTP_COPY_TIMEOUT)
        if not ok:
            return False, f"Failed to create MTP slot folder:\n{out}"
        return True, _mtp_join(root, guid)

    def delete_mission(self, mission: RC2Mission) -> Tuple[bool, str]:
        root = self._root()
        script = self._mtp_script(
            root,
            f"""
$folderName = {_ps_quote(mission.guid)}
$item = $current.Items() | Where-Object {{ $_.Name -eq $folderName -and $_.IsFolder }} | Select-Object -First 1
if (-not $item) {{
    throw "Mission folder not found: $folderName"
}}
$item.InvokeVerb('delete')
$deadline = (Get-Date).AddSeconds(15)
do {{
    $remaining = $current.Items() | Where-Object {{ $_.Name -eq $folderName -and $_.IsFolder }} | Select-Object -First 1
    if (-not $remaining) {{ break }}
    [System.Threading.Thread]::Sleep(200)
}} while ((Get-Date) -lt $deadline)
$remaining = $current.Items() | Where-Object {{ $_.Name -eq $folderName -and $_.IsFolder }} | Select-Object -First 1
if ($remaining) {{
    throw "Mission folder delete did not complete: $folderName"
}}
Write-Output $folderName
""",
        )
        ok, out = self._run_ps(script, timeout_seconds=_MTP_COPY_TIMEOUT)
        if not ok:
            return False, f"MTP delete failed:\n{out}"
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

        preview_folder = _mtp_join(root, "map_preview")
        ok_items, item_result = self._list_mtp_items_cached(
            preview_folder, timeout_seconds=list_timeout_seconds
        )
        if not ok_items:
            return None

        items = item_result if isinstance(item_result, list) else []
        preview_name = _choose_preview_name(guid, items)
        source_folder = preview_folder

        # Try nested <guid> subfolder if not found flat.
        if not preview_name:
            folder_names = [
                str(item.get("Name") or "").strip()
                for item in items
                if bool(item.get("IsFolder"))
            ]
            nested = _choose_preview_folder(guid, folder_names)
            if nested:
                source_folder = _mtp_join(preview_folder, nested)
                ok_n, nested_result = self._list_mtp_items_cached(
                    source_folder, timeout_seconds=list_timeout_seconds
                )
                if ok_n:
                    preview_name = _choose_preview_name(
                        guid, nested_result if isinstance(nested_result, list) else []
                    )

        # Direct probe fallback for unreliable IsFolder metadata.
        if not preview_name:
            source_folder = _mtp_join(preview_folder, guid)
            ok_p, probe_result = self._list_mtp_items_cached(
                source_folder, timeout_seconds=list_timeout_seconds
            )
            if ok_p:
                preview_name = _choose_preview_name(
                    guid, probe_result if isinstance(probe_result, list) else []
                )

        if not preview_name:
            return None

        ext = os.path.splitext(preview_name)[1].lower() or ".jpg"
        cache_path = f"{cache_base}{ext}"
        temp_path = f"{cache_path}.{uuid.uuid4().hex}.tmp"

        ok, _ = self._copy_from_mtp_folder(
            source_folder, preview_name, temp_path,
            timeout_seconds=copy_timeout_seconds or _MTP_COPY_TIMEOUT,
        )
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
        root = self._root() or self.DEFAULT_MTP_ROOT
        ok, _ = self._list_mtp_items(root, timeout_seconds=10)
        if ok:
            return True, "MTP RC-2 waypoint path is reachable."
        return False, "MTP RC-2 waypoint path is not reachable. Ensure RC-2 is connected via USB."

    # ------------------------------------------------------------------
    # Internal -- PowerShell execution
    # ------------------------------------------------------------------

    @staticmethod
    def _ps_executable() -> str | None:
        return shutil.which("powershell") or shutil.which("pwsh")

    @classmethod
    def _run_ps(
        cls,
        script: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, str]:
        ps = cls._ps_executable()
        if not ps:
            return False, "PowerShell executable not found."

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            result = subprocess.run(
                [ps, "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds or _POWERSHELL_TIMEOUT,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except KeyboardInterrupt:
            return False, "PowerShell command was interrupted."
        except subprocess.TimeoutExpired:
            return False, (
                f"PowerShell command timed out after "
                f"{timeout_seconds or _POWERSHELL_TIMEOUT} seconds."
            )
        except OSError:
            return False, "Failed to launch PowerShell."

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode != 0:
            return False, stderr or stdout or "PowerShell command failed."
        return True, stdout

    @classmethod
    def _run_ps_json(
        cls,
        script: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, List[dict[str, Any]] | str]:
        ok, payload = cls._run_ps(script, timeout_seconds=timeout_seconds)
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

    def _run_mtp_ps(
        self, script: str, timeout_seconds: int | None = None
    ) -> Tuple[bool, str]:
        with self._lock:
            return self._run_ps(script, timeout_seconds=timeout_seconds)

    def _run_mtp_ps_json(
        self, script: str, timeout_seconds: int | None = None
    ) -> Tuple[bool, List[dict[str, Any]] | str]:
        with self._lock:
            return self._run_ps_json(script, timeout_seconds=timeout_seconds)

    # ------------------------------------------------------------------
    # Internal -- MTP path helpers
    # ------------------------------------------------------------------

    def _root(self) -> str:
        return (self._config.rc2_folder or "").strip()

    @staticmethod
    def _mtp_script(mtp_path: str, body: str) -> str:
        segments = _ps_array(_mtp_segments(mtp_path))
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

    # ------------------------------------------------------------------
    # Internal -- MTP item listing
    # ------------------------------------------------------------------

    def _list_mtp_items(
        self,
        mtp_path: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, List[dict[str, Any]] | str]:
        script = self._mtp_script(
            mtp_path,
            """
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
$items = @(
    $current.Items() | Select-Object Name,
        @{Name='IsFolder'; Expression={ [bool]$_.IsFolder }},
        @{Name='ModifyDate'; Expression={ [string]$_.ModifyDate }},
        @{Name='ModifyDateDetail'; Expression={
            if ($modifiedIndex -ne $null) { [string]$current.GetDetailsOf($_, $modifiedIndex) }
            else { '' }
        }}
)
if ($items) { $items | ConvertTo-Json -Compress }
""",
        )
        return self._run_mtp_ps_json(
            script,
            timeout_seconds=timeout_seconds or _MTP_LIST_TIMEOUT,
        )

    def _list_missions_bulk(
        self, mtp_path: str
    ) -> Tuple[bool, List[dict[str, Any]] | str]:
        """Single PowerShell process that walks all slots and their KMZ metadata."""
        script = self._mtp_script(
            mtp_path,
            """
$result = @()
$slotFolders = @(
    $current.Items() |
        Where-Object { $_.IsFolder -and $_.Name -ne 'capability' -and $_.Name -ne 'map_preview' } |
        Sort-Object Name
)
foreach ($slot in $slotFolders) {
    $slotFolder = $slot.GetFolder
    if (-not $slotFolder) { continue }
    $modifiedIndex = $null
    for ($i = 0; $i -lt 320; $i++) {
        $label = $slotFolder.GetDetailsOf($null, $i)
        if (-not $label) { continue }
        $normalized = ($label -as [string]).Trim().ToLowerInvariant()
        if ($normalized -in @('date modified','modified','modification date','date de modification')) {
            $modifiedIndex = $i; break
        }
    }
    $kmzItems = @(
        $slotFolder.Items() |
            Where-Object { (-not $_.IsFolder) -and ($_.Name -match '(?i)\\.kmz$') } |
            Sort-Object Name
    )
    $kmzName = ''; $modifyDate = ''; $modifyDateDetail = ''
    if ($kmzItems.Count -gt 0) {
        $first = $kmzItems[0]
        $kmzName = [string]$first.Name
        $modifyDate = [string]$first.ModifyDate
        if ($modifiedIndex -ne $null) {
            $modifyDateDetail = [string]$slotFolder.GetDetailsOf($first, $modifiedIndex)
        }
    }
    $result += [PSCustomObject]@{
        Name = [string]$slot.Name
        KMZName = $kmzName
        ModifyDate = $modifyDate
        ModifyDateDetail = $modifyDateDetail
    }
}
if ($result) { $result | ConvertTo-Json -Compress }
""",
        )
        return self._run_mtp_ps_json(
            script, timeout_seconds=_MTP_LIST_TIMEOUT
        )

    def _list_mtp_items_cached(
        self,
        mtp_path: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, List[dict[str, Any]] | str]:
        cached = self._preview_items_cache.get(mtp_path)
        if cached is not None:
            return True, cached
        ok, result = self._list_mtp_items(mtp_path, timeout_seconds=timeout_seconds)
        if ok:
            self._preview_items_cache[mtp_path] = result if isinstance(result, list) else []
        return ok, result

    def invalidate_cache(self) -> None:
        """Clear the MTP item listing cache. Called when rc2_folder changes."""
        self._preview_items_cache.clear()

    # ------------------------------------------------------------------
    # Internal -- MTP file copy helpers
    # ------------------------------------------------------------------

    def _copy_to_mtp_folder(
        self, mtp_folder: str, local_source_path: str
    ) -> Tuple[bool, str]:
        # Strategy: IFileOperation PIDL delete (silent, no Explorer dialog) then
        # CopyHere (no collision = no dialog, data written correctly).
        #
        # Background:
        #   - CopyHere with FOF_NOCONFIRMATION on an existing MTP file shows a
        #     "Replace or Skip" dialog regardless of the flag -- WPD ignores it.
        #   - IFileOperation.CopyItem via PIDL creates the file entry but writes
        #     0 bytes on DJI RC-2 (MTP data transfer does not complete).
        #   - SHCreateItemFromParsingName fails (E_INVALIDARG) for WPD paths.
        #   - IFileOperation.DeleteItem via PIDL is silent and confirmed working.
        #   - CopyHere with no existing file raises no dialog and writes data.
        src_ps = _ps_quote(local_source_path)
        body = (
            _MTP_SILENT_DELETE_ADDTYPE
            + f"""
$sourcePath = {src_ps}
if (-not (Test-Path -LiteralPath $sourcePath)) {{
    throw "Source file not found: $sourcePath"
}}
$sourceName = [System.IO.Path]::GetFileName($sourcePath)

# Step 1: silent delete of any existing file via IFileOperation (no dialog).
$existing = $current.Items() | Where-Object {{ -not $_.IsFolder -and $_.Name -eq $sourceName }} | Select-Object -First 1
if ($existing) {{
    [MtpFileOp]::SilentDelete($existing)
    $deleteDeadline = (Get-Date).AddSeconds(5)
    do {{
        [System.Threading.Thread]::Sleep(100)
        $current = $shell.Namespace(17)
        foreach ($seg in $segments) {{
            $current = ($current.Items() | Where-Object {{ $_.Name -eq $seg }} | Select-Object -First 1).GetFolder
        }}
        $remaining = $current.Items() | Where-Object {{ -not $_.IsFolder -and $_.Name -eq $sourceName }} | Select-Object -First 1
    }} while ($remaining -and (Get-Date) -lt $deleteDeadline)
}}

# Step 2: fresh navigation then CopyHere -- file is gone so no collision dialog.
$current = $shell.Namespace(17)
foreach ($seg in $segments) {{
    $current = ($current.Items() | Where-Object {{ $_.Name -eq $seg }} | Select-Object -First 1).GetFolder
}}
$current.CopyHere($sourcePath, 0x4)

# Step 3: wait for the file to appear (CopyHere is asynchronous on MTP).
$copyDeadline = (Get-Date).AddSeconds(30)
$copied = $null
do {{
    [System.Threading.Thread]::Sleep(200)
    $current = $shell.Namespace(17)
    foreach ($seg in $segments) {{
        $current = ($current.Items() | Where-Object {{ $_.Name -eq $seg }} | Select-Object -First 1).GetFolder
    }}
    $copied = $current.Items() | Where-Object {{ $_.Name -eq $sourceName -and -not $_.IsFolder }} | Select-Object -First 1
    if ($copied) {{ break }}
}} while ((Get-Date) -lt $copyDeadline)
if (-not $copied) {{
    throw "MTP copy did not complete for $sourceName"
}}
Write-Output $sourceName
"""
        )
        script = WindowsMTPBackend._mtp_script(mtp_folder, body)
        return self._run_mtp_ps(script, timeout_seconds=_MTP_COPY_TIMEOUT)

    def _copy_from_mtp_folder(
        self,
        mtp_folder: str,
        filename: str,
        local_dest_path: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, str]:
        # Uses stage-then-move to guarantee local_dest_path is never partial.
        script = self._mtp_script(
            mtp_folder,
            f"""
$filename = {_ps_quote(filename)}
$destPath = {_ps_quote(local_dest_path)}
$destDir = Split-Path -Parent $destPath
if (-not (Test-Path -LiteralPath $destDir)) {{
    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
}}
$item = $current.Items() | Where-Object {{ $_.Name -eq $filename }} | Select-Object -First 1
if (-not $item) {{
    throw "MTP file not found: $filename"
}}
if (Test-Path -LiteralPath $destPath) {{
    Remove-Item -LiteralPath $destPath -Force
}}
$stageDir = Join-Path $destDir ([Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $stageDir | Out-Null
$shell = New-Object -ComObject Shell.Application
$destination = $shell.Namespace($stageDir)
if (-not $destination) {{
    throw "Local staging folder unavailable: $stageDir"
}}
$destination.CopyHere($item, 0x614)
$copyDeadline = (Get-Date).AddSeconds(15)
$stagedPath = Join-Path $stageDir $filename
do {{
    if (Test-Path -LiteralPath $stagedPath) {{ break }}
    [System.Threading.Thread]::Sleep(200)
}} while ((Get-Date) -lt $copyDeadline)
if (-not (Test-Path -LiteralPath $stagedPath)) {{
    throw "MTP preview copy did not complete for $filename"
}}
Move-Item -LiteralPath $stagedPath -Destination $destPath -Force
if (Test-Path -LiteralPath $stageDir) {{
    Remove-Item -LiteralPath $stageDir -Recurse -Force
}}
Write-Output $destPath
""",
        )
        return self._run_mtp_ps(
            script, timeout_seconds=timeout_seconds or _MTP_COPY_TIMEOUT
        )

    def _read_bytes_from_mtp_folder(
        self, mtp_folder: str, filename: str
    ) -> Tuple[bool, bytes | str]:
        fd, temp_path = tempfile.mkstemp(
            prefix="djirc2kmzsync-read-", suffix=".tmp"
        )
        os.close(fd)
        try:
            ok, out = self._copy_from_mtp_folder(mtp_folder, filename, temp_path)
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
# Module-level helpers (pure functions, no class state)
# ---------------------------------------------------------------------------

def _mtp_segments(path: str) -> List[str]:
    raw = path.strip()
    if raw.lower().startswith("mtp:"):
        raw = raw[4:].strip()
    return [s.strip() for s in raw.split("|") if s.strip()]


def _mtp_join(path: str, name: str) -> str:
    prefix = path.strip()
    sep = "" if prefix.endswith("|") else "|"
    return f"{prefix}{sep}{name}"


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ps_array(values: List[str]) -> str:
    if not values:
        return "@()"
    return "@(" + ", ".join(_ps_quote(v) for v in values) + ")"


def _normalize_mtp_date(raw_value: str) -> str:
    raw = (raw_value or "").strip()
    if not raw:
        return ""
    # DJI firmware emits 12/30/1899 as a sentinel for unknown dates.
    if any(
        raw.startswith(prefix)
        for prefix in ("12/30/1899", "30/12/1899", "1899-12-30")
    ):
        return ""
    formats = [
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M:%S",
        "%d/%m/%Y %I:%M %p",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if parsed.year <= 1900:
            return ""
        return parsed.strftime("%d/%m/%Y %H:%M:%S")
    return raw


def _preview_cache_dir() -> str:
    cache_root = os.path.join(tempfile.gettempdir(), "djirc2kmzsync-previews")
    os.makedirs(cache_root, exist_ok=True)
    return cache_root


def _preview_cache_base(root: str, guid: str) -> str:
    root_hash = hashlib.sha1(root.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return os.path.join(_preview_cache_dir(), f"{root_hash}-{guid}")


def _find_cached_preview(cache_base: str, guid: str) -> str | None:
    for suffix in (".jpg", ".jpeg", ".png"):
        path = f"{cache_base}{suffix}"
        if _is_usable_preview(path):
            return path
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    return None


def _is_usable_preview(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        if os.path.getsize(path) <= 0:
            return False
    except OSError:
        return False
    if Image is not None:
        try:
            with Image.open(path) as img:
                img.verify()
        except Exception:
            return False
    return True


def _promote_preview(temp_path: str, cache_path: str) -> None:
    cache_base = os.path.splitext(cache_path)[0]
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


def _clear_preview_cache(root: str) -> None:
    cache_dir = _preview_cache_dir()
    root_hash = hashlib.sha1(root.encode("utf-8", errors="ignore")).hexdigest()[:12]
    prefix = root_hash + "-"
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return
    for name in names:
        if not name.startswith(prefix):
            continue
        full = os.path.join(cache_dir, name)
        if os.path.isfile(full):
            try:
                os.remove(full)
            except OSError:
                pass


def _choose_preview_name(guid: str, items: List[dict[str, Any]]) -> str | None:
    guid_lower = guid.strip().lower()
    names = [
        str(item.get("Name") or "").strip()
        for item in items
        if not bool(item.get("IsFolder"))
    ]
    for suffix in (".jpg", ".jpeg", ".png"):
        target = f"{guid_lower}{suffix}"
        for name in names:
            if name.lower() == target:
                return name
    return None


def _choose_preview_folder(guid: str, names: List[str]) -> str | None:
    target = guid.strip().lower()
    for name in names:
        if str(name).strip().lower() == target:
            return str(name).strip()
    return None
