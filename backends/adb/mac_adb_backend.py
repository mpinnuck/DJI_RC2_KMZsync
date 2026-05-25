"""
mac_adb_backend.py
------------------
macOS ADB backend -- platform-specific executable discovery.

Searches for adb in standard Android SDK locations on macOS:
  - /opt/homebrew/bin (Apple Silicon Homebrew)
  - /usr/local/bin (Intel Homebrew)
  - ~/Library/Android/sdk/platform-tools (Android Studio)
  - $ANDROID_SDK_ROOT/platform-tools
  - $ANDROID_HOME/platform-tools

Install ADB on macOS with:
    brew install android-platform-tools
or via Android Studio SDK Manager.

macOS note:
    This backend is the only supported RC-2 access path on macOS.
    MTP is not supported (see README for rationale).
    USB debugging must be enabled on the RC-2 before ADB will connect.
"""

from __future__ import annotations

import os
from typing import List

from backends.adb.adb_backend import ADBBackend
from config.config_manager import ConfigManager


class MacADBBackend(ADBBackend):
    """ADB backend for macOS with Homebrew and Android Studio path discovery."""

    def __init__(self, config: ConfigManager) -> None:
        super().__init__(config)

    def _adb_search_paths(self) -> List[str]:
        exe = "adb"
        home = os.path.expanduser("~")
        roots = [
            os.environ.get("ANDROID_SDK_ROOT", ""),
            os.environ.get("ANDROID_HOME", ""),
            os.path.join(home, "Library", "Android", "sdk"),
            "/opt/homebrew",        # Apple Silicon Homebrew
            "/usr/local",           # Intel Homebrew
        ]
        candidates: List[str] = []
        for root in roots:
            cleaned = (root or "").strip()
            if not cleaned:
                continue
            candidates.append(os.path.join(cleaned, "platform-tools", exe))
            candidates.append(os.path.join(cleaned, "bin", exe))
            candidates.append(os.path.join(cleaned, exe))
        return candidates
