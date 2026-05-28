from __future__ import annotations

import io
import zipfile
from typing import Callable

from services.kmz_metadata_service import KMZMetadataService
from model.rc2_mission import RC2Mission


class DeepInspectionService:
    """Deep mission diagnostics and storage inspection helpers."""

    def __init__(self, kmz_metadata_service: KMZMetadataService):
        self._kmz_metadata_service = kmz_metadata_service

    def inspect_mission_storage(
        self,
        *,
        mission: RC2Mission,
        deep: bool,
        list_slot_files: Callable[[RC2Mission], tuple[bool, list[str] | str]],
        read_slot_file_bytes: Callable[[RC2Mission, str], tuple[bool, bytes | str]],
        inspect_metadata_candidates: Callable[[RC2Mission, str], list[str]],
        inspect_binary_candidates: Callable[[RC2Mission, str], list[str]],
    ) -> tuple[bool, str]:
        lines: list[str] = []
        lines.append(f"Inspecting mission: {mission.guid}")
        lines.append(f"Mission path: {mission.full_folder_path}")

        ok_list, listing = list_slot_files(mission)
        if not ok_list:
            return False, f"Failed to list slot files: {listing}"

        slot_files = listing if isinstance(listing, list) else []
        lines.append(f"Mission files ({len(slot_files)}): {', '.join(slot_files) if slot_files else '[none]'}")

        kmz_name = mission.kmz_name
        if not kmz_name:
            for name in slot_files:
                if name.lower().endswith(".kmz"):
                    kmz_name = name
                    break

        if not kmz_name:
            lines.append("No KMZ file found in selected mission.")
            lines.append("RC-2 display name is likely managed outside this slot by DJI app metadata.")
            return True, "\n".join(lines)

        lines.append(f"Inspecting KMZ: {kmz_name}")
        ok_bytes, payload = read_slot_file_bytes(mission, kmz_name)
        if not ok_bytes:
            return False, f"Failed to read KMZ from slot: {payload}"

        kmz_bytes = payload if isinstance(payload, bytes) else b""
        try:
            with zipfile.ZipFile(io.BytesIO(kmz_bytes)) as archive:
                entries = archive.namelist()
                lines.append(f"KMZ entries ({len(entries)}): {', '.join(entries[:12])}{' ...' if len(entries) > 12 else ''}")

                xml_entries = [
                    name for name in entries
                    if name.lower().endswith((".kml", ".wpml", ".xml"))
                ]

                discovered_names: list[str] = []
                waypoint_count = 0
                for name in xml_entries[:8]:
                    raw = archive.read(name)
                    text = ""
                    for encoding in ("utf-8", "utf-16", "latin-1"):
                        try:
                            text = raw.decode(encoding)
                            break
                        except UnicodeDecodeError:
                            continue
                    if not text:
                        continue
                    waypoint_count += self._kmz_metadata_service.count_waypoints_in_text(text)
                    for value in self._kmz_metadata_service.extract_name_like_fields_from_text(text):
                        if value not in discovered_names:
                            discovered_names.append(value)
                        if len(discovered_names) >= 20:
                            break

                lines.append(f"Waypoint count (Placemark): {waypoint_count}")

                if discovered_names:
                    preview = ", ".join(discovered_names[:10])
                    suffix = " ..." if len(discovered_names) > 10 else ""
                    lines.append(f"Name-like fields found in KMZ XML: {preview}{suffix}")
                else:
                    lines.append("No obvious name/title fields found inside KMZ XML files.")
                    lines.append("RC-2 edited mission display names are likely stored in DJI app index/database metadata.")

                if not deep:
                    lines.append(
                        "Quick inspect summary: display-name metadata is likely external to the KMZ. "
                        "Use Deep Inspect to search DJI metadata/index files."
                    )
                    return True, "\n".join(lines)

                lines.extend(inspect_metadata_candidates(mission, kmz_name))
                lines.extend(inspect_binary_candidates(mission, kmz_name))
                lines.append(
                    "Deep inspect summary: if the edited display name is still missing, it is likely "
                    "stored in a DJI binary database/index file outside the slot and outside the KMZ."
                )
        except zipfile.BadZipFile:
            return False, "Selected mission KMZ is not a valid zip archive."

        return True, "\n".join(lines)
