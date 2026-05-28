from __future__ import annotations

import os
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

        if root and scheme in {"adb", "mtp"}:
            ok, status = get_status()
            if ok:
                return True, f"Configured RC-2 root is reachable via {scheme.upper()}: {root}", root

        if root and BackendFactory.path_scheme(root) not in {"adb", "mtp"} and os.path.isdir(root):
            return True, f"RC-2 folder is reachable on disk: {root}", root

        mtp_root = detect_mtp_root()
        if mtp_root:
            return True, f"RC-2 is reachable via Explorer-style MTP access. Use {mtp_root} as the RC-2 root.", mtp_root

        adb_ready, adb_message = get_adb_status()
        if adb_ready:
            return True, f"{adb_message}. Use {default_adb_root} as the RC-2 root.", default_adb_root

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
