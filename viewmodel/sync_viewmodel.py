import os
from datetime import datetime
from typing import List, Tuple

from backends.backend_factory import BackendFactory
from config.config_manager import ConfigManager
from model.kmz_file import KMZFile
from model.rc2_mission import RC2Mission
from services.copy_map_service import CopyMapService
from services.deep_inspection_service import DeepInspectionService
from services.kmz_metadata_service import KMZMetadataService
from services.mission_discovery_service import MissionDiscoveryService
from services.mission_verification_service import MissionVerificationService
from services.preview_cache_service import PreviewCacheService
from services.sync_engine import SyncEngine
from services.windows_powershell_runner import WindowsPowerShellRunner


class SyncViewModel:
    """
    All business logic for the KMZ sync operation.
    No dependency on tkinter — purely data and file operations.
    """

    DEFAULT_ADB_RC2_ROOT = "adb:/sdcard/Android/data/dji.go.v5/files/waypoint"
    DEFAULT_MTP_RC2_ROOT = (
        "mtp:DJI RC 2|Internal shared storage|Android|data|dji.go.v5|files|waypoint"
    )
    POWERSHELL_TIMEOUT_SECONDS = 30
    MTP_LIST_TIMEOUT_SECONDS = 30
    MTP_VERIFY_TIMEOUT_SECONDS = 5
    MTP_SIZE_TOLERANCE_PERCENT = 10.0
    MTP_SIZE_TOLERANCE_BYTES = 4096
    DEEP_INSPECT_TIME_BUDGET_SECONDS_MTP = 8.0
    DEEP_INSPECT_MAX_DEPTH_MTP = 1
    DEEP_INSPECT_MAX_FOLDERS_MTP = 24
    DEEP_INSPECT_MAX_SCAN_FOLDERS_MTP = 10
    DEEP_INSPECT_MAX_FILE_READS_MTP = 16
    DEEP_INSPECT_FOLDER_HINT_TOKENS = (
        "history", "record", "mission", "meta", "index", "db", "database", "sqlite",
    )
    DEEP_INSPECT_FOLDER_SKIP_TOKENS = (
        "mediacache", "media_cache", "cachevideo", "video", "thumb", "thumbnail", "image",
    )

    def __init__(self, config: ConfigManager, copy_map_path: str | None = None):
        self._config = config
        self._last_error: str | None = None
        self._preview_cache_service = PreviewCacheService()
        self._kmz_metadata_service = KMZMetadataService()
        self._mission_verification_service = MissionVerificationService()
        self._mission_discovery_service = MissionDiscoveryService()
        self._deep_inspection_service = DeepInspectionService(self._kmz_metadata_service)
        self._sync_engine = SyncEngine()
        self._windows_powershell_runner = WindowsPowerShellRunner(
            default_timeout_seconds=self.POWERSHELL_TIMEOUT_SECONDS,
            default_mtp_root=self.DEFAULT_MTP_RC2_ROOT,
        )
        self._rc_backend = BackendFactory.create_rc(config.rc2_folder, self._config)
        self._pc_backend = BackendFactory.create_pc(config)
        self._copy_map_service = CopyMapService(copy_map_path=copy_map_path)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _record_copy_mapping(self, source: KMZFile, mission: RC2Mission, dest_filename: str) -> None:
        now = self._now_iso()
        self._copy_map_service.record_mapping(
            source_filename=source.filename,
            source_full_path=source.full_path,
            target_mission_guid=mission.guid,
            target_kmz_filename=dest_filename,
            target_folder_path=mission.full_folder_path,
            connection_mode=self.get_rc2_connection_mode(),
            copied_at=now,
            updated_at=now,
        )

    def get_copy_mapping_summary(self) -> tuple[list[dict[str, str]], str, str]:
        return self._copy_map_service.get_summary()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _is_device_backend_active(self) -> bool:
        return self._rc_backend.get_connection_mode().strip().upper() in {"MTP", "ADB"}

    def clear_stale_preview_cache(self) -> None:
        root = (self._config.rc2_folder or "").strip()
        self._preview_cache_service.clear_stale_preview_cache(root)

    def clear_preview_cache_for_guid(self, guid: str) -> None:
        """Delete the locally-cached preview file and stored timestamp for
        *guid* so the next refresh re-fetches the image from the device."""
        root = (self._config.rc2_folder or "").strip()
        self._preview_cache_service.clear_preview_cache_for_guid(root, guid)

    def clear_all_preview_cache(self) -> Tuple[bool, str]:
        """Clear all locally cached RC preview images for the configured root."""
        root = (self._config.rc2_folder or "").strip()
        if not root:
            return False, "RC-2 root is not configured."

        # Clear legacy timestamp/cache artifacts used by older preview paths.
        self.clear_stale_preview_cache()

        try:
            self._rc_backend.clear_preview_cache(root)
            self._rc_backend.invalidate_cache()
        except Exception as exc:
            return False, f"Failed to clear preview cache:\n{exc}"

        return True, "Preview cache cleared. Next refresh will reload previews from RC-2."

    def get_all_mission_preview_paths(
        self,
        missions: "List[RC2Mission]",
        copy_timeout_seconds: int | None = None,
        list_timeout_seconds: int | None = None,
        allow_live_fetch: bool = True,
    ) -> "dict[str, str | None]":
        """Return {guid: disk_cache_path | None} for every mission.

        For MTP paths a single PowerShell listing call is shared across all
        missions; timestamps determine whether each image needs re-fetching.
        For other path types each mission is looked up individually."""
        root = (self._config.rc2_folder or "").strip()
        if not root:
            return {m.guid: None for m in missions}

        if not self._is_device_backend_active():
            return {m.guid: None for m in missions}

        bulk_fetch = getattr(self._rc_backend, "get_preview_paths_bulk", None)
        if allow_live_fetch and callable(bulk_fetch):
            return bulk_fetch(
                root,
                [m.guid for m in missions],
                copy_timeout_seconds=copy_timeout_seconds,
                list_timeout_seconds=list_timeout_seconds,
            )
        return {
            m.guid: self._rc_backend.get_preview_path(
                root,
                m.guid,
                copy_timeout_seconds=copy_timeout_seconds,
                list_timeout_seconds=list_timeout_seconds,
                allow_live_fetch=allow_live_fetch,
            )
            for m in missions
        }

    def _set_last_error(self, message: str) -> None:
        self._last_error = message

    def consume_last_error(self) -> str | None:
        message = self._last_error
        self._last_error = None
        return message

    def diagnose_rc2_connection(self) -> Tuple[bool, str, str | None]:
        return self._mission_discovery_service.diagnose_rc2_connection(
            root=(self._config.rc2_folder or "").strip(),
            get_status=self._rc_backend.get_status,
            detect_mtp_root=lambda: self._windows_powershell_runner.detect_default_mtp_root(timeout_seconds=10),
            get_adb_status=self.get_adb_status,
            probe_windows_devices=self._windows_powershell_runner.probe_present_rc2_devices,
            default_adb_root=self.DEFAULT_ADB_RC2_ROOT,
        )

    def auto_detect_rc2_folder(self) -> Tuple[bool, str]:
        return self._mission_discovery_service.auto_detect_rc2_folder(
            current_root=self._config.rc2_folder,
            diagnose=self.diagnose_rc2_connection,
            set_rc2_folder=self.set_rc2_folder,
        )

    # ------------------------------------------------------------------
    # Properties (forwarded from config for convenience)
    # ------------------------------------------------------------------
    @property
    def rc2_folder(self) -> str:
        return self._config.rc2_folder

    def get_rc2_connection_mode(self) -> str:
        if not (self._config.rc2_folder or "").strip():
            return "Not Set"
        return self._rc_backend.get_connection_mode()

    def is_rc2_connected(self, timeout_seconds: int | None = None) -> bool:
        """Best-effort connectivity probe for the currently configured RC-2 root."""
        if not (self._config.rc2_folder or "").strip():
            return False
        return self._rc_backend.is_connected(timeout_seconds=timeout_seconds)

    @property
    def pc_folder(self) -> str:
        return self._config.pc_folder

    def get_rc2_refresh_retry_interval_seconds(self) -> int:
        return self._config.rc2_refresh_retry_interval_seconds

    def get_dummy_slot_guid(self) -> str:
        return self._config.dummy_slot_guid

    # ------------------------------------------------------------------
    # Folder update & persistence
    # ------------------------------------------------------------------
    def set_rc2_folder(self, path: str) -> None:
        cleaned = path.strip()
        old_backend = self._rc_backend
        if BackendFactory.path_scheme(cleaned) in {"adb", "mtp"}:
            self._config.rc2_folder = cleaned
        else:
            self._config.rc2_folder = os.path.normpath(cleaned)
        self._preview_cache_service.reset_timestamp_state()
        self._rc_backend = BackendFactory.create_rc(cleaned, self._config)
        close_old = getattr(old_backend, "close", None)
        if callable(close_old):
            try:
                close_old()
            except Exception:
                pass
        self._config.save()

    def shutdown(self) -> None:
        """Release backend resources on app close."""
        close_backend = getattr(self._rc_backend, "close", None)
        if callable(close_backend):
            try:
                close_backend()
            except Exception:
                pass

    def set_pc_folder(self, path: str) -> None:
        self._config.pc_folder = os.path.normpath(path)
        self._config.save()

    def set_dummy_slot_guid(self, guid: str) -> None:
        self._config.dummy_slot_guid = guid
        self._config.save()

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------
    def load_rc2_missions(self) -> List[RC2Mission]:
        """
        Scan the RC-2 root folder. Each sub-directory is a GUID mission slot.
        Returns a sorted list of RC2Mission objects.
        """
        self._last_error = None
        return self._mission_discovery_service.load_rc2_missions(
            root=self._config.rc2_folder,
            list_missions=self._rc_backend.list_missions,
            set_last_error=self._set_last_error,
        )

    def load_pc_kmz_files(self) -> List[KMZFile]:
        """
        Recursively scan the PC source folder for .kmz files.
        Returns a sorted list of KMZFile objects with full paths.
        """
        self._last_error = None
        files, err = self._pc_backend.list_kmz_files()
        if err:
            self._set_last_error(err)
        return files

    def delete_rc2_mission(self, mission: RC2Mission) -> Tuple[bool, str]:
        ok, msg = self._rc_backend.delete_mission(mission)
        if ok:
            self.clear_preview_cache_for_guid(mission.guid)
        return ok, msg

    def delete_pc_kmz_file(self, kmz_file: KMZFile) -> Tuple[bool, str]:
        return self._pc_backend.delete_file(kmz_file)

    def get_adb_status(self) -> Tuple[bool, str]:
        """
        Return (ready, message) for current ADB device state.
        """
        adb_backend = BackendFactory.create_rc(self.DEFAULT_ADB_RC2_ROOT, self._config)
        try:
            return adb_backend.get_status()
        finally:
            close_backend = getattr(adb_backend, "close", None)
            if callable(close_backend):
                try:
                    close_backend()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Core sync operation
    # ------------------------------------------------------------------
    def execute_copy(
        self,
        mission: RC2Mission,
        kmz_file: KMZFile,
    ) -> Tuple[bool, str]:
        """
        Copy kmz_file into the mission's GUID slot, preserving the existing
        destination filename (or defaulting to <GUID>.kmz for empty slots).

        Returns (success: bool, message: str).
        """
        return self._sync_engine.execute_copy(
            rc_backend=self._rc_backend,
            mission=mission,
            kmz_file=kmz_file,
            verify_mtp_copy=self._verify_mtp_copy_via_pull,
            record_copy_mapping=self._record_copy_mapping,
            clear_preview_cache_for_guid=self.clear_preview_cache_for_guid,
        )

    def execute_copy_from_mission(
        self,
        mission: RC2Mission,
        target_kmz_file: KMZFile | None = None,
    ) -> Tuple[bool, str]:
        """
        Copy the selected RC-2 mission KMZ back to the PC folder and save it
        using the selected target filename.

        Returns (success: bool, message: str).
        """
        return self._sync_engine.execute_copy_from_mission(
            rc_backend=self._rc_backend,
            pc_root=(self._config.pc_folder or "").strip(),
            mission=mission,
            target_kmz_file=target_kmz_file,
            record_copy_mapping=self._record_copy_mapping,
        )

    def _verify_mtp_copy_via_pull(
        self,
        mission: RC2Mission,
        source_path: str,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        return self._mission_verification_service.verify_mtp_copy_via_pull(
            rc_backend=self._rc_backend,
            mission=mission,
            source_path=source_path,
            dest_filename=dest_filename,
            size_tolerance_percent=self.MTP_SIZE_TOLERANCE_PERCENT,
            size_tolerance_bytes=self.MTP_SIZE_TOLERANCE_BYTES,
        )

    def _list_slot_files(self, mission: RC2Mission) -> Tuple[bool, List[str] | str]:
        return self._rc_backend.list_slot_files(mission)

    def _read_slot_file_bytes(self, mission: RC2Mission, filename: str) -> Tuple[bool, bytes | str]:
        return self._rc_backend.read_file_bytes(mission, filename)

    def _inspect_metadata_history_candidates(self, mission: RC2Mission, kmz_name: str) -> List[str]:
        return self._rc_backend.inspect_metadata_history_candidates(
            mission,
            kmz_name,
            time_budget_seconds_mtp=self.DEEP_INSPECT_TIME_BUDGET_SECONDS_MTP,
            max_depth_mtp=self.DEEP_INSPECT_MAX_DEPTH_MTP,
            max_folders_mtp=self.DEEP_INSPECT_MAX_FOLDERS_MTP,
            max_scan_folders_mtp=self.DEEP_INSPECT_MAX_SCAN_FOLDERS_MTP,
            max_file_reads_mtp=self.DEEP_INSPECT_MAX_FILE_READS_MTP,
            folder_hint_tokens=self.DEEP_INSPECT_FOLDER_HINT_TOKENS,
            folder_skip_tokens=self.DEEP_INSPECT_FOLDER_SKIP_TOKENS,
        )

    def _inspect_binary_metadata_candidates(self, mission: RC2Mission, kmz_name: str) -> List[str]:
        return self._rc_backend.inspect_binary_metadata_candidates(
            mission,
            kmz_name,
            time_budget_seconds_mtp=self.DEEP_INSPECT_TIME_BUDGET_SECONDS_MTP,
            max_depth_mtp=self.DEEP_INSPECT_MAX_DEPTH_MTP,
            max_folders_mtp=self.DEEP_INSPECT_MAX_FOLDERS_MTP,
            max_scan_folders_mtp=self.DEEP_INSPECT_MAX_SCAN_FOLDERS_MTP,
            folder_hint_tokens=self.DEEP_INSPECT_FOLDER_HINT_TOKENS,
            folder_skip_tokens=self.DEEP_INSPECT_FOLDER_SKIP_TOKENS,
        )

    def inspect_mission_storage(self, mission: RC2Mission, deep: bool = False) -> Tuple[bool, str]:
        return self._deep_inspection_service.inspect_mission_storage(
            mission=mission,
            deep=deep,
            list_slot_files=self._list_slot_files,
            read_slot_file_bytes=self._read_slot_file_bytes,
            inspect_metadata_candidates=self._inspect_metadata_history_candidates,
            inspect_binary_candidates=self._inspect_binary_metadata_candidates,
        )

    # ------------------------------------------------------------------
    # Confirmation text helper (for UI dialogs)
    # ------------------------------------------------------------------
    def confirm_copy_message(self, mission: RC2Mission, kmz_file: KMZFile) -> str:
        return self._sync_engine.confirm_copy_message(mission, kmz_file)



