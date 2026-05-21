import json
import os

CONFIG_FILE = "kmz_sync_config.json"

_DEFAULTS = {
    "rc2_folder": "",
    "pc_folder": ""
}


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

    def save(self) -> None:
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self._config, f, indent=4)
        except OSError as e:
            print(f"[ConfigManager] Failed to save config: {e}")

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, "r") as f:
                loaded = json.load(f)
            self._config.update(loaded)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[ConfigManager] Failed to load config: {e}")
