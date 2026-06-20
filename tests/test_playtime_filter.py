import os
import sys
import unittest

# Make the repo root importable so `import pcgr...` works when run from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcgr.services.filters import playtime_excludes


class TestPlaytimeExcludes(unittest.TestCase):
    def test_strictly_over_threshold_is_excluded(self):
        games = {"steam": [
            {"id": "steam_1", "playtime_minutes": 200},   # over 180 -> excluded
            {"id": "steam_2", "playtime_minutes": 180},   # equal     -> kept
            {"id": "steam_3", "playtime_minutes": 60},     # under     -> kept
        ]}
        out = playtime_excludes(games, 180)
        self.assertEqual(out["steam"], ["steam_1"])

    def test_zero_threshold_is_backlog_mode(self):
        games = {"gog": [
            {"id": "gog_a", "playtime_minutes": 1},        # any play   -> excluded
            {"id": "gog_b", "playtime_minutes": 0},        # never      -> kept
        ]}
        out = playtime_excludes(games, 0)
        self.assertEqual(out["gog"], ["gog_a"])

    def test_unknown_or_missing_playtime_is_kept(self):
        games = {"epic": [
            {"id": "epic_a"},                               # missing    -> kept
            {"id": "epic_b", "playtime_minutes": None},     # None       -> kept
            {"id": "epic_c", "playtime_minutes": 0},        # zero       -> kept
        ]}
        out = playtime_excludes(games, 60)
        self.assertEqual(out["epic"], [])

    def test_each_platform_handled_independently(self):
        games = {
            "steam": [{"id": "steam_1", "playtime_minutes": 999}],
            "gog":   [{"id": "gog_1",   "playtime_minutes": 10}],
        }
        out = playtime_excludes(games, 60)
        self.assertEqual(out, {"steam": ["steam_1"], "gog": []})


if __name__ == "__main__":
    unittest.main()
