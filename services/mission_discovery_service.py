from __future__ import annotations

import os
import platform
from typing import Callable

from backends.backend_factory import BackendFactory
from model.rc2_mission import RC2Mission


class MissionDiscoveryService:
    """Mission listing and RC root discovery helpers."""

    def load_rc2_missions(
        self,
        *,
        root: str,
        list_missions: Callable[[str], tuple[list[RC2Mission], str | None]],
        set_last_error: Callable[[str], None],
    ) -> list[RC2Mission]:
        missions, err = list_missions(root)
        if err:
            set_last_error(err)
        return missions

    def diagnose_rc2_connection(
        self,
        *,
        root: str,
        get_status: Callable[[], tuple[bool, str]],
        detect_mtp_root: Callable[[], str | None],
        get_adb_status: Callable[[], tuple[bool, str]],
        probe_windows_devices: Callable[[], list[str]],
        default_adb_root: str,
    ) -> tuple[bool, str, str | None]:
        scheme = BackendFactory.path_scheme(root)
        is_windows = platform.system().lower().startswith("win")
        configured_status: str | None = None

        if root and scheme in {"adb", "mtp"}:
            ok, status = get_status()
            if ok:
                return True, f"Configured RC-2 root is reachable via {scheme.upper()}: {root}", root
            configured_status = status

        if root and BackendFactory.path_scheme(root) not in {"adb", "mtp"} and os.path.isdir(root):
            return True, f"RC-2 folder is reachable on disk: {root}", root

        # On macOS, if the user intentionally configured MTP, keep diagnostics
        # focused on that path and do not fall back to ADB checks.
        if (not is_windows) and scheme == "mtp":
            detail = configured_status or "Configured MTP root is not reachable."
            return False, (
                "RC-2 is not reachable on this macOS session via MTP. "
                "Keep RC-2 root set to mtp:... and reconnect the controller if needed. "
                f"MTP status: {detail}"
            ), None

        mtp_root = detect_mtp_root()
        if mtp_root:
            return True, f"RC-2 is reachable via Explorer-style MTP access. Use {mtp_root} as the RC-2 root.", mtp_root

        adb_ready, adb_message = get_adb_status()
        if adb_ready:
            return True, f"{adb_message}. Use {default_adb_root} as the RC-2 root.", default_adb_root

        if is_windows:
            present_devices = probe_windows_devices()
            if present_devices:
                device_list = ", ".join(present_devices[:3])
                return False, (
                    "Windows sees RC-2 related device entries but no mounted RC-2 folder is available and ADB is not ready. "
                    f"Present devices: {device_list}. If this is a VM, reattach the controller to Windows and enable USB debugging if you want to use ADB."
                ), None

            return False, (
                "RC-2 is not reachable from this Windows session via filesystem, Portable Devices, or ADB. "
                "If Windows is running in a VM, attach the controller to the VM first."
            ), None

        return False, (
            "RC-2 is not reachable on this macOS session. "
            "If you use an adb: path, enable USB debugging on RC-2, accept the host authorization prompt, "
            "and confirm adb reports state 'device'. "
            f"ADB status: {adb_message}"
        ), None

    def auto_detect_rc2_folder(
        self,
        *,
        current_root: str,
        diagnose: Callable[[], tuple[bool, str, str | None]],
        set_rc2_folder: Callable[[str], None],
    ) -> tuple[bool, str]:
        ok, message, detected_path = diagnose()
        if ok and detected_path and detected_path != current_root:
            set_rc2_folder(detected_path)
            return True, f"{message} RC-2 root updated to {detected_path}."
        return ok, message
