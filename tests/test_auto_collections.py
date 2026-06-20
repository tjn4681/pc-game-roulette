import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcgr.genres import build_genre_buckets, CURATED_GENRES


class TestBuildGenreBuckets(unittest.TestCase):
    def test_game_appears_in_every_curated_genre_it_matches(self):
        cache = {"10": ["Action", "Adventure"], "20": ["Action"]}
        out = build_genre_buckets(cache, [10, 20], min_size=1)
        by = {b["name"]: b for b in out}
        self.assertEqual(by["Action"]["appids"], [10, 20])
        self.assertEqual(by["Adventure"]["appids"], [10])

    def test_non_curated_genres_are_dropped(self):
        cache = {"10": ["Indie", "Early Access", "Free to Play"]}
        out = build_genre_buckets(cache, [10], min_size=1)
        self.assertEqual(out, [])

    def test_min_size_hides_small_buckets(self):
        cache = {"10": ["Racing"], "20": ["Action"], "30": ["Action"]}
        out = build_genre_buckets(cache, [10, 20, 30], min_size=2)
        names = [b["name"] for b in out]
        self.assertIn("Action", names)
        self.assertNotIn("Racing", names)

    def test_only_library_appids_counted_and_uncached_ignored(self):
        cache = {"10": ["Action"], "99": ["Action"]}
        out = build_genre_buckets(cache, [10, 50], min_size=1)
        self.assertEqual(out, [{"name": "Action", "count": 1, "appids": [10]}])

    def test_curated_list_excludes_noise(self):
        for junk in ("Indie", "Early Access", "Free to Play", "Gore", "Utilities"):
            self.assertNotIn(junk, CURATED_GENRES)


if __name__ == "__main__":
    unittest.main()
