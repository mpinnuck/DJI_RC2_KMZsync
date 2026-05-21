from dataclasses import dataclass


@dataclass
class KMZFile:
    filename: str       # e.g. "survey_grid.kmz"
    full_path: str      # Absolute path to the file
