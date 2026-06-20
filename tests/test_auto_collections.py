import os
import sys
import unittest
from unittest import mock

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


class TestGenreCache(unittest.TestCase):
    def test_cache_round_trip_and_merge(self):
        import tempfile

        import pcgr.sources.store as store
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "genres.json")
            with mock.patch.object(store, "GENRES_CACHE", path):
                self.assertEqual(store.load_genre_cache(), {})
                store.save_genre_cache({"10": ["Action"]})
                self.assertEqual(store.load_genre_cache(), {"10": ["Action"]})
                # merge adds new keys, preserves existing
                store.merge_genre_cache({"20": ["RPG"], "10": ["IGNORED"]})
                got = store.load_genre_cache()
                self.assertEqual(got["20"], ["RPG"])
                self.assertEqual(got["10"], ["Action"])  # not overwritten


class TestGenreService(unittest.TestCase):
    def test_status_counts_cached_vs_pending(self):
        from pcgr.services.genres import GenreService
        svc = GenreService()
        with mock.patch("pcgr.services.genres.load_genre_cache",
                        return_value={"10": ["Action"], "20": []}):
            with mock.patch.object(svc, "_warm"):
                st = svc.status([10, 20, 30])
        self.assertEqual(st["total"], 3)
        self.assertEqual(st["categorized"], 2)
        self.assertEqual(st["pending"], 1)

    def test_get_buckets_uses_cache_and_curated_filter(self):
        from pcgr.services.genres import GenreService
        svc = GenreService()
        cache = {"10": ["Action", "Indie"], "20": ["Action"]}
        with mock.patch("pcgr.services.genres.load_genre_cache", return_value=cache):
            with mock.patch.object(svc, "_warm"):
                r = svc.get_buckets([10, 20])
        self.assertEqual(r["status"], "ok")
        names = [b["name"] for b in r["collections"]]
        self.assertEqual(names, ["Action"])
        self.assertEqual(r["collections"][0]["appids"], [10, 20])


if __name__ == "__main__":
    unittest.main()
