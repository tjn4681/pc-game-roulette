"""
Auto-collections by genre and curated tag.

Owns the genre and tag caches plus a polite background warmer (modeled on
NameService): when asked for buckets it returns whatever's cached now and kicks
off resolution of any uncached library appids, so cards fill in progressively
over the first run and load instantly afterward.  Buckets are built by the pure
``pcgr.genres.build_genre_buckets`` / ``build_tag_buckets`` (curated lists +
minimum size).  One warm pass resolves both tracks (genres from Steam, tags from
SteamSpy).

Steam only for now (Phase A).  App-internal — never writes Steam data.
"""

import threading
import time

from pcgr.config import get_setting, set_setting
from pcgr.genres import build_genre_buckets, build_tag_buckets
from pcgr.sources.store import (
    fetch_genres, fetch_tags,
    load_genre_cache, merge_genre_cache,
    load_tag_cache, merge_tag_cache,
)


class GenreService:
    def __init__(self):
        self._warm_lock = threading.Lock()
        self._warm_thread = None

    # ── Enable flag ───────────────────────────────────────────────────────

    def is_enabled(self):
        return bool(get_setting("auto_collections_enabled", False))

    def set_enabled(self, enabled):
        set_setting("auto_collections_enabled", bool(enabled))
        return {"status": "ok", "enabled": bool(enabled)}

    # ── Buckets + progress ────────────────────────────────────────────────

    def get_buckets(self, appids):
        """Return genre cards followed by curated-tag cards for the given library
        appids, building from the caches and kicking off a background warm for
        any uncached genres/tags so the buckets fill in over time."""
        ids = self._ints(appids)
        genre_cache = load_genre_cache()
        tag_cache = load_tag_cache()
        self._warm(ids, genre_cache, tag_cache)
        collections = (build_genre_buckets(genre_cache, ids)
                       + build_tag_buckets(tag_cache, ids))
        return {"status": "ok", "collections": collections}

    def status(self, appids):
        """Progress for the 'Categorizing…' indicator.  A game counts as done
        only once it's in BOTH caches, so the bar doesn't read 100% while tags
        are still resolving."""
        ids = self._ints(appids)
        genre_cache = load_genre_cache()
        tag_cache = load_tag_cache()
        categorized = sum(1 for a in ids
                          if str(a) in genre_cache and str(a) in tag_cache)
        return {"status": "ok", "total": len(ids),
                "categorized": categorized, "pending": len(ids) - categorized}

    # ── Background warmer (both tracks) ───────────────────────────────────

    def _warm(self, appids, genre_cache, tag_cache):
        """Resolve missing genres (Steam) and tags (SteamSpy) for uncached appids
        on one daemon thread.  Polite (fixed delay per appid), flushes every 25
        hits per track so progress survives a crash, resumes across launches
        (caches persist).  One warmer at a time."""
        todo = [a for a in appids
                if str(a) not in genre_cache or str(a) not in tag_cache]
        if not todo:
            return
        with self._warm_lock:
            if self._warm_thread and self._warm_thread.is_alive():
                return

            def _worker():
                res_g, res_t = {}, {}
                for appid in todo:
                    key = str(appid)
                    if key not in genre_cache:
                        try:
                            g = fetch_genres(appid)
                        except Exception:
                            g = None
                        if g is not None:        # [] (no genres) is a real result
                            res_g[key] = g
                    if key not in tag_cache:
                        try:
                            t = fetch_tags(appid)
                        except Exception:
                            t = None
                        if t is not None:        # [] (no tags) is a real result
                            res_t[key] = t
                    if len(res_g) >= 25:
                        merge_genre_cache(res_g); res_g = {}
                    if len(res_t) >= 25:
                        merge_tag_cache(res_t); res_t = {}
                    time.sleep(0.20)             # ~5 req/s, gentle on the APIs
                if res_g:
                    merge_genre_cache(res_g)
                if res_t:
                    merge_tag_cache(res_t)

            t = threading.Thread(target=_worker, name="genre-tag-warmer", daemon=True)
            self._warm_thread = t
            t.start()

    @staticmethod
    def _ints(appids):
        out = []
        for a in (appids or []):
            try:
                out.append(int(a))
            except (TypeError, ValueError):
                pass
        return out
