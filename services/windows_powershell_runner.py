import json
import os
import shutil
import subprocess
import threading
from typing import Any, Callable, List, Tuple


class WindowsPowerShellRunner:
    """Windows-only PowerShell/MTP helper operations."""

    DEFAULT_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        *,
        default_timeout_seconds: int | None = None,
        default_mtp_root: str,
    ):
        self._default_timeout_seconds = default_timeout_seconds or self.DEFAULT_TIMEOUT_SECONDS
        self._default_mtp_root = default_mtp_root
        self._lock = threading.Lock()

    @staticmethod
    def powershell_executable() -> str | None:
        return shutil.which("powershell") or shutil.which("pwsh")

    @classmethod
    def run_powershell(
        cls,
        script: str,
        timeout_seconds: int | None = None,
        *,
        powershell_executable: Callable[[], str | None] | None = None,
        default_timeout_seconds: int | None = None,
    ) -> Tuple[bool, str]:
        resolver = powershell_executable or cls.powershell_executable
        powershell = resolver()
        if not powershell:
            return False, "PowerShell executable not found."

        effective_timeout = timeout_seconds or default_timeout_seconds or cls.DEFAULT_TIMEOUT_SECONDS

        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            result = subprocess.run(
                [powershell, "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                check=False,
                timeout=effective_timeout,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except KeyboardInterrupt:
            return False, "PowerShell command was interrupted."
        except subprocess.TimeoutExpired:
            return False, f"PowerShell command timed out after {effective_timeout} seconds."
        except OSError:
            return False, "Failed to launch PowerShell."

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            return False, stderr or stdout or "PowerShell command failed."

        return True, stdout

    @classmethod
    def run_powershell_json(
        cls,
        script: str,
        timeout_seconds: int | None = None,
        *,
        run_powershell: Callable[[str, int | None], Tuple[bool, str]] | None = None,
        default_timeout_seconds: int | None = None,
    ) -> Tuple[bool, List[dict[str, Any]] | str]:
        if run_powershell is None:
            def _run_powershell(body: str, timeout: int | None) -> Tuple[bool, str]:
                return cls.run_powershell(
                    body,
                    timeout_seconds=timeout,
                    default_timeout_seconds=default_timeout_seconds,
                )

            run_powershell = _run_powershell

        ok, payload = run_powershell(script, timeout_seconds)
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

    @classmethod
    def mtp_segments(cls, path: str, default_mtp_root: str) -> List[str]:
        raw = path.strip()[4:].strip()
        if not raw:
            raw = default_mtp_root[4:]
        return [segment.strip() for segment in raw.split("|") if segment.strip()]

    @staticmethod
    def ps_single_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @classmethod
    def ps_array_literal(cls, values: List[str]) -> str:
        if not values:
            return "@()"
        quoted = ", ".join(cls.ps_single_quote(value) for value in values)
        return f"@({quoted})"

    @classmethod
    def build_mtp_script(cls, mtp_path: str, body: str, default_mtp_root: str) -> str:
        segments = cls.ps_array_literal(cls.mtp_segments(mtp_path, default_mtp_root))
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

    def run_script_json_threadsafe(
        self,
        script: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, List[dict[str, Any]] | str]:
        if os.name != "nt":
            return False, "PowerShell MTP operations are not supported on this platform."

        with self._lock:
            return self.run_powershell_json(
                script,
                timeout_seconds=timeout_seconds,
                default_timeout_seconds=self._default_timeout_seconds,
            )

    def detect_default_mtp_root(self, timeout_seconds: int = 10) -> str | None:
        if os.name != "nt":
            return None

        script = self.build_mtp_script(
            self._default_mtp_root,
            'Write-Output "OK"',
            self._default_mtp_root,
        )
        ok, _ = self.run_powershell(
            script,
            timeout_seconds=timeout_seconds,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        if ok:
            return self._default_mtp_root
        return None

    def probe_present_rc2_devices(self) -> List[str]:
        if os.name != "nt":
            return []

        script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$devices = Get-PnpDevice -PresentOnly |
    Where-Object {
        ($_.FriendlyName -match 'DJI|RC 2|ADB|MTP|Android') -or
        ($_.InstanceId -match 'VID_2CA3')
    } |
    Select-Object FriendlyName, Class, Status

if ($devices) {
    $devices | ConvertTo-Json -Compress
}
"""
        ok, payload = self.run_powershell_json(
            script,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        if not ok:
            return []

        records = payload if isinstance(payload, list) else []
        names: List[str] = []
        for record in records:
            friendly_name = str(record.get("FriendlyName") or "").strip()
            device_class = str(record.get("Class") or "").strip()
            status = str(record.get("Status") or "").strip()
            label = friendly_name or device_class or "Unknown device"
            if status:
                label = f"{label} [{status}]"
            names.append(label)
        return names
