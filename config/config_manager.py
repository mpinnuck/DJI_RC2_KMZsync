import json
import logging
import os
import sys

_logger = logging.getLogger(__name__)

CONFIG_FILE = "kmz_sync_config.json"

_DEFAULTS = {
    "rc2_folder": "",
    "pc_folder": "",
    "rc2_refresh_retry_interval_seconds": 5,
    "dummy_slot_guid": "",
}

_MIN_RETRY_SECONDS = 1
_MAX_RETRY_SECONDS = 300


def get_runtime_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.getcwd()


def get_config_file_path() -> str:
    return os.path.join(get_runtime_base_dir(), CONFIG_FILE)


class ConfigManager:
    def __init__(self):
        self._config: dict = dict(_DEFAULTS)
        self._load()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------
    @property
    def rc2_folder(self) -> str:
        return self._config.get("rc2_folder", "")

    @rc2_folder.setter
    def rc2_folder(self, value: str) -> None:
        self._config["rc2_folder"] = value

    @property
    def pc_folder(self) -> str:
        return self._config.get("pc_folder", "")

    @pc_folder.setter
    def pc_folder(self, value: str) -> None:
        self._config["pc_folder"] = value

    @property
    def rc2_refresh_retry_interval_seconds(self) -> int:
        raw_value = self._config.get(
            "rc2_refresh_retry_interval_seconds",
            _DEFAULTS["rc2_refresh_retry_interval_seconds"],
        )

        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            return _DEFAULTS["rc2_refresh_retry_interval_seconds"]

        if parsed < _MIN_RETRY_SECONDS:
            return _MIN_RETRY_SECONDS
        if parsed > _MAX_RETRY_SECONDS:
            return _MAX_RETRY_SECONDS
        return parsed

    @rc2_refresh_retry_interval_seconds.setter
    def rc2_refresh_retry_interval_seconds(self, value: int) -> None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = _DEFAULTS["rc2_refresh_retry_interval_seconds"]

        if parsed < _MIN_RETRY_SECONDS:
            parsed = _MIN_RETRY_SECONDS
        if parsed > _MAX_RETRY_SECONDS:
            parsed = _MAX_RETRY_SECONDS

        self._config["rc2_refresh_retry_interval_seconds"] = parsed

    @property
    def dummy_slot_guid(self) -> str:
        return str(self._config.get("dummy_slot_guid", "") or "")

    @dummy_slot_guid.setter
    def dummy_slot_guid(self, value: str) -> None:
        self._config["dummy_slot_guid"] = str(value or "").strip()

    def save(self) -> None:
        config_path = get_config_file_path()
        try:
            with open(config_path, "w") as f:
                json.dump(self._config, f, indent=4)
        except OSError as e:
            _logger.warning("Failed to save config: %s", e)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------
    def _load(self) -> None:
        config_path = get_config_file_path()

        if not os.path.exists(config_path):
            # Initialize a default config file for first-run UX, especially in packaged builds.
            self.save()
            return
        try:
            with open(config_path, "r") as f:
                loaded = json.load(f)
            self._config.update(loaded)
        except (OSError, json.JSONDecodeError) as e:
            _logger.warning("Failed to load config: %s", e)
