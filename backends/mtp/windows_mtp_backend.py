"""
windows_mtp_backend.py
----------------------
Windows MTP backend -- implements the nine _raw_* primitives via
PowerShell Shell.Application COM.

Architecture rationale:
    Explorer and the Windows Shell already own the active MTP session.
    Injector-style tools using raw/libmtp open a competing session and
    fail -- especially in VM environments where MTP passthrough is partial.

    This backend uses Shell.Application COM, which operates through the
    same Shell MTP layer as Explorer. It works WITH the session Windows
    already has, never competing for exclusive access.

    Practical result: reliable behaviour in Parallels VM setups where
    all injector tools failed.

Silent delete:
    CopyHere with FOF_NOCONFIRMATION shows a "Replace or Skip" dialog on
    MTP targets -- WPD ignores that flag. The solution is to delete first
    via IFileOperation (PIDL path, confirmed silent on DJI RC-2), then
    CopyHere into an empty slot which raises no dialog.

Thread safety:
    All PowerShell/MTP calls serialised through _lock (threading.Lock).
    PowerShell launched with CREATE_NO_WINDOW / STARTF_USESHOWWINDOW.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from typing import Any, List, Tuple

from backends.rc_backend import (
    RCBackend,
    _mtp_join,
    _mtp_segments,
)
from config.config_manager import ConfigManager
from model.rc2_mission import RC2Mission
from backends.mtp.file_operation import is_available as ifo_is_available
from backends.mtp.file_operation import mtp_copy_silent
from services.mtp_date_normalizer import normalize_mtp_modify_date


_POWERSHELL_TIMEOUT = 30
_MTP_LIST_TIMEOUT   = 120
_MTP_COPY_TIMEOUT   = 30
_MTP_COPY_COMPLETION_TIMEOUT = 30

# ---------------------------------------------------------------------------
# C# IFileOperation silent delete via PIDL.
#
# SHCreateItemFromParsingName fails for WPD MTP paths (E_INVALIDARG).
# SHGetIDListFromObject on the Shell FolderItem COM object gives a PIDL;
# SHCreateItemFromIDList converts it to IShellItem for IFileOperation.
# IFileOperation.DeleteItem with FOF_SILENT|FOF_NOCONFIRMATION is silent --
# confirmed on DJI RC-2 (vid_2ca3, pid_1021).
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
    Windows MTP backend.

    Implements the nine _raw_* primitives using PowerShell Shell.Application.
    All orchestration logic lives in the RCBackend base class.
    """

    DEFAULT_ROOT = (
        "mtp:DJI RC 2|Internal shared storage|Android|data"
        "|dji.go.v5|files|waypoint"
    )

    def __init__(self, config: ConfigManager) -> None:
        super().__init__(config)
        self._lock = threading.Lock()

    # ==================================================================
    # _raw_* primitives
    # ==================================================================

    def _raw_list_folder(
        self, path: str
    ) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        script = self._mtp_script(
            path,
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
        ok, result = self._run_mtp_ps_json(
            script, timeout_seconds=_MTP_LIST_TIMEOUT
        )
        if not ok:
            return False, str(result)

        rows: List[Tuple[str, bool, str]] = []
        items = result if isinstance(result, list) else []
        for item in items:
            name = str(item.get("Name") or "").strip()
            if not name:
                continue
            modified = str(item.get("ModifyDateDetail") or "") or str(item.get("ModifyDate") or "")
            rows.append((name, bool(item.get("IsFolder")), modified))
        return True, rows

    def _raw_read_file(
        self, folder_path: str, filename: str, local_dest: str
    ) -> Tuple[bool, str]:
        # Stage-then-move: copy into a temp subdir, then move to local_dest.
        # This avoids partial writes at local_dest.
        script = self._mtp_script(
            folder_path,
            f"""
$filename = {_ps_quote(filename)}
$destPath = {_ps_quote(local_dest)}
$destDir  = Split-Path -Parent $destPath

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
$shell2 = New-Object -ComObject Shell.Application
$dest   = $shell2.Namespace($stageDir)
if (-not $dest) {{ throw "Local staging folder unavailable: $stageDir" }}

$dest.CopyHere($item, 0x614)

$copyDeadline = (Get-Date).AddSeconds(15)
$stagedPath   = Join-Path $stageDir $filename
do {{
    if (Test-Path -LiteralPath $stagedPath) {{ break }}
    [System.Threading.Thread]::Sleep(200)
}} while ((Get-Date) -lt $copyDeadline)

if (-not (Test-Path -LiteralPath $stagedPath)) {{
    throw "MTP copy did not complete for $filename"
}}

Move-Item -LiteralPath $stagedPath -Destination $destPath -Force
if (Test-Path -LiteralPath $stageDir) {{
    Remove-Item -LiteralPath $stageDir -Recurse -Force
}}
Write-Output $destPath
""",
        )
        ok, out = self._run_mtp_ps(script, timeout_seconds=_MTP_COPY_TIMEOUT)
        if not ok:
            return False, out
        return True, out.strip()

    def _raw_write_file(
        self, dest_folder: str, local_source: str, dest_filename: str
    ) -> Tuple[bool, str]:
        # Stage a renamed copy if source basename != dest_filename.
        # The legacy Shell.Application copy path remains the primary route
        # because it was the last known working implementation on this setup.
        source_to_copy = local_source
        temp_dir: str | None = None
        if os.path.basename(local_source) != dest_filename:
            temp_dir = tempfile.mkdtemp(prefix="djirc2kmzsync-stage-")
            source_to_copy = os.path.join(temp_dir, dest_filename)
            shutil.copy2(local_source, source_to_copy)

        try:
            ok, out = self._copy_to_mtp_folder(dest_folder, source_to_copy)
            if not ok and ifo_is_available():
                ok, out = mtp_copy_silent(local_source, dest_folder, dest_filename)
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

        return ok, out

    def get_file_size_from_path(
        self, folder: str, filename: str
    ) -> Tuple[bool, int | str]:
        script = self._mtp_script(
            folder,
            f"""
$filename = {_ps_quote(filename)}
$item = $current.Items() | Where-Object {{ $_.Name -eq $filename -and -not $_.IsFolder }} | Select-Object -First 1
if (-not $item) {{
    throw "MTP file not found: $filename"
}}

$size = [int64]0
try {{
    $size = [int64]$item.Size
}} catch {{
    throw "MTP size metadata unavailable for $filename"
}}

if ($size -lt 0) {{ $size = 0 }}
Write-Output $size
""",
        )
        ok, out = self._run_mtp_ps(script, timeout_seconds=_MTP_COPY_TIMEOUT)
        if not ok:
            return False, f"MTP size lookup failed:\n{out}"

        text = (out or "").strip()
        if not text:
            return False, "MTP size lookup failed:\nNo size returned."

        size_text = text.splitlines()[-1].strip().replace(",", "")
        try:
            size = int(size_text)
        except ValueError:
            return False, f"MTP size lookup returned invalid value:\n{text}"
        return True, max(size, 0)

    def _raw_delete_file(
        self, folder_path: str, filename: str
    ) -> Tuple[bool, str]:
        script = self._mtp_script(
            folder_path,
            _MTP_SILENT_DELETE_ADDTYPE
            + f"""
$filename = {_ps_quote(filename)}
$item = $current.Items() | Where-Object {{ $_.Name -eq $filename -and -not $_.IsFolder }} | Select-Object -First 1
if (-not $item) {{
    Write-Output "NOT_FOUND"
    return
}}

[MtpFileOp]::SilentDelete($item)

$deadline = (Get-Date).AddSeconds(8)
do {{
    [System.Threading.Thread]::Sleep(200)
    $current = $shell.Namespace(17)
    foreach ($seg in $segments) {{
        $current = ($current.Items() | Where-Object {{ $_.Name -eq $seg }} | Select-Object -First 1).GetFolder
    }}
    $remaining = $current.Items() | Where-Object {{ $_.Name -eq $filename -and -not $_.IsFolder }} | Select-Object -First 1
    if (-not $remaining) {{ break }}
}} while ((Get-Date) -lt $deadline)

$remaining = $current.Items() | Where-Object {{ $_.Name -eq $filename -and -not $_.IsFolder }} | Select-Object -First 1
if ($remaining) {{
    throw "MTP file delete did not complete for $filename"
}}
Write-Output "DELETED"
""",
        )
        ok, out = self._run_mtp_ps(script, timeout_seconds=_MTP_COPY_TIMEOUT)
        if not ok:
            return False, f"MTP delete failed:\n{out}"
        result = out.strip()
        if result == "NOT_FOUND":
            return True, "NOT_FOUND"
        return True, f"Deleted {filename}"

    def _raw_delete_folder(self, folder_path: str) -> Tuple[bool, str]:
        # Navigate to the *parent* of the target folder and delete by name.
        segments = _mtp_segments(folder_path)
        if not segments:
            return False, f"Cannot determine parent path for: {folder_path}"
        folder_name = segments[-1]
        parent_segments = segments[:-1]
        parent_path = "mtp:" + "|".join(parent_segments)

        script = self._mtp_script(
            parent_path,
            f"""
$folderName = {_ps_quote(folder_name)}
$item = $current.Items() | Where-Object {{ $_.Name -eq $folderName -and $_.IsFolder }} | Select-Object -First 1
if (-not $item) {{
    throw "Folder not found: $folderName"
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
    throw "Folder delete did not complete: $folderName"
}}
Write-Output $folderName
""",
        )
        ok, out = self._run_mtp_ps(script, timeout_seconds=_MTP_COPY_TIMEOUT)
        if not ok:
            return False, f"MTP folder delete failed:\n{out}"
        return True, f"Deleted {folder_name}"

    def _raw_create_folder(
        self, parent_path: str, name: str
    ) -> Tuple[bool, str]:
        script = self._mtp_script(
            parent_path,
            f"""
$folderName = {_ps_quote(name)}
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
    throw "MTP folder creation failed: $folderName"
}}
Write-Output $folderName
""",
        )
        ok, out = self._run_mtp_ps(script, timeout_seconds=_MTP_COPY_TIMEOUT)
        if not ok:
            return False, f"Failed to create MTP folder:\n{out}"
        return True, _mtp_join(parent_path, name)

    def _raw_probe(self, root: str) -> bool:
        ok, _ = self._raw_list_folder(root)
        return ok

    def _raw_get_status(self) -> Tuple[bool, str]:
        root = self._root() or self.DEFAULT_ROOT
        ok, _ = self._raw_list_folder(root)
        if ok:
            return True, "MTP RC-2 waypoint path is reachable."
        return False, (
            "MTP RC-2 waypoint path is not reachable. "
            "Ensure RC-2 is connected via USB."
        )

    def _raw_connection_mode(self) -> str:
        return "MTP"

    # ==================================================================
    # Bulk missions query -- single PowerShell process for all slots
    # ==================================================================

    def _raw_list_missions_bulk(
        self, root: str
    ) -> Tuple[bool, List[dict[str, Any]] | str]:
        """Single PS process walks all slots and their KMZ metadata."""
        script = self._mtp_script(
            root,
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
        Name             = [string]$slot.Name
        KMZName          = $kmzName
        ModifyDate       = $modifyDate
        ModifyDateDetail = $modifyDateDetail
    }
}
if ($result) { $result | ConvertTo-Json -Compress }
""",
        )
        ok, result = self._run_mtp_ps_json(script, timeout_seconds=_MTP_LIST_TIMEOUT)
        return ok, result

    # ==================================================================
    # PowerShell execution layer
    # ==================================================================

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

    # ==================================================================
    # MTP path / script helpers
    # ==================================================================

    @staticmethod
    def _mtp_script(mtp_path: str, body: str) -> str:
        segments = _ps_array(_mtp_segments(mtp_path))
        return f"""
$ErrorActionPreference = 'Stop'
$shell    = New-Object -ComObject Shell.Application
$current  = $shell.Namespace(17)
if (-not $current) {{ throw 'This PC shell namespace is unavailable.' }}
$segments = {segments}
foreach ($segment in $segments) {{
    $item = $current.Items() | Where-Object {{ $_.Name -eq $segment }} | Select-Object -First 1
    if (-not $item) {{ throw "MTP path segment not found: $segment" }}
    $current = $item.GetFolder
    if (-not $current) {{ throw "MTP path segment is not a folder: $segment" }}
}}
{body}
"""

    # ==================================================================
    # Internal MTP copy helpers
    # ==================================================================

    def _copy_to_mtp_folder(
        self, mtp_folder: str, local_source_path: str
    ) -> Tuple[bool, str]:
        # Step 1: silent IFileOperation delete if file exists (no dialog).
        # Step 2: CopyHere into empty slot (no collision = no dialog).
        src_ps = _ps_quote(local_source_path)
        body = (
            _MTP_SILENT_DELETE_ADDTYPE
            + f"""
$sourcePath = {src_ps}
if (-not (Test-Path -LiteralPath $sourcePath)) {{
    throw "Source file not found: $sourcePath"
}}
$sourceName = [System.IO.Path]::GetFileName($sourcePath)

# Step 1: silent delete of any existing file.
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

# Step 2: fresh navigation then CopyHere -- no collision, no dialog.
$current = $shell.Namespace(17)
foreach ($seg in $segments) {{
    $current = ($current.Items() | Where-Object {{ $_.Name -eq $seg }} | Select-Object -First 1).GetFolder
}}
$current.CopyHere($sourcePath, 0x4)

# Step 3: wait for the file to appear (CopyHere is async on MTP).
$copyDeadline = (Get-Date).AddSeconds({_MTP_COPY_COMPLETION_TIMEOUT})
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
        script = self._mtp_script(mtp_folder, body)
        return self._run_mtp_ps(script, timeout_seconds=_MTP_COPY_TIMEOUT)


# ---------------------------------------------------------------------------
# Module-level PS helpers
# ---------------------------------------------------------------------------

def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ps_array(values: List[str]) -> str:
    if not values:
        return "@()"
    return "@(" + ", ".join(_ps_quote(v) for v in values) + ")"
