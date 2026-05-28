from __future__ import annotations

import re


class KMZMetadataService:
    """KMZ metadata parsing and normalization helpers."""

    @staticmethod
    def extract_name_like_fields_from_text(text: str) -> list[str]:
        patterns = [
            r"<name>\s*([^<]{1,200})\s*</name>",
            r"<[^>]*missionName[^>]*>\s*([^<]{1,200})\s*</[^>]+>",
            r"<[^>]*title[^>]*>\s*([^<]{1,200})\s*</[^>]+>",
        ]
        found: list[str] = []
        for pattern in patterns:
            for match in re.findall(pattern, text, flags=re.IGNORECASE):
                value = (match or "").strip()
                if value and value not in found:
                    found.append(value)
                if len(found) >= 20:
                    return found
        return found

    @staticmethod
    def count_waypoints_in_text(text: str) -> int:
        # DJI KMZ route points are represented as Placemark entries in KML/WPML.
        return len(re.findall(r"<\s*Placemark\b", text, flags=re.IGNORECASE))
