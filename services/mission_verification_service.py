from __future__ import annotations

import os

from model.rc2_mission import RC2Mission


class MissionVerificationService:
    """Copy verification and post-transfer integrity checks."""

    @staticmethod
    def verify_mtp_copy_via_pull(
        *,
        rc_backend,
        mission: RC2Mission,
        source_path: str,
        dest_filename: str,
        size_tolerance_percent: float,
        size_tolerance_bytes: int,
    ) -> tuple[bool, str]:
        expected_size = os.path.getsize(source_path)
        try:
            ok_size, out_size = rc_backend.get_file_size_from_path(
                mission.full_folder_path,
                dest_filename,
            )
            if not ok_size:
                return False, f"Unable to query destination size for verification:\n{out_size}"

            actual_size = int(out_size)
            size_diff = abs(actual_size - expected_size)
            percent_diff = (size_diff / float(expected_size) * 100.0) if expected_size > 0 else 100.0
            if actual_size <= 0 or (
                size_diff > size_tolerance_bytes
                and percent_diff > size_tolerance_percent
            ):
                return False, (
                    "Destination size mismatch.\n"
                    f"Expected size: {expected_size} bytes\n"
                    f"Destination size: {actual_size} bytes\n"
                    f"Tolerance: {size_tolerance_percent}% or {size_tolerance_bytes} bytes"
                )
        except OSError as exc:
            return False, f"Verification failed:\n{exc}"

        return True, "ok"
