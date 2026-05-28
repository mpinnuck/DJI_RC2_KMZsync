from __future__ import annotations

import json
import os
import platform
from datetime import datetime
from typing import Any

from config.config_manager import get_runtime_base_dir


class CopyMapService:
    """Persistence and query service for source->mission copy-map history."""

    COPY_MAP_FILE = "kmz_copy_map.json"
    COPY_MAP_FILE_MAC = "kmz_copy_map_m.json"
    COPY_MAP_FILE_ENV = "DJI_RC2_COPY_MAP_FILE"

    def __init__(self, copy_map_path: str | None = None):
        if copy_map_path:
            self._copy_map_path = copy_map_path
        else:
            base_dir = get_runtime_base_dir()
            self._copy_map_path = os.path.join(base_dir, self.default_copy_map_filename())
        self._ensure_copy_map_exists()

    @property
    def copy_map_path(self) -> str:
        return self._copy_map_path

    @copy_map_path.setter
    def copy_map_path(self, value: str) -> None:
        self._copy_map_path = value

    @classmethod
    def default_copy_map_filename(cls) -> str:
        override = os.environ.get(cls.COPY_MAP_FILE_ENV, "").strip()
        if override:
            return override
        if platform.system().lower() == "darwin":
            return cls.COPY_MAP_FILE_MAC
        return cls.COPY_MAP_FILE

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def default_payload() -> dict[str, Any]:
        return {
            "updated_at": "",
            "note": (
                "This map tracks file-level copy operations only. If RC-2 is opened with "
                "'adjust/open as new', DJI app metadata may create a new mission record that "
                "diverges from this mapping."
            ),
            "by_source": {},
        }

    def _ensure_copy_map_exists(self) -> None:
        if os.path.isfile(self._copy_map_path):
            return
        self._save_payload(self.default_payload())

    def ensure_copy_map_exists(self) -> None:
        self._ensure_copy_map_exists()

    def _load_payload(self) -> dict[str, Any]:
        if not os.path.isfile(self._copy_map_path):
            return self.default_payload()

        try:
            with open(self._copy_map_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                loaded.setdefault("updated_at", "")
                loaded.setdefault("note", "")
                loaded.setdefault("by_source", {})
                if not isinstance(loaded.get("by_source"), dict):
                    loaded["by_source"] = {}
                return loaded
        except (OSError, json.JSONDecodeError):
            pass

        return self.default_payload()

    def load_copy_map(self) -> dict[str, Any]:
        return self._load_payload()

    def _save_payload(self, payload: dict[str, Any]) -> None:
        try:
            with open(self._copy_map_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=4)
        except OSError:
            # Copy operations must remain successful even if map persistence fails.
            return

    def save_copy_map(self, payload: dict[str, Any]) -> None:
        self._save_payload(payload)

    def record_mapping(
        self,
        *,
        source_filename: str,
        source_full_path: str,
        target_mission_guid: str,
        target_kmz_filename: str,
        target_folder_path: str,
        connection_mode: str,
        copied_at: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        payload = self._load_payload()
        by_source = payload.get("by_source") if isinstance(payload.get("by_source"), dict) else {}
        payload["by_source"] = by_source

        source_key = source_filename
        entry = by_source.get(source_key)
        if not isinstance(entry, dict):
            entry = {"history": []}

        history = entry.get("history") if isinstance(entry.get("history"), list) else []
        row = {
            "copied_at": copied_at or self._now_iso(),
            "source_filename": source_filename,
            "source_full_path": source_full_path,
            "target_mission_guid": target_mission_guid,
            "target_kmz_filename": target_kmz_filename,
            "target_folder_path": target_folder_path,
            "connection_mode": connection_mode,
        }
        history.append(row)
        entry["history"] = history[-25:]
        entry["last"] = row
        by_source[source_key] = entry

        payload["updated_at"] = updated_at or self._now_iso()
        self._save_payload(payload)

    def get_summary(self) -> tuple[list[dict[str, str]], str, str]:
        payload = self._load_payload()
        by_source = payload.get("by_source") if isinstance(payload.get("by_source"), dict) else {}

        rows: list[dict[str, str]] = []
        for source_name, value in by_source.items():
            if not isinstance(value, dict):
                continue

            last = value.get("last") if isinstance(value.get("last"), dict) else None
            if not last:
                history = value.get("history") if isinstance(value.get("history"), list) else []
                if history:
                    candidate = history[-1]
                    if isinstance(candidate, dict):
                        last = candidate
            if not last:
                continue

            rows.append(
                {
                    "source_filename": str(last.get("source_filename") or source_name or ""),
                    "source_full_path": str(last.get("source_full_path") or ""),
                    "target_mission_guid": str(last.get("target_mission_guid") or ""),
                    "target_kmz_filename": str(last.get("target_kmz_filename") or ""),
                    "target_folder_path": str(last.get("target_folder_path") or ""),
                    "connection_mode": str(last.get("connection_mode") or ""),
                    "copied_at": str(last.get("copied_at") or ""),
                }
            )

        rows.sort(key=lambda row: row.get("copied_at") or "", reverse=True)
        updated_at = str(payload.get("updated_at") or "")
        note = str(payload.get("note") or "")
        return rows, updated_at, note
