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


from unittest import mock
from pcgr.services.filters import FilterService


class _StubSteam:
    def __init__(self, playtimes):
        self.playtimes = playtimes


class _StubLibrary:
    def __init__(self, games):
        self._games = games
    def get_games(self):
        return {"status": "ok", "games": self._games}


class TestPlaytimeFilterService(unittest.TestCase):
    def _service(self, playtimes, gog_games, epic_games):
        return FilterService(
            steam=_StubSteam(playtimes),
            gog=_StubLibrary(gog_games),
            epic=_StubLibrary(epic_games),
            names=None,
        )

    def test_disabled_returns_empty(self):
        svc = self._service({123: 9999}, [], [])
        with mock.patch("pcgr.services.filters.get_setting") as gs:
            gs.side_effect = lambda k, d=None: {"playtime_filter_enabled": False}.get(k, d)
            out = svc.get_playtime_filter()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["steam"], [])

    def test_enabled_filters_each_platform(self):
        svc = self._service(
            playtimes={123: 200 * 60, 456: 10},  # 123 = 200h, 456 = 10min
            gog_games=[{"id": "gog_1", "platform": "gog", "playtime_minutes": 5000}],
            epic_games=[{"id": "epic_1", "platform": "epic", "playtime_minutes": 30}],
        )
        settings = {"playtime_filter_enabled": True, "playtime_max_hours": 50}
        with mock.patch("pcgr.services.filters.get_setting") as gs:
            gs.side_effect = lambda k, d=None: settings.get(k, d)
            out = svc.get_playtime_filter()
        self.assertEqual(out["steam"], ["steam_123"])  # 200h > 50h
        self.assertEqual(out["gog"], ["gog_1"])         # ~83h > 50h
        self.assertEqual(out["epic"], [])               # 30min kept


if __name__ == "__main__":
    unittest.main()
