import os
import zipfile
import subprocess
import tempfile
import unittest

try:
    from PIL import Image
except ImportError:
    Image = None

from config.config_manager import ConfigManager
from model.kmz_file import KMZFile
from model.rc2_mission import RC2Mission
from viewmodel.sync_viewmodel import SyncViewModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vm(rc2_root: str = "", pc_root: str = "") -> SyncViewModel:
    """SyncViewModel backed by a no-op ConfigManager (no disk writes)."""
    cfg = ConfigManager.__new__(ConfigManager)
    cfg._config = {"rc2_folder": rc2_root, "pc_folder": pc_root}
    cfg.save = lambda: None
    map_path = os.path.join(tempfile.mkdtemp(), "kmz_copy_map_test.json")
    return SyncViewModel(cfg, copy_map_path=map_path)


def _write(path: str, content: bytes = b"KMZ_DATA") -> None:
    with open(path, "wb") as f:
        f.write(content)


def _write_preview_jpeg(path: str) -> None:
    if Image is None:
        _write(path, b"JPG")
        return

    image = Image.new("RGB", (2, 2), color=(200, 220, 240))
    image.save(path, format="JPEG")


# ---------------------------------------------------------------------------
# load_rc2_missions
# ---------------------------------------------------------------------------

class TestLoadRC2Missions(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_empty_folder_returns_empty_list(self):
        self.assertEqual(_make_vm(rc2_root=self._tmp).load_rc2_missions(), [])

    def test_single_populated_slot(self):
        slot = os.path.join(self._tmp, "guid-0001")
        os.makedirs(slot)
        _write(os.path.join(slot, "mission.kmz"))
        missions = _make_vm(rc2_root=self._tmp).load_rc2_missions()
        self.assertEqual(len(missions), 1)
        self.assertEqual(missions[0].guid, "guid-0001")
        self.assertEqual(missions[0].kmz_name, "mission.kmz")
        self.assertFalse(missions[0].is_empty)

    def test_empty_slot_detected(self):
        os.makedirs(os.path.join(self._tmp, "guid-empty"))
        missions = _make_vm(rc2_root=self._tmp).load_rc2_missions()
        self.assertEqual(len(missions), 1)
        self.assertTrue(missions[0].is_empty)

    def test_multiple_slots_sorted_by_name(self):
        for name in ("guid-zzz", "guid-aaa", "guid-mmm"):
            os.makedirs(os.path.join(self._tmp, name))
        guids = [m.guid for m in _make_vm(rc2_root=self._tmp).load_rc2_missions()]
        self.assertEqual(guids, sorted(guids))

    def test_non_directory_entries_ignored(self):
        _write(os.path.join(self._tmp, "stray_file.kmz"))
        os.makedirs(os.path.join(self._tmp, "guid-real"))
        missions = _make_vm(rc2_root=self._tmp).load_rc2_missions()
        self.assertEqual(len(missions), 1)
        self.assertEqual(missions[0].guid, "guid-real")

    def test_only_first_kmz_in_slot_surfaced(self):
        slot = os.path.join(self._tmp, "guid-multi")
        os.makedirs(slot)
        _write(os.path.join(slot, "alpha.kmz"))
        _write(os.path.join(slot, "beta.kmz"))
        missions = _make_vm(rc2_root=self._tmp).load_rc2_missions()
        self.assertEqual(len(missions), 1)
        self.assertIn(missions[0].kmz_name, ("alpha.kmz", "beta.kmz"))

    def test_invalid_rc2_path_returns_empty_list(self):
        self.assertEqual(_make_vm(rc2_root="/nonexistent/xyz").load_rc2_missions(), [])

    def test_blank_rc2_path_returns_empty_list(self):
        self.assertEqual(_make_vm(rc2_root="").load_rc2_missions(), [])


# ---------------------------------------------------------------------------
# load_pc_kmz_files
# ---------------------------------------------------------------------------

class TestLoadPCKMZFiles(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_empty_folder_returns_empty_list(self):
        self.assertEqual(_make_vm(pc_root=self._tmp).load_pc_kmz_files(), [])

    def test_kmz_files_listed(self):
        for name in ("survey.kmz", "orbit.kmz"):
            _write(os.path.join(self._tmp, name))
        files = _make_vm(pc_root=self._tmp).load_pc_kmz_files()
        self.assertEqual(len(files), 2)
        names = {f.filename for f in files}
        self.assertIn("survey.kmz", names)
        self.assertIn("orbit.kmz", names)

    def test_non_kmz_files_excluded(self):
        _write(os.path.join(self._tmp, "mission.kmz"))
        _write(os.path.join(self._tmp, "notes.txt"))
        files = _make_vm(pc_root=self._tmp).load_pc_kmz_files()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].filename, "mission.kmz")

    def test_case_insensitive_kmz_extension(self):
        _write(os.path.join(self._tmp, "UPPER.KMZ"))
        _write(os.path.join(self._tmp, "mixed.Kmz"))
        self.assertEqual(len(_make_vm(pc_root=self._tmp).load_pc_kmz_files()), 2)

    def test_files_sorted_by_name(self):
        for name in ("zzz.kmz", "aaa.kmz", "mmm.kmz"):
            _write(os.path.join(self._tmp, name))
        names = [f.filename for f in _make_vm(pc_root=self._tmp).load_pc_kmz_files()]
        self.assertEqual(names, sorted(names))

    def test_invalid_pc_path_returns_empty_list(self):
        self.assertEqual(_make_vm(pc_root="/nonexistent/xyz").load_pc_kmz_files(), [])

    def test_blank_pc_path_returns_empty_list(self):
        self.assertEqual(_make_vm(pc_root="").load_pc_kmz_files(), [])


class TestMissionPreviewLookup(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_local_preview_path_uses_map_preview_folder(self):
        preview_dir = os.path.join(self._tmp, "map_preview")
        os.makedirs(preview_dir)
        preview_path = os.path.join(preview_dir, "guid-001.jpg")
        _write(preview_path, b"PNG")

        vm = _make_vm(rc2_root=self._tmp)
        self.assertEqual(vm.get_mission_preview_path("guid-001"), preview_path)

    def test_local_preview_falls_back_to_png(self):
        preview_dir = os.path.join(self._tmp, "map_preview")
        os.makedirs(preview_dir)
        preview_path = os.path.join(preview_dir, "guid-001.png")
        _write(preview_path, b"PNG")

        vm = _make_vm(rc2_root=self._tmp)
        self.assertEqual(vm.get_mission_preview_path("guid-001"), preview_path)

    def test_local_preview_supports_nested_guid_folder(self):
        preview_dir = os.path.join(self._tmp, "map_preview", "guid-001")
        os.makedirs(preview_dir)
        preview_path = os.path.join(preview_dir, "guid-001.jpg")
        _write(preview_path, b"PNG")

        vm = _make_vm(rc2_root=self._tmp)
        self.assertEqual(vm.get_mission_preview_path("guid-001"), preview_path)

    def test_missing_local_preview_returns_none(self):
        vm = _make_vm(rc2_root=self._tmp)
        self.assertIsNone(vm.get_mission_preview_path("guid-missing"))

    def test_mtp_preview_is_copied_to_cache(self):
        vm = _make_vm(rc2_root=SyncViewModel.DEFAULT_MTP_RC2_ROOT)
        cache_base = vm._preview_cache_path(SyncViewModel.DEFAULT_MTP_RC2_ROOT, "guid-123")
        for ext in (".jpg", ".jpeg", ".png"):
            stale = f"{cache_base}{ext}"
            if os.path.isfile(stale):
                os.remove(stale)

        def fake_list(path: str):
            self.assertTrue(path.endswith("|map_preview"))
            return True, [{"Name": "guid-123.jpg", "IsFolder": False}]

        def fake_copy(folder: str, filename: str, dest: str):
            self.assertTrue(folder.endswith("|map_preview"))
            self.assertEqual(filename, "guid-123.jpg")
            self.assertIn(".jpg", dest)
            _write_preview_jpeg(dest)
            return True, dest

        vm._list_mtp_items = fake_list
        vm._copy_file_from_mtp_folder = fake_copy
        preview_path = vm.get_mission_preview_path("guid-123")
        self.assertIsNotNone(preview_path)
        self.assertTrue(os.path.isfile(preview_path))
        self.assertEqual(os.path.splitext(preview_path)[1].lower(), ".jpg")
        self.assertIn("guid-123", os.path.basename(preview_path).lower())

    def test_mtp_preview_supports_nested_guid_folder(self):
        vm = _make_vm(rc2_root=SyncViewModel.DEFAULT_MTP_RC2_ROOT)

        def fake_list(path: str):
            if path.endswith("|map_preview"):
                return True, [{"Name": "guid-456", "IsFolder": True}]
            if path.endswith("|map_preview|guid-456"):
                return True, [{"Name": "guid-456.jpg", "IsFolder": False}]
            self.fail(f"Unexpected list path: {path}")

        def fake_copy(folder: str, filename: str, dest: str):
            self.assertTrue(folder.endswith("|map_preview|guid-456"))
            self.assertEqual(filename, "guid-456.jpg")
            _write_preview_jpeg(dest)
            return True, dest

        vm._list_mtp_items = fake_list
        vm._copy_file_from_mtp_folder = fake_copy

        preview_path = vm.get_mission_preview_path("guid-456")
        self.assertIsNotNone(preview_path)
        self.assertTrue(os.path.isfile(preview_path))
        self.assertEqual(os.path.splitext(preview_path)[1].lower(), ".jpg")


# ---------------------------------------------------------------------------
# execute_copy
# ---------------------------------------------------------------------------

class TestExecuteCopy(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._rc2 = os.path.join(self._tmp, "rc2")
        self._pc  = os.path.join(self._tmp, "pc")
        os.makedirs(self._rc2)
        os.makedirs(self._pc)
        self._vm = _make_vm(rc2_root=self._rc2, pc_root=self._pc)

    def _slot(self, guid: str, kmz_name: str = "") -> RC2Mission:
        folder = os.path.join(self._rc2, guid)
        os.makedirs(folder, exist_ok=True)
        if kmz_name:
            _write(os.path.join(folder, kmz_name))
        return RC2Mission(guid=guid, kmz_name=kmz_name, full_folder_path=folder)

    def _source(self, filename: str, content: bytes = b"NEW_KMZ") -> KMZFile:
        path = os.path.join(self._pc, filename)
        _write(path, content)
        return KMZFile(filename=filename, full_path=path)

    def test_copy_into_populated_slot_preserves_dest_name(self):
        mission = self._slot("guid-001", "existing.kmz")
        kmz     = self._source("new_mission.kmz", b"NEW")
        ok, _   = self._vm.execute_copy(mission, kmz)
        self.assertTrue(ok)
        dest = os.path.join(self._rc2, "guid-001", "existing.kmz")
        self.assertTrue(os.path.exists(dest))
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), b"NEW")

    def test_copy_into_empty_slot_uses_guid_as_filename(self):
        mission = self._slot("guid-002")
        kmz     = self._source("source.kmz", b"DATA")
        ok, _   = self._vm.execute_copy(mission, kmz)
        self.assertTrue(ok)
        dest = os.path.join(self._rc2, "guid-002", "guid-002.kmz")
        self.assertTrue(os.path.exists(dest))

    def test_copy_overwrites_existing_dest_file(self):
        mission = self._slot("guid-003", "target.kmz")
        kmz     = self._source("replacement.kmz", b"REPLACED")
        self._vm.execute_copy(mission, kmz)
        dest = os.path.join(self._rc2, "guid-003", "target.kmz")
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), b"REPLACED")

    def test_source_file_missing_returns_failure(self):
        mission = self._slot("guid-004", "target.kmz")
        kmz = KMZFile(filename="ghost.kmz",
                      full_path=os.path.join(self._pc, "ghost.kmz"))
        ok, msg = self._vm.execute_copy(mission, kmz)
        self.assertFalse(ok)
        self.assertIn("not found", msg.lower())

    def test_dest_folder_missing_returns_failure(self):
        mission = RC2Mission(
            guid="guid-missing",
            kmz_name="target.kmz",
            full_folder_path=os.path.join(self._rc2, "does_not_exist")
        )
        kmz = self._source("source.kmz")
        ok, msg = self._vm.execute_copy(mission, kmz)
        self.assertFalse(ok)
        self.assertIn("not found", msg.lower())

    def test_success_message_contains_source_and_slot(self):
        mission = self._slot("guid-005", "target.kmz")
        kmz     = self._source("my_flight.kmz")
        ok, msg = self._vm.execute_copy(mission, kmz)
        self.assertTrue(ok)
        self.assertIn("my_flight.kmz", msg)
        self.assertIn("guid-005", msg)

    def test_source_file_unchanged_after_copy(self):
        mission = self._slot("guid-006", "dest.kmz")
        kmz     = self._source("src.kmz", b"ORIGINAL")
        self._vm.execute_copy(mission, kmz)
        self.assertTrue(os.path.exists(kmz.full_path))
        with open(kmz.full_path, "rb") as fh:
            self.assertEqual(fh.read(), b"ORIGINAL")

    def test_copy_back_overwrites_selected_pc_filename(self):
        mission = self._slot("guid-008", "edited_on_rc2.kmz")
        with open(os.path.join(mission.full_folder_path, "edited_on_rc2.kmz"), "wb") as fh:
            fh.write(b"RC2_EDITED")

        target = self._source("dronelink_target.kmz", b"ORIGINAL")
        ok, _ = self._vm.execute_copy_from_mission(mission, target)

        self.assertTrue(ok)
        with open(target.full_path, "rb") as fh:
            self.assertEqual(fh.read(), b"RC2_EDITED")

        rows, _, _ = self._vm.get_copy_mapping_summary()
        match = next((row for row in rows if row.get("source_filename") == "dronelink_target.kmz"), None)
        self.assertIsNotNone(match)
        self.assertEqual(match.get("target_mission_guid"), "guid-008")
        self.assertEqual(match.get("target_kmz_filename"), "edited_on_rc2.kmz")

    def test_copy_back_uses_first_slot_kmz_when_name_missing(self):
        mission = self._slot("guid-009")
        source_in_slot = os.path.join(mission.full_folder_path, "from_slot.kmz")
        with open(source_in_slot, "wb") as fh:
            fh.write(b"FROM_SLOT")
        mission = RC2Mission(guid="guid-009", kmz_name="", full_folder_path=mission.full_folder_path)

        target = self._source("target_name.kmz", b"OLD")
        ok, _ = self._vm.execute_copy_from_mission(mission, target)

        self.assertTrue(ok)
        with open(target.full_path, "rb") as fh:
            self.assertEqual(fh.read(), b"FROM_SLOT")

    def test_copy_back_fails_when_slot_has_no_kmz(self):
        mission = self._slot("guid-010")
        target = self._source("target_name.kmz", b"OLD")

        ok, msg = self._vm.execute_copy_from_mission(mission, target)

        self.assertFalse(ok)
        self.assertIn("no kmz", msg.lower())

    def test_copy_back_without_target_selection_uses_guid_filename(self):
        mission = self._slot("guid-011", "edited_on_rc2.kmz")
        with open(os.path.join(mission.full_folder_path, "edited_on_rc2.kmz"), "wb") as fh:
            fh.write(b"RC2_TO_GUID")

        ok, _ = self._vm.execute_copy_from_mission(mission, None)
        self.assertTrue(ok)

        dest_path = os.path.join(self._pc, "guid-011.kmz")
        self.assertTrue(os.path.isfile(dest_path))
        with open(dest_path, "rb") as fh:
            self.assertEqual(fh.read(), b"RC2_TO_GUID")

    def test_delete_selected_rc2_mission_removes_folder(self):
        mission = self._slot("guid-del-001", "existing.kmz")
        self.assertTrue(os.path.isdir(mission.full_folder_path))

        ok, _ = self._vm.delete_rc2_mission(mission)

        self.assertTrue(ok)
        self.assertFalse(os.path.exists(mission.full_folder_path))

    def test_delete_selected_pc_kmz_removes_file(self):
        kmz = self._source("delete_me.kmz", b"TO_DELETE")
        self.assertTrue(os.path.isfile(kmz.full_path))

        ok, _ = self._vm.delete_pc_kmz_file(kmz)

        self.assertTrue(ok)
        self.assertFalse(os.path.exists(kmz.full_path))

    def test_delete_selected_pc_kmz_fails_when_missing(self):
        kmz = KMZFile(filename="missing.kmz", full_path=os.path.join(self._pc, "missing.kmz"))

        ok, msg = self._vm.delete_pc_kmz_file(kmz)

        self.assertFalse(ok)
        self.assertIn("not found", msg.lower())


# ---------------------------------------------------------------------------
# confirm_copy_message
# ---------------------------------------------------------------------------

class TestConfirmCopyMessage(unittest.TestCase):

    def test_message_contains_guid(self):
        vm      = _make_vm()
        mission = RC2Mission("my-guid", "old.kmz", "/some/path")
        kmz     = KMZFile("new.kmz", "/pc/new.kmz")
        self.assertIn("my-guid", vm.confirm_copy_message(mission, kmz))

    def test_message_contains_source_filename(self):
        vm      = _make_vm()
        mission = RC2Mission("my-guid", "old.kmz", "/some/path")
        kmz     = KMZFile("new.kmz", "/pc/new.kmz")
        self.assertIn("new.kmz", vm.confirm_copy_message(mission, kmz))

    def test_message_shows_dest_filename_for_empty_slot(self):
        vm      = _make_vm()
        mission = RC2Mission("slot-guid", "", "/some/path")
        kmz     = KMZFile("source.kmz", "/pc/source.kmz")
        self.assertIn("slot-guid.kmz", vm.confirm_copy_message(mission, kmz))


# ---------------------------------------------------------------------------
# set_rc2_folder / set_pc_folder
# ---------------------------------------------------------------------------

class TestFolderSetters(unittest.TestCase):

    def test_set_rc2_folder_normalises_path(self):
        vm = _make_vm()
        vm.set_rc2_folder("/some//path/")
        self.assertNotIn("//", vm.rc2_folder)

    def test_set_pc_folder_normalises_path(self):
        vm = _make_vm()
        vm.set_pc_folder("/some//path/")
        self.assertNotIn("//", vm.pc_folder)

    def test_set_rc2_folder_preserves_adb_prefix(self):
        vm = _make_vm()
        vm.set_rc2_folder("adb:/sdcard/Android/data/dji.go.v5/files/waypoint")
        self.assertTrue(vm.rc2_folder.startswith("adb:"))

    def test_set_rc2_folder_preserves_mtp_prefix(self):
        vm = _make_vm()
        vm.set_rc2_folder(
            "mtp:DJI RC 2|Internal shared storage|Android|data|dji.go.v5|files|waypoint"
        )
        self.assertTrue(vm.rc2_folder.startswith("mtp:"))


class TestAdbHelpers(unittest.TestCase):

    def test_adb_remote_root_defaults_when_empty(self):
        self.assertEqual(
            SyncViewModel._adb_remote_root("adb:"),
            "/sdcard/Android/data/dji.go.v5/files/waypoint"
        )

    def test_adb_remote_root_normalises_slashes(self):
        self.assertEqual(
            SyncViewModel._adb_remote_root("adb:sdcard\\Android\\data"),
            "/sdcard/Android/data"
        )


class TestMtpHelpers(unittest.TestCase):

    def test_mtp_segments_default_when_empty(self):
        self.assertEqual(
            SyncViewModel._mtp_segments("mtp:"),
            [
                "DJI RC 2",
                "Internal shared storage",
                "Android",
                "data",
                "dji.go.v5",
                "files",
                "waypoint",
            ],
        )

    def test_mtp_join_appends_child(self):
        root = SyncViewModel.DEFAULT_MTP_RC2_ROOT
        self.assertEqual(
            SyncViewModel._mtp_join(root, "ABC123"),
            f"{root}|ABC123",
        )

    def test_rc2_slot_filter_excludes_support_folders(self):
        self.assertFalse(SyncViewModel._is_rc2_slot_name("capability"))
        self.assertFalse(SyncViewModel._is_rc2_slot_name("map_preview"))
        self.assertTrue(SyncViewModel._is_rc2_slot_name("guid-0001"))


class TestPowerShellHelpers(unittest.TestCase):

    def test_run_powershell_timeout_returns_error_instead_of_hanging(self):
        original_run = subprocess.run
        original_exec = SyncViewModel._powershell_executable

        def fake_run(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="powershell", timeout=1)

        try:
            subprocess.run = fake_run
            SyncViewModel._powershell_executable = staticmethod(lambda: "powershell")
            ok, message = SyncViewModel._run_powershell("Write-Output 'hi'", timeout_seconds=1)
        finally:
            subprocess.run = original_run
            SyncViewModel._powershell_executable = original_exec

        self.assertFalse(ok)
        self.assertIn("timed out", message.lower())

    def test_run_powershell_keyboard_interrupt_returns_error(self):
        original_run = subprocess.run
        original_exec = SyncViewModel._powershell_executable

        def fake_run(*_args, **_kwargs):
            raise KeyboardInterrupt()

        try:
            subprocess.run = fake_run
            SyncViewModel._powershell_executable = staticmethod(lambda: "powershell")
            ok, message = SyncViewModel._run_powershell("Write-Output 'hi'", timeout_seconds=1)
        finally:
            subprocess.run = original_run
            SyncViewModel._powershell_executable = original_exec

        self.assertFalse(ok)
        self.assertIn("interrupted", message.lower())


class TestViewModelErrorChannel(unittest.TestCase):

    def test_consume_last_error_returns_then_clears(self):
        vm = _make_vm()
        vm._set_last_error("sample error")
        self.assertEqual(vm.consume_last_error(), "sample error")
        self.assertIsNone(vm.consume_last_error())


class TestAdbErrorFormatting(unittest.TestCase):

    def test_offline_message_is_actionable(self):
        msg = SyncViewModel._format_adb_error("adb.exe: device offline")
        self.assertIn("offline", msg.lower())
        self.assertIn("adb devices", msg.lower())

    def test_unauthorized_message_is_actionable(self):
        msg = SyncViewModel._format_adb_error("error: device unauthorized")
        self.assertIn("unauthorized", msg.lower())
        self.assertIn("accept", msg.lower())

    def test_no_device_message_is_actionable(self):
        msg = SyncViewModel._format_adb_error("error: no devices/emulators found")
        self.assertIn("no adb device detected", msg.lower())

    def test_unmapped_error_passes_through(self):
        source = "some adb failure"
        self.assertEqual(SyncViewModel._format_adb_error(source), source)


class TestGetAdbStatus(unittest.TestCase):

    def test_no_devices(self):
        vm = _make_vm(rc2_root="adb:/sdcard/Android/data/dji.go.v5/files/waypoint")
        vm._run_adb = lambda _args: (True, "List of devices attached\n\n")
        ok, msg = vm.get_adb_status()
        self.assertFalse(ok)
        self.assertIn("no adb devices", msg.lower())

    def test_device_ready(self):
        vm = _make_vm(rc2_root="adb:/sdcard/Android/data/dji.go.v5/files/waypoint")
        vm._run_adb = lambda _args: (True, "List of devices attached\nABC123\tdevice\n")
        ok, msg = vm.get_adb_status()
        self.assertTrue(ok)
        self.assertIn("ABC123", msg)

    def test_device_offline(self):
        vm = _make_vm(rc2_root="adb:/sdcard/Android/data/dji.go.v5/files/waypoint")
        vm._run_adb = lambda _args: (True, "List of devices attached\nABC123\toffline\n")
        ok, msg = vm.get_adb_status()
        self.assertFalse(ok)
        self.assertIn("offline", msg.lower())

    def test_device_unauthorized(self):
        vm = _make_vm(rc2_root="adb:/sdcard/Android/data/dji.go.v5/files/waypoint")
        vm._run_adb = lambda _args: (True, "List of devices attached\nABC123\tunauthorized\n")
        ok, msg = vm.get_adb_status()
        self.assertFalse(ok)
        self.assertIn("unauthorized", msg.lower())


class TestDiagnoseRC2Connection(unittest.TestCase):

    def test_local_folder_wins_when_reachable(self):
        tmp = tempfile.mkdtemp()
        vm = _make_vm(rc2_root=tmp)
        ok, msg, detected = vm.diagnose_rc2_connection()
        self.assertTrue(ok)
        self.assertIn("reachable on disk", msg.lower())
        self.assertEqual(detected, tmp)

    def test_ready_adb_returns_default_adb_root(self):
        vm = _make_vm()
        vm._detect_mtp_rc2_folder = lambda: None
        vm.get_adb_status = lambda: (True, "ADB device connected: ABC123")
        vm._probe_windows_present_rc2_devices = lambda: []
        ok, msg, detected = vm.diagnose_rc2_connection()
        self.assertTrue(ok)
        self.assertIn("adb device connected", msg.lower())
        self.assertEqual(detected, SyncViewModel.DEFAULT_ADB_RC2_ROOT)

    def test_ready_mtp_returns_default_mtp_root(self):
        vm = _make_vm()
        vm._detect_mtp_rc2_folder = lambda: SyncViewModel.DEFAULT_MTP_RC2_ROOT
        ok, msg, detected = vm.diagnose_rc2_connection()
        self.assertTrue(ok)
        self.assertIn("explorer-style mtp", msg.lower())
        self.assertEqual(detected, SyncViewModel.DEFAULT_MTP_RC2_ROOT)

    def test_windows_device_without_adb_reports_actionable_message(self):
        vm = _make_vm()
        vm._detect_mtp_rc2_folder = lambda: None
        vm.get_adb_status = lambda: (False, "No ADB devices detected.")
        vm._probe_windows_present_rc2_devices = lambda: ["DJI RC 2 [OK]"]
        ok, msg, detected = vm.diagnose_rc2_connection()
        self.assertFalse(ok)
        self.assertIn("windows sees rc-2 related device entries", msg.lower())
        self.assertIn("dji rc 2", msg.lower())
        self.assertIsNone(detected)

    def test_missing_everything_reports_vm_or_attach_hint(self):
        vm = _make_vm()
        vm._detect_mtp_rc2_folder = lambda: None
        vm.get_adb_status = lambda: (False, "No ADB devices detected.")
        vm._probe_windows_present_rc2_devices = lambda: []
        ok, msg, detected = vm.diagnose_rc2_connection()
        self.assertFalse(ok)
        self.assertIn("not reachable", msg.lower())
        self.assertIn("vm", msg.lower())
        self.assertIsNone(detected)


class TestAutoDetectRC2Folder(unittest.TestCase):

    def test_updates_config_when_adb_is_detected(self):
        vm = _make_vm()
        vm.diagnose_rc2_connection = lambda: (
            True,
            "ADB device connected: ABC123",
            SyncViewModel.DEFAULT_ADB_RC2_ROOT,
        )
        ok, msg = vm.auto_detect_rc2_folder()
        self.assertTrue(ok)
        self.assertEqual(vm.rc2_folder, SyncViewModel.DEFAULT_ADB_RC2_ROOT)
        self.assertIn("updated", msg.lower())

    def test_leaves_config_unchanged_when_not_detected(self):
        vm = _make_vm(rc2_root="")
        vm.diagnose_rc2_connection = lambda: (False, "RC-2 not reachable.", None)
        ok, msg = vm.auto_detect_rc2_folder()
        self.assertFalse(ok)
        self.assertEqual(vm.rc2_folder, "")
        self.assertIn("not reachable", msg.lower())


class TestLoadRC2MissionsMtp(unittest.TestCase):

    def test_loads_guid_slots_and_filters_support_folders(self):
        vm = _make_vm(rc2_root=SyncViewModel.DEFAULT_MTP_RC2_ROOT)

        def fake_list(path: str):
            if path == SyncViewModel.DEFAULT_MTP_RC2_ROOT:
                return True, [
                    {"Name": "capability", "IsFolder": True},
                    {"Name": "map_preview", "IsFolder": True},
                    {"Name": "guid-001", "IsFolder": True},
                    {"Name": "guid-002", "IsFolder": True},
                ]
            if path.endswith("|guid-001"):
                return True, [
                    {"Name": "mission.kmz", "IsFolder": False},
                    {"Name": "preview.jpg", "IsFolder": False},
                ]
            if path.endswith("|guid-002"):
                return True, []
            return False, "missing"

        vm._list_mtp_items = fake_list
        missions = vm.load_rc2_missions()
        self.assertEqual([mission.guid for mission in missions], ["guid-001", "guid-002"])
        self.assertEqual(missions[0].kmz_name, "mission.kmz")
        self.assertTrue(missions[1].is_empty)
        self.assertTrue(missions[0].full_folder_path.endswith("|guid-001"))


class TestExecuteCopyMtp(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._pc = os.path.join(self._tmp, "pc")
        os.makedirs(self._pc)
        self._vm = _make_vm(rc2_root=SyncViewModel.DEFAULT_MTP_RC2_ROOT, pc_root=self._pc)

    def test_copy_into_mtp_slot_uses_existing_dest_name(self):
        src = os.path.join(self._pc, "source.kmz")
        _write(src, b"DATA")
        mission = RC2Mission(
            guid="guid-001",
            kmz_name="existing.kmz",
            full_folder_path=f"{SyncViewModel.DEFAULT_MTP_RC2_ROOT}|guid-001",
        )
        captured = {}

        def fake_copy(folder: str, source_path: str):
            captured["folder"] = folder
            captured["source_path"] = source_path
            return True, "existing.kmz"

        self._vm._copy_file_to_mtp_folder = fake_copy
        ok, msg = self._vm.execute_copy(mission, KMZFile("source.kmz", src))
        self.assertTrue(ok)
        self.assertEqual(captured["folder"], mission.full_folder_path)
        self.assertEqual(os.path.basename(captured["source_path"]), "existing.kmz")
        self.assertIn("existing.kmz", msg)

    def test_copy_into_empty_mtp_slot_uses_guid_filename(self):
        src = os.path.join(self._pc, "source.kmz")
        _write(src, b"DATA")
        mission = RC2Mission(
            guid="guid-002",
            kmz_name="",
            full_folder_path=f"{SyncViewModel.DEFAULT_MTP_RC2_ROOT}|guid-002",
        )
        captured = {}

        def fake_copy(folder: str, source_path: str):
            captured["folder"] = folder
            captured["source_path"] = source_path
            return True, "guid-002.kmz"

        self._vm._copy_file_to_mtp_folder = fake_copy
        ok, _ = self._vm.execute_copy(mission, KMZFile("source.kmz", src))
        self.assertTrue(ok)
        self.assertEqual(os.path.basename(captured["source_path"]), "guid-002.kmz")


class TestCopyMapping(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._vm = _make_vm()
        self._vm._copy_map_path = os.path.join(self._tmp, "test_map.json")

    def _mission(self) -> RC2Mission:
        return RC2Mission("guid-1", "target.kmz", "/slot/guid-1")

    def test_record_creates_entry(self):
        mission = self._mission()
        kmz = KMZFile("source.kmz", "/pc/source.kmz")

        self._vm._record_copy_mapping(kmz, mission, "target.kmz")
        rows, _, _ = self._vm.get_copy_mapping_summary()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_filename"], "source.kmz")
        self.assertEqual(rows[0]["target_mission_guid"], "guid-1")

    def test_history_capped_at_25(self):
        mission = self._mission()
        for i in range(30):
            kmz = KMZFile("source.kmz", f"/pc/source_{i}.kmz")
            self._vm._record_copy_mapping(kmz, mission, "target.kmz")

        payload = self._vm._load_copy_map()
        history = payload["by_source"]["source.kmz"]["history"]
        self.assertEqual(len(history), 25)

    def test_corrupt_map_file_returns_empty_summary(self):
        with open(self._vm._copy_map_path, "w", encoding="utf-8") as fh:
            fh.write("{ not valid json }")

        rows, _, _ = self._vm.get_copy_mapping_summary()
        self.assertEqual(rows, [])

    def test_summary_sorted_newest_first(self):
        mission = self._mission()
        timestamps = iter([
            "2026-01-01 10:00:00",
            "2026-01-01 10:00:00",
            "2026-01-01 10:00:01",
            "2026-01-01 10:00:01",
        ])
        self._vm._now_iso = lambda: next(timestamps)

        self._vm._record_copy_mapping(KMZFile("a.kmz", "/pc/a.kmz"), mission, "target.kmz")
        self._vm._record_copy_mapping(KMZFile("b.kmz", "/pc/b.kmz"), mission, "target.kmz")

        rows, _, _ = self._vm.get_copy_mapping_summary()
        self.assertEqual(rows[0]["source_filename"], "b.kmz")
        self.assertEqual(rows[1]["source_filename"], "a.kmz")


class TestInspectMissionStorage(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _mission_with_kmz(self) -> RC2Mission:
        slot_dir = os.path.join(self._tmp, "guid-inspect")
        os.makedirs(slot_dir)
        kmz_path = os.path.join(slot_dir, "mission.kmz")
        with zipfile.ZipFile(kmz_path, "w") as archive:
            archive.writestr("wpmz/template.kml", "<kml><Document></Document></kml>")
            archive.writestr("wpmz/waylines.wpml", "<wpml><mission></mission></wpml>")
        return RC2Mission("guid-inspect", "mission.kmz", slot_dir)

    def test_quick_inspect_skips_deep_probes_and_reports_summary(self):
        vm = _make_vm(rc2_root=self._tmp)
        mission = self._mission_with_kmz()

        def _should_not_run(*_args, **_kwargs):
            raise AssertionError("Deep probe should not run in quick inspect mode")

        vm._inspect_metadata_history_candidates = _should_not_run
        vm._inspect_binary_metadata_candidates = _should_not_run

        ok, details = vm.inspect_mission_storage(mission, deep=False)

        self.assertTrue(ok)
        self.assertIn("Quick inspect summary", details)
        self.assertIn("external to the KMZ", details)

    def test_deep_inspect_reports_binary_candidates(self):
        vm = _make_vm(rc2_root=self._tmp)
        mission = self._mission_with_kmz()

        with open(os.path.join(self._tmp, "DJI_MissionIndex.sqlite"), "wb") as fh:
            fh.write(b"sqlite")

        ok, details = vm.inspect_mission_storage(mission, deep=True)

        self.assertTrue(ok)
        self.assertIn("Best candidate:", details)
        self.assertIn("Binary metadata/index search", details)
        self.assertIn("DJI_MissionIndex.sqlite", details)
        self.assertIn("Deep inspect summary", details)


if __name__ == "__main__":
    unittest.main()
