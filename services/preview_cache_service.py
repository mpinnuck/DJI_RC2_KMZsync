from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from typing import Any

try:
    from PIL import Image
except ImportError:
    Image = None


class PreviewCacheService:
    """Preview cache utility and state holder extracted from SyncViewModel."""

    def __init__(self) -> None:
        self._preview_timestamp_cache: dict[str, str] = {}
        self._preview_timestamps_loaded = False

    @staticmethod
    def preview_name_candidates(guid: str) -> list[str]:
        return [f"{guid}.jpg", f"{guid}.jpeg", f"{guid}.png"]

    @classmethod
    def is_preview_name_for_guid(cls, guid: str, name: str) -> bool:
        lowered = (name or "").strip().lower()
        guid_lower = guid.strip().lower()
        return lowered in {candidate.lower() for candidate in cls.preview_name_candidates(guid_lower)}

    @classmethod
    def choose_preview_name_from_items(cls, guid: str, items: list[dict[str, Any]]) -> str | None:
        preferred = [".jpg", ".jpeg", ".png"]
        names = [
            str(item.get("Name") or "").strip()
            for item in items
            if not bool(item.get("IsFolder"))
        ]
        for suffix in preferred:
            for name in names:
                if name.lower() == f"{guid.lower()}{suffix}":
                    return name
        return None

    @staticmethod
    def choose_preview_folder_name(guid: str, names: list[str]) -> str | None:
        target = guid.strip().lower()
        for name in names:
            if str(name).strip().lower() == target:
                return str(name).strip()
        return None

    @staticmethod
    def find_case_insensitive_child_dir(parent: str, target_name: str) -> str | None:
        try:
            entries = os.scandir(parent)
        except OSError:
            return None

        target = target_name.strip().lower()
        with entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                if entry.name.strip().lower() == target:
                    return entry.path
        return None

    @staticmethod
    def preview_cache_path(root: str, guid: str) -> str:
        cache_root = os.path.join(tempfile.gettempdir(), "djirc2kmzsync-previews")
        os.makedirs(cache_root, exist_ok=True)
        root_hash = hashlib.sha1(root.encode("utf-8", errors="ignore")).hexdigest()[:12]
        return os.path.join(cache_root, f"{root_hash}-{guid}")

    @staticmethod
    def preview_timestamps_path(root: str) -> str:
        cache_root = os.path.join(tempfile.gettempdir(), "djirc2kmzsync-previews")
        root_hash = hashlib.sha1(root.encode("utf-8", errors="ignore")).hexdigest()[:12]
        return os.path.join(cache_root, f"{root_hash}-timestamps.json")

    def load_preview_timestamps(self, root: str) -> None:
        if self._preview_timestamps_loaded:
            return
        self._preview_timestamps_loaded = True
        path = self.preview_timestamps_path(root)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._preview_timestamp_cache = {str(k): str(v) for k, v in data.items()}
        except (OSError, json.JSONDecodeError):
            pass

    def reset_timestamp_state(self) -> None:
        self._preview_timestamp_cache.clear()
        self._preview_timestamps_loaded = False

    def save_preview_timestamps(self, root: str) -> None:
        path = self.preview_timestamps_path(root)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._preview_timestamp_cache, fh)
            os.replace(tmp, path)
        except OSError:
            pass

    def set_device_timestamp(self, guid: str, device_ts: str, root: str) -> None:
        if not device_ts:
            return
        self._preview_timestamp_cache[guid] = device_ts
        self.save_preview_timestamps(root)

    def clear_stale_preview_cache(self, root: str) -> None:
        self._preview_timestamp_cache.clear()
        self._preview_timestamps_loaded = False

        if not root:
            return

        try:
            os.remove(self.preview_timestamps_path(root))
        except OSError:
            pass

        cache_base = self.preview_cache_path(root, "")
        cache_root = os.path.dirname(cache_base)
        prefix = os.path.basename(cache_base).lower()

        try:
            names = os.listdir(cache_root)
        except OSError:
            return

        for name in names:
            if not name.lower().startswith(prefix):
                continue
            full_path = os.path.join(cache_root, name)
            if not os.path.isfile(full_path):
                continue
            try:
                os.remove(full_path)
            except OSError:
                pass

    def clear_preview_cache_for_guid(self, root: str, guid: str) -> None:
        if not root or not guid:
            return

        self._preview_timestamp_cache.pop(guid, None)
        self.save_preview_timestamps(root)

        cache_base = self.preview_cache_path(root, guid)
        for candidate in self.preview_name_candidates(guid):
            ext = os.path.splitext(candidate)[1].lower()
            cache_path = f"{cache_base}{ext}"
            if os.path.exists(cache_path):
                try:
                    os.remove(cache_path)
                except OSError:
                    pass

    @staticmethod
    def cache_temp_copy_path(cache_path: str) -> str:
        return f"{cache_path}.{uuid.uuid4().hex}.tmp"

    @staticmethod
    def promote_cache_copy(temp_path: str, cache_path: str) -> None:
        cache_base, _ = os.path.splitext(cache_path)
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

    @staticmethod
    def is_usable_preview_file(path: str) -> bool:
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

    @classmethod
    def find_usable_cached_preview(cls, cache_base: str, guid: str) -> str | None:
        for candidate in cls.preview_name_candidates(guid):
            ext = os.path.splitext(candidate)[1].lower()
            cache_path = f"{cache_base}{ext}"
            if cls.is_usable_preview_file(cache_path):
                return cache_path
            if os.path.exists(cache_path):
                try:
                    os.remove(cache_path)
                except OSError:
                    pass
        return None
