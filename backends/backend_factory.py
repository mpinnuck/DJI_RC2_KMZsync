"""
backend_factory.py
------------------
Factory that constructs the correct RCBackend and PCBackend pair
based on the configured RC-2 path and the current platform.

Backend selection matrix:
    Path prefix   | Windows              | macOS
    --------------|----------------------|------------------
    mtp:...       | WindowsMTPBackend    | MacMTPBackend
    adb:...       | WindowsADBBackend    | MacADBBackend
    (no prefix)   | UnsupportedBackendError (RC-2 is Android, not a local path)

PCBackend is always a single concrete class -- Python stdlib handles
Windows/macOS filesystem differences transparently.
"""

from __future__ import annotations

import os

from backends.adb.mac_adb_backend import MacADBBackend
from backends.adb.windows_adb_backend import WindowsADBBackend
from backends.mtp.mac_mtp_backend import MacMTPBackend
from backends.mtp.windows_mtp_backend import WindowsMTPBackend
from backends.pc.pc_backend import PCBackend
from backends.rc_backend import RCBackend
from config.config_manager import ConfigManager


class UnsupportedBackendError(Exception):
    """Raised when the requested backend is not available on this platform."""


class BackendFactory:
    """
    Constructs the correct RCBackend for the given RC-2 path and platform.

    Usage:
        rc_backend = BackendFactory.create_rc(config.rc2_folder, config)
        pc_backend = BackendFactory.create_pc(config)
    """

    @staticmethod
    def create_rc(path: str, config: ConfigManager) -> RCBackend:
        """
        Return a concrete RCBackend for the given RC-2 root path.

        Raises UnsupportedBackendError if the path scheme is not supported
        on the current platform (e.g. mtp: on macOS).
        """
        cleaned = (path or "").strip()
        is_windows = os.name == "nt"

        if cleaned.lower().startswith("mtp:"):
            if not is_windows:
                return MacMTPBackend(config)
            return WindowsMTPBackend(config)

        if cleaned.lower().startswith("adb:"):
            if is_windows:
                return WindowsADBBackend(config)
            return MacADBBackend(config)

        if not cleaned:
            # No path configured yet — return a disconnected ADB backend so
            # connection checks (get_status, auto_detect) work without raising.
            if is_windows:
                return WindowsADBBackend(config)
            return MacADBBackend(config)

        # Non-empty path with no recognised protocol prefix.
        # RC-2 is an Android device; bare filesystem paths are not valid roots.
        raise UnsupportedBackendError(
            "RC-2 path must use the 'mtp:' prefix (Windows) or 'adb:' prefix. "
            "A bare filesystem path is not valid — RC-2 is an Android device. "
            f"Got: {cleaned!r}"
        )

    @staticmethod
    def create_pc(config: ConfigManager) -> PCBackend:
        """
        Return the PC-side filesystem backend.

        A single concrete class handles both Windows and macOS -- Python
        stdlib (os, shutil) abstracts platform path differences.
        """
        return PCBackend(config)

    @staticmethod
    def create_both(
        config: ConfigManager,
    ) -> tuple[RCBackend, PCBackend]:
        """
        Convenience method -- returns (rc_backend, pc_backend) in one call.
        """
        rc = BackendFactory.create_rc(config.rc2_folder, config)
        pc = BackendFactory.create_pc(config)
        return rc, pc
