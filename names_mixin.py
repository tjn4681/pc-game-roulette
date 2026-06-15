"""
NameWarmingMixin for SteamRouletteAPI.

Game-name resolution: the background name-warmer threads, Steam-name
status, on-demand appid->name lookups, and HowLongToBeat data.

Methods here run on the single js_api object via cooperative multiple
inheritance, so they share instance state (self.*) set up in the core
SteamRouletteAPI.__init__.
"""

import json
import os
import re
import steam_names
import threading
import time

from appconfig import CACHE_DIR, HLTB_CACHE
from steam_library import lookup_acf_name
from game_names import fetch_name_from_api, fetch_name_from_steamspy, load_name_cache, load_xplat_searched, save_name_cache, save_xplat_searched, search_steam_store
from game_titles import normalize_title, _hltb_search_variants


class NameWarmingMixin:
    # ── Background name warmer ────────────────────────────────────────────

    def _start_name_warmer(self, appids):
        """Resolve missing Steam app names on a background daemon thread.

        Runs entirely off the UI path so startup is never blocked, and works
        through the ENTIRE unresolved list in one pass so cross-platform
        duplicates (incl. obscure / uninstalled owned games) are caught on the
        next dedup computation.  Because the cache persists, this is a
        one-time cost per machine — once warm, later launches skip it.

        Politeness / resilience:
          • a small fixed delay between requests (gentle on Steam),
          • resolved names flushed to cache/names.json every 25 hits, so
            progress survives a crash or early exit,
          • a long run of empty results (which usually means Steam is
            rate-limiting, though it can also just be a cluster of nameless
            SDK / dedicated-server / delisted appids) triggers a cooldown
            pause — NOT a bail-out.  We never abandon the list; anything left
            unresolved is simply retried on the next launch.
        """
        with self._name_warmer_lock:
            if self._name_warmer_thread and self._name_warmer_thread.is_alive():
                return  # already warming
            todo = list(dict.fromkeys(appids))  # de-dupe, keep order

            def _worker():
                DELAY_SEC      = 0.20   # ~5 req/s — gentle on Steam's endpoints
                COOLDOWN_AFTER = 40     # consecutive empties before we cool off
                COOLDOWN_SEC   = 20.0   # pause length when likely rate-limited
                resolved = {}
                consec_empty = 0
                for appid in todo:
                    try:
                        n = (fetch_name_from_api(appid)
                             or fetch_name_from_steamspy(appid))
                    except Exception:
                        n = None
                    if n:
                        resolved[str(appid)] = n
                        consec_empty = 0
                    else:
                        # Empty is normal for delisted/tool appids — do NOT
                        # bail.  Only cool down if empties pile up (possible
                        # throttling), then keep going.
                        consec_empty += 1
                        if consec_empty >= COOLDOWN_AFTER:
                            if resolved:
                                self._merge_names_into_cache(resolved)
                                resolved = {}
                            time.sleep(COOLDOWN_SEC)
                            consec_empty = 0
                    # Flush periodically so progress isn't lost on a long run
                    if len(resolved) >= 25:
                        self._merge_names_into_cache(resolved)
                        resolved = {}
                    time.sleep(DELAY_SEC)
                if resolved:
                    self._merge_names_into_cache(resolved)

            t = threading.Thread(target=_worker, name="name-warmer", daemon=True)
            self._name_warmer_thread = t
            t.start()

    @staticmethod
    def _merge_names_into_cache(new_names):
        """Read-merge-write the on-disk name cache (atomic save handles
        locking).  Only adds keys that aren't already present."""
        if not new_names:
            return
        cache = load_name_cache()
        changed = False
        for k, v in new_names.items():
            if k not in cache and v:
                cache[k] = v
                changed = True
        if changed:
            save_name_cache(cache)

    # ── Bulk Steam names (SteamSpy) — non-blocking, stale-while-revalidate ──

    def _bulk_steam_names(self):
        """Return the bulk {appid: name} map WITHOUT ever blocking on the
        ~30s SteamSpy fetch.

        Fresh cache → use it.  Stale/missing → return whatever's on disk
        (possibly empty) and kick off a background refresh.  This keeps the
        loading-screen path responsive even when dedup is enabled and the
        weekly bulk cache has expired."""
        names = steam_names.load_cache(CACHE_DIR)
        if names:
            return names
        stale = steam_names.load_cache_any_age(CACHE_DIR)
        self._start_steam_bulk_warmer()
        return stale

    def _start_steam_bulk_warmer(self):
        """Fetch/refresh the SteamSpy bulk name cache on a background thread."""
        with self._steam_bulk_lock:
            if self._steam_bulk_thread and self._steam_bulk_thread.is_alive():
                return
            def _worker():
                try:
                    steam_names.get_steam_app_names(CACHE_DIR, force_refresh=True)
                except Exception:
                    pass
            t = threading.Thread(target=_worker, name="steam-bulk-warmer",
                                 daemon=True)
            self._steam_bulk_thread = t
            t.start()

    # ── Targeted cross-platform name resolver ─────────────────────────────

    def _start_xplat_name_resolver(self, candidate_names, owned_appids):
        """Resolve the Steam names needed for cross-platform dedup by searching
        the Steam store for each *unmatched* GOG/Epic title and caching the name
        of any returned appid the user actually owns.

        This is far faster to converge than the per-appid forward warmer: it's
        bounded by the GOG/Epic library (and only the titles not already
        matched), and it goes straight at the dedup-relevant games instead of
        resolving the entire Steam library.  Once a name lands in the cache the
        normal dedup pass catches the duplicate, with correct priority.

        Background daemon; polite (delay + cap)."""
        with self._xplat_lock:
            if self._xplat_thread and self._xplat_thread.is_alive():
                return
            todo  = list(dict.fromkeys(candidate_names))   # de-dupe, keep order
            owned = set(owned_appids)

            def _worker():
                DELAY_SEC = 0.30
                MAX_PER_RUN = 600
                resolved = {}
                existing = load_name_cache()
                searched = load_xplat_searched()
                searched_dirty = False
                done = 0
                for name in todo:
                    if done >= MAX_PER_RUN:
                        break
                    target = normalize_title(name)
                    if not target or target in searched:
                        continue   # blank, or already looked up recently
                    done += 1
                    try:
                        results = search_steam_store(name)
                    except Exception:
                        results = []
                    for aid, sname in results:
                        # Only cache an owned appid whose name actually matches —
                        # never pollute the cache with a wrong/unowned game.
                        if (aid in owned
                                and str(aid) not in existing
                                and normalize_title(sname) == target):
                            resolved[str(aid)] = sname
                    # Remember we searched this title so GOG-exclusive games
                    # aren't re-searched on every launch.
                    searched[target] = time.time()
                    searched_dirty = True
                    if len(resolved) >= 15:
                        self._merge_names_into_cache(resolved)
                        resolved = {}
                    if searched_dirty and done % 25 == 0:
                        save_xplat_searched(searched)
                        searched_dirty = False
                    time.sleep(DELAY_SEC)
                if resolved:
                    self._merge_names_into_cache(resolved)
                if searched_dirty:
                    save_xplat_searched(searched)

            t = threading.Thread(target=_worker, name="xplat-resolver",
                                 daemon=True)
            self._xplat_thread = t
            t.start()

    def get_steam_names_status(self):
        """Return whether the bulk SteamSpy name cache exists and how big it is.
        The frontend uses this to warn users that the first dedup computation
        will take ~30 seconds (the SteamSpy pages fetch sequentially)."""
        age = steam_names.cache_age_seconds(CACHE_DIR)
        bulk = steam_names.load_cache(CACHE_DIR)
        return {
            "status": "ok",
            "cached": len(bulk) > 0,
            "count":  len(bulk),
            "age_seconds": age,
            "ttl_seconds": steam_names.CACHE_TTL_SEC,
        }

    def refresh_steam_names(self):
        """Force-refresh the bulk Steam name cache (blocking, ~30 sec)."""
        names = steam_names.get_steam_app_names(CACHE_DIR, force_refresh=True)
        return {"status": "ok", "count": len(names)}

    # ── Game name resolution ──────────────────────────────────────────────

    def get_game_name(self, appid_str):
        """
        Resolve a game's name. Resolution order:
          1. Name cache (instant)
          2. Local appmanifest .acf file (instant, no network)
          3. Steam store API (network, ~1-2 s)
        Result is cached to disk after any successful fetch.
        """
        try:
            appid = int(appid_str)
        except (ValueError, TypeError):
            return {"status": "error", "name": f"App {appid_str}"}

        key   = str(appid)
        cache = load_name_cache()

        if key in cache:
            return {"status": "ok", "name": cache[key], "source": "cache",
                    "playtime_minutes": self._playtimes.get(appid, 0)}

        # Non-Steam shortcut?  Its real name lives in shortcuts.vdf — Steam's
        # store API would have no idea what to do with the synthetic appid.
        sc = self._shortcuts.get(appid)
        if sc and sc.get("name"):
            cache[key] = sc["name"]
            save_name_cache(cache)
            return {"status": "ok", "name": sc["name"], "source": "shortcut",
                    "playtime_minutes": self._playtimes.get(appid, 0)}

        # Try local .acf first (free, instant)
        if self._steam_path:
            name = lookup_acf_name(self._steam_path, appid)
            if name:
                cache[key] = name
                save_name_cache(cache)
                return {"status": "ok", "name": name, "source": "acf",
                        "playtime_minutes": self._playtimes.get(appid, 0)}

        # Try Steam store API, then SteamSpy for old/delisted games
        name = fetch_name_from_api(appid) or fetch_name_from_steamspy(appid)
        if not name:
            name = f"App {appid}"

        cache[key] = name
        save_name_cache(cache)
        return {"status": "ok", "name": name, "source": "api",
                "playtime_minutes": self._playtimes.get(appid, 0)}

    # ── HowLongToBeat completion times ───────────────────────────────────

    # Symbols Steam appends that confuse HLTB's similarity scorer
    _TRADEMARK_RE = re.compile(r'[™®©℠]')
    # Common subtitle separators — everything after these is stripped for the
    # fallback search term (e.g. "Game Name: Subtitle" → "Game Name")
    _SUBTITLE_RE  = re.compile(r'\s+[-–—:]\s+|\s+\(')

    def get_hltb_data(self, appid_str, game_name):
        """
        Fetch HowLongToBeat completion times for a game.
        Requires: pip install howlongtobeatpy
        Returns main_story, main_extra, completionist hours (floats).
        Successful results are cached to cache/hltb.json.
        Failures are NOT cached so that transient errors (network, stale API
        key, name mismatch) are automatically retried next spin.
        """
        # Accept any non-empty string as the cache key — works for both Steam
        # appids (plain numeric strings) and GOG / Epic ids like "gog_1207659146"
        # or "epic_Hades" so that all three platforms share the same cache file.
        key = str(appid_str).strip()
        if not key:
            return {"status": "error", "message": "Invalid id"}

        game_name = (game_name or "").strip()
        if not game_name or game_name.startswith("App "):
            return {"status": "not_found"}

        # Strip ™ ® © etc. that trip up HLTB's string similarity
        clean_name = self._TRADEMARK_RE.sub('', game_name)
        clean_name = ' '.join(clean_name.split())   # normalise whitespace

        # Load cache — only positive (dict) hits are stored; None entries are
        # legacy stale failures that should be retried, so we ignore them.
        cache = {}
        try:
            if os.path.isfile(HLTB_CACHE):
                with open(HLTB_CACHE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
        except Exception:
            pass

        hit = cache.get(key)
        if isinstance(hit, dict):           # valid cached result
            return {"status": "ok", **hit}
        # (None / missing → fall through and search)

        # Build extended search terms: original name plus Arabic↔Roman numeral
        # and &↔"and" variants so e.g. "Might & Magic 6" (GOG) finds
        # "Might & Magic VI" on HLTB.
        search_terms = _hltb_search_variants(clean_name, self._SUBTITLE_RE)

        try:
            from howlongtobeatpy import HowLongToBeat
            hltb = HowLongToBeat()
        except ImportError:
            return {"status": "unavailable"}

        def _best_result(term, threshold):
            try:
                results = hltb.search(term)
            except Exception:
                return None
            if not results:
                return None
            candidate = max(results, key=lambda r: r.similarity)
            return candidate if candidate.similarity >= threshold else None

        # Try the primary term at 0.55; fall back through all variants at 0.50.
        best = None
        for i, term in enumerate(search_terms):
            threshold = 0.55 if i == 0 else 0.50
            best = _best_result(term, threshold)
            if best is not None:
                break

        if best is None:
            return {"status": "not_found"}   # not cached — will retry next time

        def safe_hours(v):
            if v is None or (isinstance(v, (int, float)) and v <= 0):
                return None
            return float(v)

        data = {
            "matched_name": best.game_name,
            "main_story":    safe_hours(best.main_story),
            "main_extra":    safe_hours(best.main_extra),
            "completionist": safe_hours(best.completionist),
        }
        # Only cache successes — failures stay uncached for automatic retry
        cache[key] = data
        self._write_hltb_cache(cache)
        return {"status": "ok", **data}

    def _write_hltb_cache(self, cache):
        try:
            with open(HLTB_CACHE, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2)
        except Exception:
            pass
