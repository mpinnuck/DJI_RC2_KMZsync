import json
import os
import tempfile
import unittest

from config.config_manager import ConfigManager, CONFIG_FILE


class TestConfigManager(unittest.TestCase):

    def setUp(self):
        self._orig_dir = os.getcwd()
        self._tmp = tempfile.mkdtemp()
        os.chdir(self._tmp)

    def tearDown(self):
        os.chdir(self._orig_dir)

    def test_defaults_when_no_file(self):
        cfg = ConfigManager()
        self.assertEqual(cfg.rc2_folder, "")
        self.assertEqual(cfg.pc_folder, "")
        self.assertEqual(cfg.rc2_refresh_retry_interval_seconds, 5)

    def test_save_and_reload(self):
        cfg = ConfigManager()
        cfg.rc2_folder = "/media/rc2/waypoints"
        cfg.pc_folder  = "/home/mark/missions"
        cfg.rc2_refresh_retry_interval_seconds = 9
        cfg.save()
        self.assertTrue(os.path.exists(CONFIG_FILE))
        cfg2 = ConfigManager()
        self.assertEqual(cfg2.rc2_folder, "/media/rc2/waypoints")
        self.assertEqual(cfg2.pc_folder,  "/home/mark/missions")
        self.assertEqual(cfg2.rc2_refresh_retry_interval_seconds, 9)

    def test_partial_config_file(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump({"rc2_folder": "/some/path"}, f)
        cfg = ConfigManager()
        self.assertEqual(cfg.rc2_folder, "/some/path")
        self.assertEqual(cfg.pc_folder, "")

    def test_corrupt_config_file_does_not_raise(self):
        with open(CONFIG_FILE, "w") as f:
            f.write("{ not valid json }")
        try:
            cfg = ConfigManager()
        except Exception as e:
            self.fail(f"ConfigManager raised on corrupt file: {e}")
        self.assertEqual(cfg.rc2_folder, "")
        self.assertEqual(cfg.pc_folder,  "")

    def test_setters_update_values(self):
        cfg = ConfigManager()
        cfg.rc2_folder = "/new/rc2"
        cfg.pc_folder  = "/new/pc"
        self.assertEqual(cfg.rc2_folder, "/new/rc2")
        self.assertEqual(cfg.pc_folder,  "/new/pc")

    def test_save_writes_valid_json(self):
        cfg = ConfigManager()
        cfg.rc2_folder = "/a"
        cfg.pc_folder  = "/b"
        cfg.rc2_refresh_retry_interval_seconds = 7
        cfg.save()
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        self.assertEqual(data["rc2_folder"], "/a")
        self.assertEqual(data["pc_folder"],  "/b")
        self.assertEqual(data["rc2_refresh_retry_interval_seconds"], 7)

    def test_retry_interval_clamps_to_min(self):
        cfg = ConfigManager()
        cfg.rc2_refresh_retry_interval_seconds = 0
        self.assertEqual(cfg.rc2_refresh_retry_interval_seconds, 1)

    def test_retry_interval_clamps_to_max(self):
        cfg = ConfigManager()
        cfg.rc2_refresh_retry_interval_seconds = 999
        self.assertEqual(cfg.rc2_refresh_retry_interval_seconds, 300)

    def test_retry_interval_invalid_type_uses_default(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump({"rc2_refresh_retry_interval_seconds": "oops"}, f)
        cfg = ConfigManager()
        self.assertEqual(cfg.rc2_refresh_retry_interval_seconds, 5)


if __name__ == "__main__":
    unittest.main()
