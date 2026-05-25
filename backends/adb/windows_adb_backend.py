"""
windows_adb_backend.py
----------------------
Windows ADB backend -- platform-specific executable discovery.

Searches for adb.exe in standard Android SDK locations on Windows:
  - %LOCALAPPDATA%\\Android\\Sdk\\platform-tools
  - %USERPROFILE%\\AppData\\Local\\Android\\Sdk\\platform-tools
  - %ANDROID_SDK_ROOT%\\platform-tools
  - %ANDROID_HOME%\\platform-tools

Install ADB on Windows with:
    winget install --id Google.PlatformTools --exact
"""

from __future__ import annotations

import os
from typing import List

from backends.adb.adb_backend import ADBBackend
from config.config_manager import ConfigManager


class WindowsADBBackend(ADBBackend):
    """ADB backend for Windows with Windows-specific SDK path discovery."""

    def __init__(self, config: ConfigManager) -> None:
        super().__init__(config)

    def _adb_search_paths(self) -> List[str]:
        exe = "adb.exe"
        roots = [
            os.environ.get("ANDROID_SDK_ROOT", ""),
            os.environ.get("ANDROID_HOME", ""),
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk"
            ),
            os.path.join(
                os.environ.get("USERPROFILE", ""),
                "AppData", "Local", "Android", "Sdk",
            ),
        ]
        candidates: List[str] = []
        for root in roots:
            cleaned = (root or "").strip().strip('"')
            if not cleaned:
                continue
            candidates.append(os.path.join(cleaned, "platform-tools", exe))
            candidates.append(os.path.join(cleaned, exe))
        return candidates
