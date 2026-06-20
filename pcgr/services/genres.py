"""
Auto-collections by genre.

Owns the genre cache and a polite background warmer (modeled on NameService):
when asked for buckets it returns whatever genres are cached now and kicks off
resolution of any uncached library appids, so cards fill in progressively over
the first run and load instantly afterward.  Buckets are built by the pure
``pcgr.genres.build_genre_buckets`` (curated allowlist + minimum size).

Steam only for now (Phase A).  App-internal — never writes Steam data.
"""

import threading
import time

from pcgr.config import get_setting, set_setting
from pcgr.genres import build_genre_buckets
from pcgr.sources.store import fetch_genres, load_genre_cache, merge_genre_cache


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
        """Return genre collection cards for the given library appids, building
        from cached genres and kicking off a background warm for any uncached
        ones so the buckets fill in over time."""
        ids = self._ints(appids)
        cache = load_genre_cache()
        self._warm(ids, cache)
        return {"status": "ok", "collections": build_genre_buckets(cache, ids)}

    def status(self, appids):
        """Progress for the 'Categorizing…' indicator."""
        ids = self._ints(appids)
        cache = load_genre_cache()
        categorized = sum(1 for a in ids if str(a) in cache)
        return {"status": "ok", "total": len(ids),
                "categorized": categorized, "pending": len(ids) - categorized}

    # ── Background warmer ─────────────────────────────────────────────────

    def _warm(self, appids, cache):
        """Resolve genres for uncached appids on a daemon thread.  Polite
        (fixed delay), flushes every 25 hits so progress survives a crash, and
        resumes across launches (cache persists).  One warmer at a time."""
        todo = [a for a in appids if str(a) not in cache]
        if not todo:
            return
        with self._warm_lock:
            if self._warm_thread and self._warm_thread.is_alive():
                return

            def _worker():
                resolved = {}
                for appid in todo:
                    try:
                        g = fetch_genres(appid)
                    except Exception:
                        g = None
                    if g is not None:            # [] (no genres) is a real result
                        resolved[str(appid)] = g
                    if len(resolved) >= 25:
                        merge_genre_cache(resolved)
                        resolved = {}
                    time.sleep(0.20)             # ~5 req/s, gentle on Steam
                if resolved:
                    merge_genre_cache(resolved)

            t = threading.Thread(target=_worker, name="genre-warmer", daemon=True)
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
