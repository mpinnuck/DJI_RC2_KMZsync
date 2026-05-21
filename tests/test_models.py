import unittest

from model.rc2_mission import RC2Mission
from model.kmz_file import KMZFile


class TestRC2Mission(unittest.TestCase):

    def _make(self, kmz_name="survey.kmz"):
        return RC2Mission(
            guid="abc-123-def",
            kmz_name=kmz_name,
            full_folder_path="/media/rc2/abc-123-def"
        )

    def test_is_empty_false_when_kmz_present(self):
        self.assertFalse(self._make("survey.kmz").is_empty)

    def test_is_empty_true_when_no_kmz(self):
        self.assertTrue(self._make("").is_empty)

    def test_display_kmz_name_returns_filename_when_present(self):
        self.assertEqual(self._make("survey.kmz").display_kmz_name, "survey.kmz")

    def test_display_kmz_name_returns_placeholder_when_empty(self):
        self.assertEqual(self._make("").display_kmz_name, "[Empty Slot]")

    def test_fields_accessible(self):
        m = self._make("grid.kmz")
        self.assertEqual(m.guid, "abc-123-def")
        self.assertEqual(m.full_folder_path, "/media/rc2/abc-123-def")


class TestKMZFile(unittest.TestCase):

    def test_fields_accessible(self):
        f = KMZFile(filename="orbit.kmz", full_path="/home/mark/missions/orbit.kmz")
        self.assertEqual(f.filename, "orbit.kmz")
        self.assertEqual(f.full_path, "/home/mark/missions/orbit.kmz")


if __name__ == "__main__":
    unittest.main()
