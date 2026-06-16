"""
Duplicate / edition filtering.

Computes which game ids the frontend should hide: cross-platform duplicates
(the same game owned on a higher-priority launcher) and same-platform edition
variants (Mass Effect vs Mass Effect Legendary Edition).  This is inherently a
cross-launcher concern, so the service is composed with references to the
launchers it reads (steam/gog/epic) and the name service that warms the Steam
names dedup needs.
"""

from pcgr.config import CACHE_DIR, get_setting, set_setting
from pcgr.platforms import PLATFORMS, _GOG_INTEGRATED_PREFIXES
from pcgr.sources.steam_files import scan_all_acf
from pcgr.sources.store import load_name_cache, load_xplat_searched
from pcgr.sources import epic_auth, steam_api
from pcgr.titles import normalize_title
from pcgr.dedup import find_cross_platform_duplicates, find_same_platform_edition_dupes


class FilterService:
    def __init__(self, steam, gog, epic, names):
        self.steam = steam
        self.gog = gog
        self.epic = epic
        self.names = names

    # ── Cross-platform duplicate settings ─────────────────────────────────

    def get_dedup_settings(self):
        """Return the user's cross-platform duplicate hide settings."""
        # Default priority follows the registry order — Steam first, then GOG,
        # Epic, Battle.net.  Users can reorder in Settings.
        default_priority = sorted(
            PLATFORMS.keys(),
            key=lambda p: PLATFORMS[p]["default_priority"],
        )
        stored = get_setting("platform_priority", None)
        if not isinstance(stored, list):
            stored = default_priority
        else:
            # Top up with any newly-supported launchers the user's saved
            # priority predates (e.g. they had a priority saved before we
            # added Battle.net) — append missing ones at the end.
            for p in default_priority:
                if p not in stored:
                    stored.append(p)
        return {
            "status":   "ok",
            "enabled":  bool(get_setting("hide_duplicates", False)),
            "priority": [p for p in stored if p in PLATFORMS],
        }

    def set_dedup_settings(self, enabled, priority):
        """Persist the dedup toggle and priority order."""
        if not isinstance(priority, list):
            return {"status": "error", "message": "priority must be a list"}
        # Sanitise: keep only known platforms, dedupe, and ensure every
        # supported platform appears at least once.
        seen = []
        for p in priority:
            if p in PLATFORMS and p not in seen:
                seen.append(p)
        for p in sorted(PLATFORMS.keys(),
                        key=lambda x: PLATFORMS[x]["default_priority"]):
            if p not in seen:
                seen.append(p)
        set_setting("hide_duplicates", bool(enabled))
        set_setting("platform_priority", seen)
        return self.get_dedup_settings()

    # ── Edition preference (newer/enhanced vs original) ──────────────────

    def get_edition_preference(self):
        """Return the user's preference for same-game edition variants:
            'both'     — show everything (default)
            'enhanced' — hide originals when an enhanced edition exists
            'original' — hide enhanced editions when an original exists
        """
        pref = get_setting("edition_preference", "both")
        if pref not in ("both", "enhanced", "original"):
            pref = "both"
        return {"status": "ok", "preference": pref}

    def set_edition_preference(self, preference):
        if preference not in ("both", "enhanced", "original"):
            return {"status": "error",
                    "message": "preference must be 'both', 'enhanced', or 'original'"}
        set_setting("edition_preference", preference)
        return {"status": "ok", "preference": preference}

    def apply_edition_preference(self, games):
        """Strip same-game duplicates within the given list per the user's
        edition_preference setting.  Safe to call on any platform's games."""
        pref = get_setting("edition_preference", "both")
        if pref not in ("enhanced", "original") or not games:
            return games
        hidden = find_same_platform_edition_dupes(games, pref)
        if not hidden:
            return games
        return [g for g in games if g.get("id") not in hidden]

    def get_edition_filter(self, _gog_games=None, _epic_games=None):
        """Same shape as get_duplicate_filter but for same-platform edition
        variants (e.g. Mass Effect vs Mass Effect Legendary Edition).  Returns
        per-platform game IDs to hide based on the edition_preference setting.

        `_gog_games` / `_epic_games`: see get_duplicate_filter — let
        get_all_filters() share one library fetch across both filters.

        Disabled (preference='both') returns all empty lists."""
        pref = get_setting("edition_preference", "both")
        empty = {"status": "ok", "preference": pref}
        for pid in PLATFORMS:
            empty[pid] = []
        empty["counts"] = {f"{pid}_hidden": 0 for pid in PLATFORMS}
        if pref not in ("enhanced", "original"):
            return empty

        # Steam — synthesise game dicts using cached names (lazy + bulk).
        # Non-blocking bulk lookup (stale-while-revalidate) so this never
        # stalls the UI on the SteamSpy fetch.
        steam_appids = set()
        for ids in (self.steam.collections or {}).values():
            steam_appids.update(ids)
        name_cache = load_name_cache()
        bulk_names = self.names.bulk_steam_names()
        def _name_for(a):
            return name_cache.get(str(a)) or bulk_names.get(str(a)) or ""
        steam_games = [
            {"id": f"steam_{a}", "raw_id": a, "platform": "steam",
             "name": _name_for(a)}
            for a in steam_appids
            if _name_for(a)
        ]

        per_platform_games = {
            "steam": steam_games,
            "gog":   (_gog_games if _gog_games is not None
                      else self.gog.get_games().get("games", [])),
            "epic":  (_epic_games if _epic_games is not None
                      else self.epic.get_games().get("games", [])),
        }

        out = {"status": "ok", "preference": pref}
        counts = {}
        for pid, games in per_platform_games.items():
            hidden = find_same_platform_edition_dupes(games, pref)
            out[pid] = list(hidden)
            counts[f"{pid}_hidden"] = len(hidden)
        out["counts"] = counts
        return out

    def get_duplicate_filter(self, _gog_games=None, _epic_games=None):
        """Load every owned game across Steam / GOG / Epic (+ integrated
        launchers via GOG Galaxy), compute which IDs should be hidden based on
        the priority order, and return per-platform exclude-ID lists.

        `_gog_games` / `_epic_games` let get_all_filters() pass libraries it has
        already fetched, so dedup + edition filtering don't each re-query GOG /
        Epic.  Omitted (the js_api call) → fetched here as before.

        Integrated launchers (battlenet, origin, uplay) are always lowest
        priority — they lose to any launcher in the user's priority list.
        Returns empty lists if dedup is disabled."""
        settings = self.get_dedup_settings()
        all_platform_ids = list(PLATFORMS) + list(_GOG_INTEGRATED_PREFIXES)
        empty = {pid: [] for pid in all_platform_ids}
        if not settings["enabled"]:
            return {"status": "ok", **empty, "counts": {}}

        priority = settings["priority"]

        # Steam: synthesise game dicts from the cached + bulk name pool.
        # Source the appids from BOTH custom collections AND installed
        # appmanifest .acf files — otherwise an owned-but-uncategorised
        # Steam game (no custom collection) is invisible to the dedup
        # bucket and its GOG/Epic twin slips through.
        steam_appids = set()
        for ids in (self.steam.collections or {}).values():
            steam_appids.update(ids)

        installed_steam = {}
        steam_path = self.steam.steam_path
        if steam_path:
            try:
                installed_steam = scan_all_acf(steam_path)
            except Exception:
                installed_steam = {}
        steam_appids.update(installed_steam.keys())

        # Full owned library via the optional Steam API key (comes with names)
        # — folds owned-but-uninstalled games into the dedup pool.  Read from
        # cache ONLY here: this runs on the loading-screen path, so we never
        # block on the network (the frontend's get_steam_owned_games() does the
        # actual fetch and warms this cache).
        owned_names = {}
        _key = epic_auth.load_secret(CACHE_DIR, "steam_api_key")
        if _key:
            _sid = self.steam._steamid64()
            _cached = steam_api.load_cache(CACHE_DIR, _sid) if _sid else None
            if _cached and _cached.get("status") == "ok":
                for g in _cached.get("games", []):
                    owned_names[g["appid"]] = g["name"]
                steam_appids.update(owned_names.keys())

        name_cache = load_name_cache()
        bulk_names = self.names.bulk_steam_names()

        # Resolve names from LOCAL sources only — cache, the SteamSpy bulk
        # dump (~30k titles), and installed appmanifests.  We deliberately do
        # NOT hit the network here: this method runs on the loading-screen
        # path, and a large library can have >1000 uncached appids — fetching
        # each one synchronously (8s timeout apiece, plus rate-limiting) would
        # freeze startup for many minutes.
        #
        # Any appid still unnamed is handed to a background warmer (below),
        # which fills the cache politely so it's caught on the next dedup pass.
        def _local_name(appid):
            key = str(appid)
            return (name_cache.get(key)
                    or bulk_names.get(key)
                    or installed_steam.get(appid, "")
                    or owned_names.get(appid, "")
                    or "")

        steam_games = []
        unresolved = []
        for a in steam_appids:
            n = _local_name(a)
            if n:
                steam_games.append(
                    {"id": f"steam_{a}", "raw_id": a, "platform": "steam",
                     "name": n})
            else:
                unresolved.append(a)

        # Warm the missing names in the background (non-blocking).  Catches
        # obscure / uninstalled owned games (e.g. recent re-releases not yet in
        # the SteamSpy dump) on a later dedup computation without ever
        # stalling the UI.
        if unresolved:
            self.names.start_name_warmer(unresolved)

        # Split GOG result by platform so integrated launchers are deduped
        # independently (not lumped under the 'gog' bucket)
        gog_all    = (_gog_games if _gog_games is not None
                      else self.gog.get_games().get("games", []))
        epic_list  = (_epic_games if _epic_games is not None
                      else self.epic.get_games().get("games", []))
        gog_native = [g for g in gog_all if g["platform"] == "gog"]
        bnet_games = [g for g in gog_all if g["platform"] == "battlenet"]
        orig_games = [g for g in gog_all if g["platform"] == "origin"]
        uply_games = [g for g in gog_all if g["platform"] == "uplay"]

        excludes = find_cross_platform_duplicates({
            "steam":     steam_games,
            "gog":       gog_native,
            "epic":      epic_list,
            "battlenet": bnet_games,
            "origin":    orig_games,
            "uplay":     uply_games,
        }, priority)

        # Targeted cross-platform resolution: for every GOG/Epic title whose
        # normalized name doesn't already match a *resolved* Steam game, search
        # the Steam store by name in the background and cache the name of any
        # owned appid that matches.  This catches owned-but-unresolved Steam
        # twins (e.g. recent re-releases not in the SteamSpy dump) in roughly
        # one pass — bounded by the GOG/Epic library — instead of waiting for
        # the per-appid warmer to crawl the whole Steam library.
        steam_norms = {normalize_title(g["name"]) for g in steam_games if g.get("name")}
        already_searched = load_xplat_searched()
        xplat_candidates = []
        seen_norm = set()
        for g in (gog_native + bnet_games + orig_games + uply_games + epic_list):
            nm = g.get("name")
            if not nm:
                continue
            n = normalize_title(nm)
            if (n and n not in steam_norms and n not in seen_norm
                    and n not in already_searched):
                seen_norm.add(n)
                xplat_candidates.append(nm)
        if xplat_candidates:
            self.names.start_xplat_name_resolver(xplat_candidates, steam_appids)

        out = {"status": "ok"}
        counts = {}
        for pid in all_platform_ids:
            out[pid] = excludes.get(pid, [])
            counts[f"{pid}_hidden"] = len(out[pid])
        out["counts"] = counts
        return out

    def get_all_filters(self):
        """Compute cross-platform dedup AND same-platform edition excludes,
        fetching each platform's library only once and sharing it between the
        two filters.  The frontend calls this instead of get_duplicate_filter +
        get_edition_filter separately (which each re-queried GOG / Epic)."""
        dedup_on   = self.get_dedup_settings()["enabled"]
        edition_on = get_setting("edition_preference", "both") in ("enhanced", "original")
        # Only fetch the GOG / Epic libraries if a filter actually needs them.
        # When both are off (the default), this avoids a potentially slow
        # library fetch — e.g. a cold Epic OAuth call — on the loading path.
        if dedup_on or edition_on:
            gog  = self.gog.get_games().get("games", [])
            epic = self.epic.get_games().get("games", [])
        else:
            gog, epic = [], []
        return {
            "status":  "ok",
            "dedup":   self.get_duplicate_filter(_gog_games=gog, _epic_games=epic),
            "edition": self.get_edition_filter(_gog_games=gog, _epic_games=epic),
        }
