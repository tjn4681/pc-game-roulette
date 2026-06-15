"""
PC Game Roulette backend — Steam / GOG / Epic detection, collections parsing,
name fetching, cross-platform dedup, OAuth, config.

The class kept its historical SteamRouletteAPI name because it's referenced
by main.py and any future migrations would just churn diffs.

Exposed to the frontend via js_api in main.py.
"""

import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import winreg

import epic_auth
import epic_api
import retroarch
import steam_api
import steam_names


# ── Relocated helpers ─────────────────────────────────────────────────────
#
# What used to be ~1200 lines of module-level helpers now lives in focused
# modules, one concern each.  They're imported here (not re-implemented) so the
# js_api methods below keep calling them by bare name, and so anything that
# imports names like CACHE_DIR from backend keeps working unchanged.

from appconfig import (
    CACHE_DIR, ART_CACHE_DIR, HLTB_CACHE, EPIC_LIB_CACHE,
    EPIC_LIB_CACHE_TTL_SECONDS, GOG_GALAXY_DB, _PROGRAM_DATA,
    load_config, save_config, get_setting, set_setting,
)
from steam_library import (
    find_steam_path, find_collections_files, parse_collections,
    parse_shortcuts_vdf, find_shortcuts_vdf_for, merge_shortcuts_into_collections,
    lookup_acf_name, scan_all_acf, parse_playtimes_from_localconfig,
)
from game_names import (
    fetch_name_from_api, search_steam_store, fetch_name_from_steamspy,
    load_name_cache, save_name_cache, load_xplat_searched, save_xplat_searched,
)
from images import (
    _read_image_as_data_url, _find_grid_images, _try_fetch_image, _cache_and_return,
)
from galaxy import (
    _galaxy_db_open, apply_galaxy_enrichment, query_galaxy_db_for_platform,
)
from game_titles import normalize_title, _hltb_search_variants
from dedup import find_cross_platform_duplicates, find_same_platform_edition_dupes


# ── Platform registry ─────────────────────────────────────────────────────
#
# Single source of truth for which launchers we support and the metadata each
# one needs across the rest of the codebase.  Adding a new launcher should
# only require: adding an entry here, adding its detector + games getter, and
# (in the frontend) adding the tab markup + a brand color in CSS.
#
# Note: only describes *metadata*.  Detection and fetching live in dedicated
# methods on SteamRouletteAPI because each launcher has unique quirks.

PLATFORMS = {
    "steam": {"id": "steam", "name": "Steam",
              "galaxy_prefix": "steam",
              "default_priority": 0},
    "gog":   {"id": "gog",   "name": "GOG",
              "galaxy_prefix": "gog",
              "default_priority": 1},
    "epic":  {"id": "epic",  "name": "Epic Games",
              "galaxy_prefix": "epic",
              "default_priority": 2},
}

# Launchers that are NOT first-class tabs but whose games appear in the GOG
# tab when the user has integrated them in GOG Galaxy.
_GOG_INTEGRATED_PREFIXES = ("battlenet", "origin", "uplay")

# Battle.net URL-protocol codes (used when launching via the GOG Galaxy URI).
# Galaxy stores internal codenames; the protocol expects marketing codes.
BATTLENET_LAUNCH_CODES = {
    "wow":         "WoW",   "wow_classic": "WoWC",
    "d2":          "D2",    "d2LOD":       "D2",
    "osi":         "OSI",   "diablo3":     "D3",
    "fenrispup":   "Fen",   "fenris":      "Fen",
    "prometheus":  "Pro",   "s1":          "S1",
    "s2":          "S2",    "w3":          "W3",
    "w3tft":       "War3",  "heroes":      "Hero",
    "hs_beta":     "WTCG",  "odin":        "ODIN",
    "zeus":        "ZEUS",  "fore":        "FORE",
    "lazr":        "LAZR",  "viper":       "VIPER",
}


def find_epic_manifests_dir():
    """Locate Epic Games Launcher's Manifests folder.

    Epic helpfully stores its AppDataPath under
      HKLM\\SOFTWARE\\WOW6432Node\\Epic Games\\EpicGamesLauncher\\AppDataPath
    so we consult the registry first (works even if Epic is installed to a
    non-default drive).  Falls back to the standard %ProgramData% location."""
    candidates = []
    for hive, subkey in [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Epic Games\EpicGamesLauncher"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Epic Games\EpicGamesLauncher"),
    ]:
        try:
            with winreg.OpenKey(hive, subkey) as k:
                val, _ = winreg.QueryValueEx(k, "AppDataPath")
                if val:
                    candidates.append(os.path.join(val, "Manifests"))
        except OSError:
            continue
    # Fallback: %ProgramData%\Epic\EpicGamesLauncher\Data\Manifests
    candidates.append(os.path.join(
        _PROGRAM_DATA, "Epic", "EpicGamesLauncher", "Data", "Manifests",
    ))
    for p in candidates:
        if os.path.isdir(p):
            return p
    return None

# ── js_api class ──────────────────────────────────────────────────────────────

class SteamRouletteAPI:
    """Methods callable from JavaScript via window.pywebview.api.*"""

    def __init__(self):
        self._collections_path = None
        self._collections = {}
        self._shortcuts   = {}   # {appid: {'name': str, 'tags': [str,...]}}
        self._playtimes   = {}   # {appid: minutes_played} from localconfig.vdf
        self._steam_path  = find_steam_path()
        self._window      = None
        self._name_warmer_lock    = threading.Lock()
        self._name_warmer_thread  = None
        self._steam_bulk_lock     = threading.Lock()
        self._steam_bulk_thread   = None
        self._xplat_lock          = threading.Lock()
        self._xplat_thread        = None
        # RetroArch (lazy): install dir + parsed playlists + id->game index
        self._retroarch_dir       = None
        self._ra_dir_checked      = False  # True once we've scanned (found or not)
        self._ra_playlists        = None   # list of {name, system, count, games}
        self._ra_index            = None   # {game_id: game dict}
        self._ra_art_port         = None   # local boxart HTTP server port
        self._ra_art_lock         = threading.Lock()

    def set_window(self, window):
        self._window = window

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

    # ── Collection loading ────────────────────────────────────────────────

    def auto_load(self):
        """
        Auto-detect and load collections.
        Returns {"status": "ok", "collections": [...], "path": "..."}
             or {"status": "notfound", "message": "..."}
        """
        cfg = load_config()
        saved = cfg.get("collections_path")
        if saved and os.path.isfile(saved):
            return self._load_from_path(saved)

        if not self._steam_path:
            return {"status": "notfound", "message": "Steam installation not found."}

        accounts = find_collections_files(self._steam_path)

        if not accounts:
            return {"status": "notfound", "message": "No collections file found under Steam userdata."}

        if len(accounts) == 1:
            return self._load_from_path(accounts[0][1])

        # Multiple accounts: pick the one with the most collections.
        # Ties broken by lower (older) account ID.
        def score(item):
            _, path = item
            try:
                count = len(parse_collections(path))
            except Exception:
                count = 0
            acct_id = item[0]
            lower_is_better = -int(acct_id) if acct_id.isdigit() else 0
            return (count, lower_is_better)

        best = max(accounts, key=score)
        return self._load_from_path(best[1])

    def select_account(self, path):
        """User manually picked an account path."""
        return self._load_from_path(path)

    def reload_collections(self):
        """Re-parse the cached collections file.  Use this after adding games or
        editing collections in Steam — the JSON is rewritten by Steam Cloud
        Sync but the app only parses it on startup."""
        if not self._collections_path or not os.path.isfile(self._collections_path):
            return {"status": "error", "message": "No collections file loaded yet."}
        return self._load_from_path(self._collections_path)

    def debug_shortcuts(self):
        """Inspect shortcuts.vdf: does it exist, how many shortcuts, what tags?"""
        if not self._collections_path:
            return {"status": "error", "message": "No collections loaded yet."}

        config_dir = os.path.dirname(os.path.dirname(self._collections_path))
        vdf_path   = os.path.join(config_dir, "shortcuts.vdf")
        result = {
            "status":           "ok",
            "vdf_path":         vdf_path,
            "vdf_exists":       os.path.isfile(vdf_path),
            "collection_names": sorted(self._collections.keys()),
        }
        if not result["vdf_exists"]:
            return result

        try:
            with open(vdf_path, "rb") as f:
                file_size = len(f.read())
            result["vdf_size_bytes"] = file_size
        except OSError as e:
            result["read_error"] = str(e)
            return result

        shortcuts = parse_shortcuts_vdf(vdf_path)
        result["total_shortcuts"] = len(shortcuts)
        result["shortcuts"] = [
            {
                "appid": sc.get("appid"),
                "name":  (sc.get("name") or "")[:60],
                "tags":  sc.get("tags", []),
            }
            for sc in shortcuts[:30]
        ]

        all_tags = set()
        for sc in shortcuts:
            for t in sc.get("tags", []):
                all_tags.add(t)
        coll_lower = {n.lower() for n in self._collections.keys()}
        result["unique_tags"]               = sorted(all_tags)
        result["tags_matching_collections"] = sorted(t for t in all_tags if t.strip().lower() in coll_lower)
        result["tags_not_matching"]         = sorted(t for t in all_tags if t.strip().lower() not in coll_lower)
        return result

    def debug_all_keys(self):
        """Comprehensive debug: prefix breakdown + searches for where Steam
        actually stores shortcut→collection memberships."""
        if not self._collections_path or not os.path.isfile(self._collections_path):
            return {"status": "error", "message": "No collections file loaded."}
        try:
            with open(self._collections_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return {"status": "error", "message": str(e)}

        # ── Top-level prefix breakdown ─────────────────────────────────────
        by_prefix = {}
        for entry in data:
            key = entry[0]
            for sep in ("-", "."):
                if sep in key:
                    prefix = key.split(sep, 1)[0]
                    break
            else:
                prefix = key
            by_prefix.setdefault(prefix, []).append(key)

        # ── user-* sub-prefix breakdown (where the answer likely lives) ────
        user_subtypes = {}
        for entry in data:
            key = entry[0]
            if not key.startswith("user-"):
                continue
            rest = key[5:]
            sep_pos = len(rest)
            for s in (".", "-"):
                p = rest.find(s)
                if 0 < p < sep_pos:
                    sep_pos = p
            sub = "user-" + rest[:sep_pos]
            user_subtypes.setdefault(sub, []).append(key)

        # ── Any key mentioning "shortcut" or "collection" (case-insensitive) ─
        notable_keys = sorted({
            entry[0] for entry in data
            if "shortcut" in entry[0].lower() or "collection" in entry[0].lower()
        })

        # ── Search the JSON for any reference to a non-Steam shortcut appid.
        #    Wherever we find them, that's where memberships are stored.
        #    Try both unsigned and signed representations since Steam isn't
        #    consistent about it across config files. ────────────────────────
        shortcut_id_strs = set()
        for a in self._shortcuts.keys():
            shortcut_id_strs.add(str(a))                   # unsigned
            if a > 0x7FFFFFFF:
                shortcut_id_strs.add(str(a - 0x100000000)) # signed equivalent
        search_matches = []
        if shortcut_id_strs:
            for entry in data:
                key = entry[0]
                value_str = json.dumps(entry[1])
                hits = [sid for sid in shortcut_id_strs if sid in value_str]
                if hits:
                    search_matches.append({
                        "key":         key,
                        "hit_count":   len(hits),
                        "hit_samples": hits[:5],
                        "preview":     value_str[:400],
                    })

        # ── List sibling files in cloudstorage/ (other namespaces?) ────────
        folder = os.path.dirname(self._collections_path)
        folder_files = []
        small_file_contents = {}
        try:
            for fname in sorted(os.listdir(folder)):
                p = os.path.join(folder, fname)
                if os.path.isfile(p):
                    size = os.path.getsize(p)
                    folder_files.append({"name": fname, "size": size})
                    # Dump tiny files — they're probably empty markers or registries
                    if 0 < size < 5000:
                        try:
                            with open(p, "r", encoding="utf-8", errors="replace") as fp:
                                small_file_contents[fname] = fp.read()[:1500]
                        except OSError:
                            pass
        except OSError:
            pass

        # ── Probe Steam's TEXT vdf config files (where the new Collections
        #    feature likely stores shortcut memberships) ──────────────────
        parts = os.path.normpath(self._collections_path).split(os.sep)
        account_dir = None
        try:
            ud_idx = parts.index("userdata")
            account_dir = os.sep.join(parts[: ud_idx + 2])
        except (ValueError, IndexError):
            pass

        # Build a collection_id → name map so we can locate each collection's
        # membership block inside localconfig.vdf by its unique uc-XXX id.
        coll_id_to_name = {}
        for entry in data:
            key = entry[0]
            if not key.startswith("user-collections.uc-"):
                continue
            meta = entry[1]
            if meta.get("is_deleted") or "value" not in meta:
                continue
            try:
                v = json.loads(meta["value"])
                cid   = v.get("id")
                cname = (v.get("name") or "").strip()
                if cid and cname:
                    coll_id_to_name[cid] = cname
            except Exception:
                continue

        config_probe = []
        if account_dir:
            probe_paths = [
                os.path.join(account_dir, "config", "localconfig.vdf"),
                os.path.join(account_dir, "config", "sharedconfig.vdf"),
                os.path.join(account_dir, "7",      "remote", "sharedconfig.vdf"),
            ]
            collection_names = list(self._collections.keys())
            for p in probe_paths:
                info = {"path": p, "exists": os.path.isfile(p)}
                if info["exists"]:
                    info["size"] = os.path.getsize(p)
                    try:
                        with open(p, "rb") as f:
                            raw = f.read()
                        text = raw.decode("utf-8", errors="replace")
                        id_hits = [s for s in shortcut_id_strs if s in text]
                        info["shortcut_id_hits"]     = len(id_hits)
                        info["sample_id_hits"]       = id_hits[:5]
                        info["collection_name_hits"] = [n for n in collection_names if n in text]

                        # Look up each collection's uc-id in the file and dump
                        # a generous window around it so we can see the VDF
                        # structure (key path + value format).
                        coll_ctx = []
                        for cid, cname in coll_id_to_name.items():
                            idx = text.find(cid)
                            if idx < 0:
                                continue
                            start = max(0, idx - 200)
                            end   = min(len(text), idx + 3000)
                            coll_ctx.append({
                                "id":      cid,
                                "name":    cname,
                                "offset":  idx,
                                "context": text[start:end],
                            })
                            if len(coll_ctx) >= 2:  # 2 examples is plenty
                                break
                        info["collection_id_contexts"] = coll_ctx

                        # And a much larger raw window around the first
                        # shortcut id hit as a fallback if uc-id lookup misses.
                        if id_hits:
                            first = text.find(id_hits[0])
                            info["context_around_first_hit"] = text[max(0, first - 1500):first + 3500]
                    except Exception as e:
                        info["error"] = str(e)
                config_probe.append(info)

        return {
            "status":         "ok",
            "total_entries":  len(data),
            "by_prefix":      {p: len(keys) for p, keys in by_prefix.items()},
            "user_subtypes":  {p: len(keys) for p, keys in user_subtypes.items()},
            "user_examples":  {p: keys[:3]  for p, keys in user_subtypes.items()},
            "notable_keys":   notable_keys[:50],
            "shortcut_id_search": {
                "shortcut_count":      len(shortcut_id_strs),
                "matching_entries":    len(search_matches),
                "matches":             search_matches[:15],
            },
            "cloudstorage_folder":     folder,
            "cloudstorage_dir_files":  folder_files,
            "small_cloudstorage_files": small_file_contents,
            "config_probe":            config_probe,
        }

    def debug_collection(self, name):
        """Return the raw structure of one collection so we can see how Steam
        is actually storing its entries.  Used to diagnose missing shortcuts."""
        if not self._collections_path or not os.path.isfile(self._collections_path):
            return {"status": "error", "message": "No collections file loaded."}
        try:
            with open(self._collections_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return {"status": "error", "message": str(e)}

        target = (name or "").strip().lower()
        for entry in data:
            key = entry[0]
            if not key.startswith("user-collections.uc-"):
                continue
            meta = entry[1]
            if meta.get("is_deleted") or "value" not in meta:
                continue
            try:
                value = json.loads(meta["value"])
            except (json.JSONDecodeError, KeyError):
                continue
            if value.get("name", "").strip().lower() != target:
                continue

            added = value.get("added", [])
            samples = [{"value": a, "type": type(a).__name__} for a in added[:30]]
            other_fields = {
                k: (v if not isinstance(v, (list, dict)) else f"<{type(v).__name__} len={len(v)}>")
                for k, v in value.items() if k != "added"
            }
            return {
                "status":        "ok",
                "name":          value.get("name"),
                "keys":          list(value.keys()),
                "added_count":   len(added),
                "added_samples": samples,
                "other_fields":  other_fields,
            }

        return {"status": "notfound", "message": f"Collection {name!r} not found."}

    def save_debug_log(self, content):
        """Open a Save As dialog and write the debug log to the chosen path."""
        if self._window is None:
            return {"status": "error", "message": "Window not ready."}
        result = self._window.create_file_dialog(
            dialog_type=30,  # SAVE_DIALOG
            save_filename="pc-game-roulette-debug.txt",
            file_types=("Text files (*.txt)", "All files (*.*)"),
        )
        if not result:
            return {"status": "cancelled"}
        path = result if isinstance(result, str) else result[0]
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"status": "ok", "path": path}
        except OSError as e:
            return {"status": "error", "message": str(e)}

    def browse_for_file(self):
        """Open a file dialog so the user can locate the collections file."""
        if self._window is None:
            return {"status": "error", "message": "Window not ready."}
        result = self._window.create_file_dialog(
            dialog_type=10,
            allow_multiple=False,
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        if not result:
            return {"status": "cancelled"}
        path = result[0]
        if not os.path.isfile(path):
            return {"status": "error", "message": f"File not found: {path}"}
        return self._load_from_path(path)

    def get_collections(self):
        return self._collections_as_list()

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

    # ── GOG and Epic library ──────────────────────────────────────────────

    def get_gog_games(self):
        """Return owned GOG games plus any games from launchers integrated in
        GOG Galaxy (Battle.net, EA App, Ubisoft Connect).

        Prefers the GOG Galaxy SQLite database which has the full owned
        library across all integrations; falls back to the Windows registry
        (installed GOG games only) if Galaxy isn't present or its DB isn't
        readable."""
        # Primary: Galaxy DB — GOG native + any integrated-launcher games
        galaxy = query_galaxy_db_for_platform("gog")
        for prefix in _GOG_INTEGRATED_PREFIXES:
            extras = query_galaxy_db_for_platform(prefix)
            galaxy.extend(extras)

        if galaxy:
            # Each game already has its own release-key id, so enrichment
            # resolves correctly for all platforms without a platform hint.
            apply_galaxy_enrichment(galaxy)
            galaxy = self._filter_platform_excluded(galaxy)
            galaxy.sort(key=lambda g: g["name"].lower())
            # Report 'galaxy' source; note whether any integrations are present
            has_integrated = any(
                g["platform"] in _GOG_INTEGRATED_PREFIXES for g in galaxy
            )
            return {"status": "ok", "games": galaxy, "source": "galaxy",
                    "has_integrated": has_integrated}

        # Fallback: registry-based detection (installed only)
        games = []
        reg_paths = [
            r"SOFTWARE\WOW6432Node\GOG.com\Games",
            r"SOFTWARE\GOG.com\Games",
        ]
        for reg_path in reg_paths:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as base:
                    i = 0
                    while True:
                        try:
                            subkey_name = winreg.EnumKey(base, i)
                            with winreg.OpenKey(base, subkey_name) as gk:
                                try:
                                    name    = winreg.QueryValueEx(gk, "gameName")[0]
                                    game_id = winreg.QueryValueEx(gk, "gameID")[0]
                                    if name and game_id:
                                        games.append({
                                            "id":       f"gog_{game_id}",
                                            "raw_id":   str(game_id),
                                            "name":     name,
                                            "platform": "gog",
                                            "source":   "registry",
                                        })
                                except OSError:
                                    pass
                            i += 1
                        except OSError:
                            break   # no more subkeys
            except OSError:
                continue            # try the other reg path
            if games:
                break               # found games in the first working path

        seen, unique = set(), []
        for g in games:
            if g["id"] not in seen:
                seen.add(g["id"])
                unique.append(g)
        filtered = self._filter_platform_excluded(
            sorted(unique, key=lambda g: g["name"].lower())
        )
        return {
            "status": "ok",
            "games": filtered,
            "source": "registry",
        }

    def _filter_platform_excluded(self, games):
        """Strip games the user has manually excluded (per-platform exclusion
        list keyed by full game id)."""
        if not games:
            return games
        cfg = load_config()
        excluded = set((cfg.get("excluded_platform_games") or {}).keys())
        if not excluded:
            return games
        return [g for g in games if g.get("id") not in excluded]

    # ── Epic dispatch ─────────────────────────────────────────────────────

    def get_epic_games(self, force_refresh=False):
        """Return owned Epic games.  Routes to one of three sources based on
        the `epic_source` setting:

            'oauth'  — direct from Epic's API (requires OAuth connection)
            'galaxy' — GOG Galaxy SQLite DB (Epic integration plugin)
            (auto-fallback to local manifests if both above yield nothing)

        force_refresh=True bypasses the on-disk OAuth library cache.
        """
        source = get_setting("epic_source", "galaxy")

        if source == "oauth":
            result = self._get_epic_games_oauth(force_refresh=force_refresh)
            if result["status"] == "ok" and result["games"]:
                result["games"] = self._filter_platform_excluded(result["games"])
                return result
            # If OAuth failed (e.g. refresh expired), bubble the auth status up
            # so the frontend can prompt the user to reconnect — don't silently
            # fall through to a different source.
            if result["status"] == "auth_required":
                return result
            # Empty result but no auth error: fall through to alternatives

        # 'galaxy' source, OR fallback when OAuth returned empty
        galaxy = query_galaxy_db_for_platform("epic")
        if galaxy:
            apply_galaxy_enrichment(galaxy, platform="epic")
            galaxy = self._filter_platform_excluded(galaxy)
            return {"status": "ok", "games": galaxy, "source": "galaxy"}

        # Final fallback: locally installed games via manifest folder
        result = self._get_epic_games_manifests()
        apply_galaxy_enrichment(result.get("games", []), platform="epic")
        result["games"] = self._filter_platform_excluded(result.get("games", []))
        return result

    # ── Epic sources ──────────────────────────────────────────────────────

    def _get_epic_games_oauth(self, force_refresh=False):
        """Fetch the user's full owned Epic library via Epic's REST API.
        Uses a 1-hour on-disk cache so reloading is instant; force_refresh
        bypasses the cache for the manual Reload button."""
        # Cache hit? (positive results only — failures shouldn't be cached)
        if not force_refresh and os.path.isfile(EPIC_LIB_CACHE):
            try:
                with open(EPIC_LIB_CACHE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                age = time.time() - cached.get("fetched_at", 0)
                if age < EPIC_LIB_CACHE_TTL_SECONDS and cached.get("games"):
                    # Re-enrich on every cache hit so playtime / tags stay fresh
                    # (Galaxy data can change between OAuth library refreshes)
                    games = cached["games"]
                    apply_galaxy_enrichment(games, platform="epic")
                    return {"status": "ok", "games": games,
                            "source": "oauth", "cached": True}
            except (json.JSONDecodeError, OSError):
                pass

        # Get a valid access token (refreshing silently if needed)
        tokens, status = epic_auth.get_valid_token(CACHE_DIR)
        if status != "ok" or not tokens:
            return {"status": "auth_required", "games": [], "source": "oauth",
                    "auth_status": status}

        try:
            games = epic_api.get_owned_games_with_titles(tokens["access_token"])
            # Hybrid: enrich OAuth library with Galaxy playtime/images/tags when
            # the user also has Galaxy installed.  This is the "best of both"
            # mode — full owned library from Epic, rich metadata from Galaxy.
            apply_galaxy_enrichment(games, platform="epic")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # Token rejected — wipe and ask user to reconnect
                epic_auth.clear_tokens(CACHE_DIR)
                return {"status": "auth_required", "games": [], "source": "oauth",
                        "auth_status": "token_rejected"}
            return {"status": "error", "games": [], "source": "oauth",
                    "message": f"Epic API HTTP {e.code}"}
        except Exception as e:
            return {"status": "error", "games": [], "source": "oauth",
                    "message": str(e)}

        # Write through to cache
        try:
            with open(EPIC_LIB_CACHE, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": time.time(), "games": games}, f, indent=2)
        except OSError:
            pass

        return {"status": "ok", "games": games, "source": "oauth"}

    def _get_epic_games_manifests(self):
        """Read the launcher's per-game manifest folder for installed Epic games."""
        manifests_path = find_epic_manifests_dir()
        if not manifests_path:
            return {"status": "ok", "games": [], "source": "none"}

        skip_kw = {
            "epic games launcher", "directx", "vcredist", "redistrib",
            "prerequisites", "unreal engine",
        }
        games = []
        try:
            for filename in os.listdir(manifests_path):
                if not filename.endswith(".item"):
                    continue
                try:
                    with open(os.path.join(manifests_path, filename),
                              "r", encoding="utf-8") as f:
                        data = json.load(f)
                    display_name = (data.get("DisplayName") or "").strip()
                    app_name     = (data.get("AppName")     or "").strip()
                    if not display_name or not app_name:
                        continue
                    if any(kw in display_name.lower() for kw in skip_kw):
                        continue
                    if not data.get("CatalogNamespace"):
                        continue
                    games.append({
                        "id":       f"epic_{app_name}",
                        "raw_id":   app_name,
                        "name":     display_name,
                        "platform": "epic",
                        "source":   "manifests",
                    })
                except Exception:
                    pass
        except OSError:
            pass

        seen, unique = set(), []
        for g in games:
            if g["id"] not in seen:
                seen.add(g["id"])
                unique.append(g)
        return {
            "status": "ok",
            "games": sorted(unique, key=lambda g: g["name"].lower()),
            "source": "manifests",
        }

    # ── Epic OAuth API (called from the Settings UI) ──────────────────────

    def get_epic_source(self):
        """Return the user's currently-selected Epic library source."""
        return {"status": "ok", "source": get_setting("epic_source", "galaxy")}

    def set_epic_source(self, source):
        """Persist the user's Epic library source choice."""
        if source not in ("galaxy", "oauth"):
            return {"status": "error", "message": f"Invalid source: {source}"}
        set_setting("epic_source", source)
        return {"status": "ok", "source": source}

    def get_epic_merge(self):
        """Whether Epic games should be folded into the GOG tab (instead of
        getting their own tab).  Useful for small Epic libraries."""
        return {"status": "ok", "enabled": bool(get_setting("merge_epic_into_gog", False))}

    def set_epic_merge(self, enabled):
        set_setting("merge_epic_into_gog", bool(enabled))
        return {"status": "ok", "enabled": bool(enabled)}

    def epic_oauth_url(self):
        """Return the Epic login URL the user opens in their browser."""
        return {"status": "ok", "url": epic_auth.get_login_url()}

    def epic_oauth_complete(self, code):
        """Exchange the pasted authorization code for tokens (one-time)."""
        if not code or not str(code).strip():
            return {"status": "error", "message": "No code provided"}
        try:
            info = epic_auth.complete_auth(CACHE_DIR, code)
            return {"status": "ok", **info}
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8")
            except Exception:
                body = ""
            return {"status": "error",
                    "message": f"HTTP {e.code}: {body[:300] or e.reason}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def epic_oauth_status(self):
        """Return current connection status for the Settings UI."""
        return {"status": "ok", **epic_auth.get_status(CACHE_DIR)}

    def epic_oauth_disconnect(self):
        """Wipe stored Epic tokens and clear the cached library."""
        epic_auth.clear_tokens(CACHE_DIR)
        try:
            if os.path.isfile(EPIC_LIB_CACHE):
                os.remove(EPIC_LIB_CACHE)
        except OSError:
            pass
        return {"status": "ok"}

    def get_galaxy_collections(self, platform):
        """Group the user's Galaxy tags into pseudo-collections for the given
        platform ('gog' or 'epic').  Returns the same structure as Steam
        collections so the existing collection-card grid renders them natively:

            [{ name: 'JRPG', count: 12, appids: ['epic_<id>', ...] }, ...]

        Empty list if Galaxy isn't installed or the user has no tags yet.
        Caller is expected to render these alongside (or instead of) the
        platform's "Full Library" card.
        """
        if platform not in ("gog", "epic", "battlenet", "origin", "uplay"):
            return {"status": "error", "message": "Invalid platform"}

        conn = _galaxy_db_open()
        if conn is None:
            return {"status": "ok", "collections": []}

        # When fetching for the GOG tab, include all integrated-launcher prefixes
        # so games tagged in GOG Galaxy (e.g. an EA game tagged "FPS") appear
        # in the GOG tag categories alongside native GOG games.
        if platform == "gog":
            prefixes = ("gog",) + _GOG_INTEGRATED_PREFIXES
        else:
            prefixes = (platform,)

        tag_to_keys = {}
        try:
            cur = conn.cursor()
            for prefix in prefixes:
                cur.execute(
                    "SELECT releaseKey, tag FROM UserReleaseTags "
                    "WHERE releaseKey LIKE ? AND tag IS NOT NULL",
                    (f"{prefix}_%",),
                )
                for release_key, tag in cur.fetchall():
                    if not tag:
                        continue
                    tag = tag.strip()
                    if not tag:
                        continue
                    tag_to_keys.setdefault(tag, []).append(release_key)
        except sqlite3.Error:
            pass
        finally:
            conn.close()

        collections = [
            {
                "name":   tag,
                "count":  len(keys),
                "appids": sorted(set(keys)),
            }
            for tag, keys in sorted(tag_to_keys.items(), key=lambda kv: kv[0].lower())
        ]
        return {"status": "ok", "collections": collections}

    # ── Cross-platform duplicate filtering ────────────────────────────────

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

    def _apply_edition_preference(self, games):
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
        for ids in (self._collections or {}).values():
            steam_appids.update(ids)
        name_cache = load_name_cache()
        bulk_names = self._bulk_steam_names()
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
                      else self.get_gog_games().get("games", [])),
            "epic":  (_epic_games if _epic_games is not None
                      else self.get_epic_games().get("games", [])),
        }

        out = {"status": "ok", "preference": pref}
        counts = {}
        for pid, games in per_platform_games.items():
            hidden = find_same_platform_edition_dupes(games, pref)
            out[pid] = list(hidden)
            counts[f"{pid}_hidden"] = len(hidden)
        out["counts"] = counts
        return out

    def detect_platforms(self):
        """Report which of the supported launchers we found on this PC.

        Used by the frontend to decide which tab to auto-select on a fresh
        install and to show a friendly empty state when nothing is detected.
        Cheap to call — only stats file paths and registry."""
        steam_path = self._steam_path or find_steam_path()
        galaxy_ok  = os.path.isfile(GOG_GALAXY_DB)
        epic_dir   = find_epic_manifests_dir()
        epic_oauth = epic_auth.load_tokens(CACHE_DIR) is not None
        ra_dir     = self._get_retroarch_dir()
        return {
            "status": "ok",
            "steam":              bool(steam_path),
            "gog":                galaxy_ok,
            "epic":               bool(epic_dir) or epic_oauth,
            "retroarch":          bool(ra_dir),
            "any":                bool(steam_path or galaxy_ok or epic_dir
                                       or epic_oauth or ra_dir),
            "epic_oauth_connected": epic_oauth,
        }

    # ── Per-launcher enable/disable ───────────────────────────────────────

    def get_launcher_connection_status(self):
        """Lightweight per-launcher status text for the Settings UI.

        Doesn't hammer Epic's API or do expensive work — uses cached data
        where possible and just reports source+name+count when known.
        Returns: { steam: {name, count}, gog: {name, count}, ... }
        """
        info = self.get_platform_user_info()  # uses internal cache

        # Steam count = total appids across user's collections
        steam_count = len({a for ids in (self._collections or {}).values()
                           for a in ids})

        # GOG count from Galaxy DB query (already cached at app level)
        try:
            gog_count = len(query_galaxy_db_for_platform("gog"))
        except Exception:
            gog_count = 0

        # Epic OAuth: check connection state, use cached library if present
        epic_oauth_connected = epic_auth.load_tokens(CACHE_DIR) is not None
        epic_count = 0
        if epic_oauth_connected:
            try:
                if os.path.isfile(EPIC_LIB_CACHE):
                    with open(EPIC_LIB_CACHE, "r", encoding="utf-8") as f:
                        epic_count = len(json.load(f).get("games") or [])
            except Exception:
                pass
        if epic_count == 0:
            try:
                epic_count = len(query_galaxy_db_for_platform("epic"))
            except Exception:
                pass

        def _galaxy_count(prefix):
            try:
                return len(query_galaxy_db_for_platform(prefix))
            except Exception:
                return 0

        return {
            "status": "ok",
            "steam": {
                "connected": bool(self._steam_path),
                "name":      info.get("steam", {}).get("name"),
                "count":     steam_count,
                "source":    "library JSON",
            },
            "gog": {
                "connected": os.path.isfile(GOG_GALAXY_DB),
                "name":      info.get("gog", {}).get("name"),
                "count":     gog_count,
                "source":    "GOG Galaxy DB",
            },
            "epic": {
                "connected": epic_oauth_connected or self._epic_in_galaxy_or_manifests(),
                "name":      info.get("epic", {}).get("name"),
                "count":     epic_count,
                "source":    get_setting("epic_source", "galaxy"),
                "oauth":     epic_oauth_connected,
            },
        }

    def _epic_in_galaxy_or_manifests(self):
        if find_epic_manifests_dir():
            return True
        return bool(query_galaxy_db_for_platform("epic"))

    def get_launcher_status(self):
        """Return per-launcher {installed, enabled} for the Launcher Visibility
        settings UI."""
        detected = self.detect_platforms()
        disabled = set(get_setting("disabled_launchers", []) or [])
        launchers = []
        for pid, meta in PLATFORMS.items():
            launchers.append({
                "id":        pid,
                "name":      meta["name"],
                "installed": bool(detected.get(pid, False)),
                "enabled":   pid not in disabled,
            })
        return {"status": "ok", "launchers": launchers}

    def set_launcher_enabled(self, launcher_id, enabled):
        """Toggle visibility of a launcher's tab.  Disabling a launcher hides
        its tab and excludes its games from Leave-It-To-Fate."""
        if launcher_id not in PLATFORMS:
            return {"status": "error", "message": f"Unknown launcher: {launcher_id}"}
        disabled = set(get_setting("disabled_launchers", []) or [])
        if enabled:
            disabled.discard(launcher_id)
        else:
            disabled.add(launcher_id)
        set_setting("disabled_launchers", sorted(disabled))
        return self.get_launcher_status()

    def launch_battlenet_game(self, raw_id, source=None):
        """Launch a Battle.net game via GOG Galaxy (always Galaxy-sourced now)."""
        release_key = raw_id if raw_id.startswith("battlenet_") else f"battlenet_{raw_id}"
        return self._launch_uri(f"goggalaxy://openGameView/{release_key}")

    def launch_origin_game(self, raw_id, source=None):
        """Launch an EA App game via GOG Galaxy."""
        release_key = raw_id if raw_id.startswith("origin_") else f"origin_{raw_id}"
        return self._launch_uri(f"goggalaxy://openGameView/{release_key}")

    def launch_uplay_game(self, raw_id, source=None):
        """Launch a Ubisoft Connect game via GOG Galaxy."""
        release_key = raw_id if raw_id.startswith("uplay_") else f"uplay_{raw_id}"
        return self._launch_uri(f"goggalaxy://openGameView/{release_key}")

    def get_sound_enabled(self):
        """Whether reel tick / landing sounds are enabled."""
        return {"status": "ok", "enabled": bool(get_setting("sound_enabled", True))}

    def set_sound_enabled(self, enabled):
        """Toggle reel tick / landing sounds."""
        set_setting("sound_enabled", bool(enabled))
        return {"status": "ok", "enabled": bool(enabled)}

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
        for ids in (self._collections or {}).values():
            steam_appids.update(ids)

        installed_steam = {}
        if self._steam_path:
            try:
                installed_steam = scan_all_acf(self._steam_path)
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
            _sid = self._steamid64()
            _cached = steam_api.load_cache(CACHE_DIR, _sid) if _sid else None
            if _cached and _cached.get("status") == "ok":
                for g in _cached.get("games", []):
                    owned_names[g["appid"]] = g["name"]
                steam_appids.update(owned_names.keys())

        name_cache = load_name_cache()
        bulk_names = self._bulk_steam_names()

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
            self._start_name_warmer(unresolved)

        # Split GOG result by platform so integrated launchers are deduped
        # independently (not lumped under the 'gog' bucket)
        gog_all    = (_gog_games if _gog_games is not None
                      else self.get_gog_games().get("games", []))
        epic_list  = (_epic_games if _epic_games is not None
                      else self.get_epic_games().get("games", []))
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
            self._start_xplat_name_resolver(xplat_candidates, steam_appids)

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
            gog  = self.get_gog_games().get("games", [])
            epic = self.get_epic_games().get("games", [])
        else:
            gog, epic = [], []
        return {
            "status":  "ok",
            "dedup":   self.get_duplicate_filter(_gog_games=gog, _epic_games=epic),
            "edition": self.get_edition_filter(_gog_games=gog, _epic_games=epic),
        }

    def open_external_url(self, url):
        """Open an http/https URL in the user's default browser.  Used by the
        OAuth modal to fire the Epic login page."""
        if not isinstance(url, str) or not (url.startswith("http://") or
                                            url.startswith("https://")):
            return {"status": "error", "message": "Invalid URL"}
        try:
            os.startfile(url)
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # Both Galaxy and Epic register URL Protocol handlers — these resolve via
    # ShellExecute when launched through the Windows shell.  ``cmd /c start``
    # is the most reliable cross-context way to fire a URI on Windows; it works
    # even when the calling process is a non-foreground thread (which is what
    # pywebview's js_api callbacks are).
    def _launch_uri(self, uri):
        """Open a protocol URI through the Windows shell.  Returns the URI in
        the response so the frontend can surface it in a toast for debugging."""
        import subprocess
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", uri],
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return {"status": "ok", "uri": uri}
        except Exception as e:
            return {"status": "error", "message": str(e), "uri": uri}

    def launch_gog_game(self, raw_id, source=None):
        """Launch a GOG game via the GOG Galaxy URI scheme.  Galaxy opens the
        game page, where the user clicks Play (installed) or Install (not).

        The correct verb is ``openGameView`` (not ``openGame``) — the latter
        is silently ignored by recent Galaxy versions."""
        release_key = raw_id if raw_id.startswith("gog_") else f"gog_{raw_id}"
        return self._launch_uri(f"goggalaxy://openGameView/{release_key}")

    def launch_epic_game(self, raw_id, source=None, app_name=None):
        """Launch an Epic game.  Picks the best launch method based on what
        we know about the game:

        * source='manifests' — raw_id IS the launcher AppName, so we can
          fire the Epic Launcher URI directly.
        * source='oauth' with app_name — same, AppName came from the catalog
          API response.
        * source='galaxy' (or anything else) — fall back to GOG Galaxy URI,
          which handles both installed and uninstalled games gracefully.
        """
        # Direct Epic Launcher launch when we know the AppName
        if source == "manifests":
            uri = (f"com.epicgames.launcher://apps/{raw_id}"
                   "?action=launch&silent=true")
        elif source == "oauth" and app_name:
            uri = (f"com.epicgames.launcher://apps/{app_name}"
                   "?action=launch&silent=true")
        else:
            # Galaxy URI — works for both installed and uninstalled games
            release_key = raw_id if raw_id.startswith("epic_") else f"epic_{raw_id}"
            uri = f"goggalaxy://openGameView/{release_key}"
        return self._launch_uri(uri)

    # ── RetroArch ─────────────────────────────────────────────────────────
    def _get_retroarch_dir(self):
        """Locate (and cache) the RetroArch install dir, honouring a saved
        path from config so the drive scan is skipped on later launches."""
        if self._retroarch_dir:
            return self._retroarch_dir
        if self._ra_dir_checked:
            # Already scanned and came up empty — don't re-walk every drive on
            # each detect_platforms() call (most users won't have RetroArch).
            return None
        self._ra_dir_checked = True
        saved = (load_config() or {}).get("retroarch_path")
        d = retroarch.find_retroarch_dir(saved)
        if d:
            self._retroarch_dir = d
            if d != saved:                 # persist for next time
                cfg = load_config()
                cfg["retroarch_path"] = d
                save_config(cfg)
        return d

    def _ensure_retroarch(self):
        """Parse playlists once and build an id->game index.  Cached for the
        process lifetime; reload_retroarch() refreshes it."""
        if self._ra_playlists is not None:
            return self._ra_playlists
        d = self._get_retroarch_dir()
        playlists = retroarch.load_playlists(d) if d else []
        index = {}
        for pl in playlists:
            for g in pl["games"]:
                index[g["id"]] = g
        self._ra_playlists = playlists
        self._ra_index = index
        return playlists

    def reload_retroarch(self):
        """Drop caches so the next call re-scans playlists (e.g. after the
        user scans new ROMs in RetroArch).  Also re-arms install detection in
        case RetroArch was installed after this session started."""
        self._ra_playlists = None
        self._ra_index = None
        if not self._retroarch_dir:
            self._ra_dir_checked = False
        return self.get_retroarch_playlists()

    @staticmethod
    def _ra_public(g):
        """Trim a game dict to what the frontend needs — no filesystem paths.
        Art and launch are resolved server-side by id."""
        return {
            "id":        g["id"],
            "raw_id":    g["id"],
            "name":      g["name"],
            "platform":  "retroarch",
            "system":    g["system"],
            "has_thumb": bool(g.get("thumb_path")),
        }

    def get_retroarch_playlists(self):
        """Lightweight grid data: one entry per system playlist (name, count,
        and a sample game id whose boxart can back the card).  No per-game
        payload, so this stays tiny even with a 10k-ROM library."""
        playlists = self._ensure_retroarch()
        if not self._get_retroarch_dir():
            return {"status": "notfound",
                    "message": "RetroArch not found on this PC."}
        out = []
        for pl in playlists:
            sample = next((g["id"] for g in pl["games"] if g.get("thumb_path")), None)
            out.append({"name": pl["name"], "system": pl["system"],
                        "count": pl["count"], "sample_id": sample})
        total = sum(pl["count"] for pl in playlists)
        port = self._ensure_art_server()
        art_base = f"http://127.0.0.1:{port}/ra" if port else None
        return {"status": "ok", "total": total, "playlists": out,
                "art_base": art_base}

    def get_retroarch_games(self, system=None):
        """Return trimmed game dicts for one system, or every system when
        `system` is None (RetroArch Library card and Leave It To Fate)."""
        playlists = self._ensure_retroarch()
        games = []
        for pl in playlists:
            if system is None or pl["system"] == system:
                games.extend(self._ra_public(g) for g in pl["games"])
        return {"status": "ok", "games": games}

    # Local boxart server ----------------------------------------------------
    # RetroArch boxarts are big PNGs (hundreds of KB) and a big library has
    # thousands of them, so we can't ship them over the js_api bridge as base64
    # for every card/reel tile.  Instead we run a tiny localhost HTTP server
    # that serves each game's boxart by id, downscaled (Pillow) and disk-cached
    # to a few tens of KB.  The browser then loads them like any <img> URL —
    # in parallel, lazily, and cached in the WebView2 profile — so cards and
    # the whole reel can show art without choking the app.
    def _art_bytes(self, thumb_path, max_w):
        """Return (bytes, content_type) for a boxart, downscaled to max_w px
        wide and disk-cached.  Falls back to the original PNG if Pillow is
        unavailable or anything goes wrong."""
        if not max_w:
            try:
                with open(thumb_path, "rb") as f:
                    return f.read(), "image/png"
            except OSError:
                return None, None
        # Cache key includes mtime so a re-scanned / replaced boxart at the
        # same path invalidates the old downscaled copy instead of serving it
        # forever.
        try:
            mtime = int(os.path.getmtime(thumb_path))
        except OSError:
            mtime = 0
        key = hashlib.sha1(
            f"{thumb_path}|{max_w}|{mtime}".encode("utf-8", "replace")).hexdigest()[:16]
        cache_dir = os.path.join(CACHE_DIR, "ra_thumbs")
        cpath = os.path.join(cache_dir, f"{key}.jpg")
        if os.path.isfile(cpath):
            try:
                with open(cpath, "rb") as f:
                    return f.read(), "image/jpeg"
            except OSError:
                pass
        try:
            from PIL import Image
            im = Image.open(thumb_path)
            # Flatten any transparency onto white (JPEG has no alpha; the
            # default convert("RGB") would turn transparent pixels black).
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
            if im.width > max_w:
                im = im.resize((max_w, max(1, round(im.height * max_w / im.width))),
                               Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82)
            data = buf.getvalue()
            # Atomic write (temp + replace) so a concurrent request can't read
            # a half-written file.
            try:
                os.makedirs(cache_dir, exist_ok=True)
                tmp = f"{cpath}.{os.getpid()}.tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, cpath)
            except OSError:
                pass
            return data, "image/jpeg"
        except Exception:
            try:
                with open(thumb_path, "rb") as f:
                    return f.read(), "image/png"
            except OSError:
                return None, None

    def _ensure_art_server(self):
        """Start the localhost boxart server (once) and return its port."""
        with self._ra_art_lock:
            if self._ra_art_port:
                return self._ra_art_port
            import http.server
            import socketserver
            api = self

            class _Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass  # stay quiet
                def do_GET(self):
                    # /ra/<game_id>[/<max_w>]
                    parts = self.path.split("?")[0].strip("/").split("/")
                    if len(parts) < 2 or parts[0] != "ra":
                        self.send_response(404); self.end_headers(); return
                    gid  = parts[1]
                    maxw = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                    g = (api._ra_index or {}).get(gid)
                    if not g or not g.get("thumb_path"):
                        self.send_response(404); self.end_headers(); return
                    data, ctype = api._art_bytes(g["thumb_path"], maxw)
                    if not data:
                        self.send_response(404); self.end_headers(); return
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    try:
                        self.wfile.write(data)
                    except OSError:
                        pass

            class _ArtServer(socketserver.ThreadingTCPServer):
                allow_reuse_address = True   # rebind cleanly after a restart
                daemon_threads = True

            # Prefer a fixed port so art URLs stay stable between launches and
            # WebView2's persistent HTTP cache can reuse them; fall back to an
            # ephemeral port if it's taken.
            srv = None
            for port in (47653, 0):
                try:
                    srv = _ArtServer(("127.0.0.1", port), _Handler)
                    break
                except OSError:
                    continue
            if srv is None:
                self._ra_art_port = None
                return None
            self._ra_art_port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True,
                             name="ra-art-server").start()
            return self._ra_art_port

    def launch_retroarch_game(self, game_id):
        """Launch a ROM: `retroarch.exe -L <core> <rom>`.  ROM + core are
        resolved from the server-side index by id; -L is omitted if we
        couldn't resolve a core (RetroArch then auto-picks one)."""
        self._ensure_retroarch()
        g = (self._ra_index or {}).get(game_id)
        if not g:
            return {"status": "error", "message": "Unknown RetroArch game."}
        ra_dir = self._get_retroarch_dir() or ""
        exe = os.path.join(ra_dir, "retroarch.exe")
        if not os.path.isfile(exe):
            return {"status": "error", "message": "retroarch.exe not found."}
        rom  = g.get("rom_path")
        core = g.get("core_path")
        if not rom or not os.path.isfile(rom):
            return {"status": "error", "message": "ROM file not found on disk."}
        try:
            import subprocess
            args = [exe]
            if core and os.path.isfile(core):
                args += ["-L", core]
            args.append(rom)
            subprocess.Popen(args, cwd=ra_dir,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── Game launch ───────────────────────────────────────────────────────

    def launch_game(self, appid_str):
        """Launch a game via the steam:// URI protocol.  Non-Steam shortcuts
        need the 64-bit `rungameid` form, not plain `run`."""
        try:
            appid = int(appid_str)
            if appid in self._shortcuts:
                # Non-Steam shortcut: gameid = (unsigned_appid << 32) | 0x02000000
                gameid = (appid << 32) | 0x02000000
                uri = f"steam://rungameid/{gameid}"
            else:
                uri = f"steam://run/{appid}"
            os.startfile(uri)
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── Installed library (fallback for no-collections users) ─────────────

    def get_installed_games(self):
        """
        Scan local appmanifest files and return all installed games.
        Used as a fallback 'Whole Library' when the user has no custom collections.
        """
        if not self._steam_path:
            return {"status": "error", "message": "Steam not found."}
        games = scan_all_acf(self._steam_path)
        if not games:
            return {"status": "notfound", "message": "No installed games found."}

        # Also persist names into the cache while we're here
        cache = load_name_cache()
        updated = False
        for appid, name in games.items():
            if str(appid) not in cache:
                cache[str(appid)] = name
                updated = True
        if updated:
            save_name_cache(cache)

        return {
            "status": "ok",
            "games": [{"appid": appid, "name": name}
                      for appid, name in sorted(games.items(), key=lambda kv: kv[1].lower())],
        }

    # ── Steam Web API key + full owned library (opt-in) ───────────────────
    def _steamid64(self):
        """Best-effort 64-bit SteamID for the active user.

        Prefer the account id baked into our collections path; fall back to the
        most-recent user in loginusers.vdf so this still works for users who
        have no collections file at all."""
        info = self.get_user_info()
        if info.get("status") == "ok" and info.get("steamid64"):
            return info["steamid64"]
        if self._steam_path:
            lu = os.path.join(self._steam_path, "config", "loginusers.vdf")
            if os.path.isfile(lu):
                try:
                    with open(lu, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    first = None
                    for m in re.finditer(r'"(7656\d{10,})"\s*\{(.*?)\n\s*\}',
                                         content, re.DOTALL):
                        sid, body = m.group(1), m.group(2)
                        if first is None:
                            first = sid
                        if re.search(r'"MostRecent"\s*"1"', body):
                            return sid
                    return first
                except Exception:
                    pass
        return None

    def set_steam_api_key(self, key):
        """Validate + store a Steam Web API key (DPAPI-encrypted) and verify it
        works by doing one live GetOwnedGames call.  Returns a status the
        Settings UI can show."""
        key = (key or "").strip()
        if not steam_api.validate_key_format(key):
            return {"status": "invalid_format",
                    "message": "That doesn't look like a Steam Web API key "
                               "(it should be 32 letters/numbers)."}
        sid = self._steamid64()
        if not sid:
            return {"status": "no_steamid",
                    "message": "Couldn't determine your SteamID — is Steam installed and signed in?"}
        games, st = steam_api.fetch_owned(key, sid)
        if st == "unauthorized":
            return {"status": "unauthorized",
                    "message": "Steam rejected that key. Double-check you copied it correctly."}
        if st == "error":
            return {"status": "error",
                    "message": "Couldn't reach Steam to verify the key. Check your connection and try again."}
        # st in ('ok', 'private') — the key itself is valid; persist it.
        epic_auth.store_secret(CACHE_DIR, "steam_api_key", key)
        steam_api.save_cache(CACHE_DIR, sid, st, games)
        if st == "private":
            return {"status": "private",
                    "message": "Key saved, but your Steam profile's Game details "
                               "are private — set them to Public so the key can "
                               "read your library (Steam → Profile → Edit → Privacy)."}
        # Fold the fetched names into the on-disk name cache for dedup/display.
        self._merge_names_into_cache({str(g["appid"]): g["name"]
                                      for g in games if g["name"]})
        return {"status": "ok", "count": len(games)}

    def get_steam_api_key_status(self):
        """Whether a key is stored (never returns the key itself)."""
        return {"status": "ok",
                "has_key": epic_auth.load_secret(CACHE_DIR, "steam_api_key") is not None}

    def clear_steam_api_key(self):
        """Forget the stored key and drop the owned-library cache."""
        epic_auth.clear_secret(CACHE_DIR, "steam_api_key")
        steam_api.clear_cache(CACHE_DIR)
        return {"status": "ok"}

    def get_steam_owned_games(self, force_refresh=False):
        """Full owned Steam library via the user's API key (cached).

        Returns {status, games:[{appid,name}]}.  status:
          'ok'        — games returned
          'no_key'    — no API key stored (caller uses collections/installed)
          'private'   — key works but profile game details are private
          'unauthorized' / 'error' — key/network problem
        """
        key = epic_auth.load_secret(CACHE_DIR, "steam_api_key")
        if not key:
            return {"status": "no_key", "games": []}
        sid = self._steamid64()
        if not sid:
            return {"status": "error", "games": [],
                    "message": "Couldn't determine your SteamID."}
        result = steam_api.get_owned(CACHE_DIR, key, sid, force_refresh=force_refresh)
        if result["status"] == "ok" and result.get("games") and not result.get("cached"):
            self._merge_names_into_cache({str(g["appid"]): g["name"]
                                          for g in result["games"] if g["name"]})
        return {"status": result["status"], "games": result.get("games", [])}

    # ── First-run onboarding ──────────────────────────────────────────────
    def get_onboarding_state(self):
        """Whether to show the first-run welcome (with the optional Steam API
        key prompt)."""
        return {
            "status":         "ok",
            "onboarded":      bool(get_setting("onboarded", False)),
            "steam_detected": bool(self._steam_path),
            "has_key":        epic_auth.load_secret(CACHE_DIR, "steam_api_key") is not None,
        }

    def dismiss_onboarding(self):
        """Mark the welcome as seen so it doesn't show again."""
        set_setting("onboarded", True)
        return {"status": "ok"}

    # ── Game art (winner panel) ───────────────────────────────────────────

    def get_game_art(self, appid_str):
        """
        Fetch a game's header image via Python's network stack.  Used for the
        winner panel where reliability matters more than latency.
        Resolution order:
          1. Disk cache       (cache/art/<appid>.<ext>)
          2. Steam grid folder — user-uploaded art for non-Steam shortcuts
          3. Steam CDN URLs   (with age-gate cookies, validates magic bytes)
          4. Steam appdetails API — returns the canonical header_image URL,
                                    which works for some restricted games the
                                    direct CDN paths don't.
        Returns {"status": "ok",  "data": "data:image/<mime>;base64,..."}
             or {"status": "notfound"}
        """
        try:
            appid = int(appid_str)
        except (ValueError, TypeError):
            return {"status": "notfound"}

        # 1. Disk cache (jpg or png)
        for ext in ("jpg", "png"):
            cache_path = os.path.join(ART_CACHE_DIR, f"{appid}.{ext}")
            if os.path.isfile(cache_path):
                hit = _read_image_as_data_url(cache_path)
                if hit:
                    return {"status": "ok", "data": hit}

        # 2. Steam grid folder (non-Steam shortcut user-uploaded art)
        if self._steam_path:
            for grid_path in _find_grid_images(self._steam_path, appid):
                hit = _read_image_as_data_url(grid_path)
                if hit:
                    # Cache it
                    ext = "png" if grid_path.lower().endswith(".png") else "jpg"
                    try:
                        with open(grid_path, "rb") as src, \
                             open(os.path.join(ART_CACHE_DIR, f"{appid}.{ext}"), "wb") as dst:
                            dst.write(src.read())
                    except OSError:
                        pass
                    return {"status": "ok", "data": hit}

        # 3. Steam CDN URLs with mature-content cookies
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Cookie":     "birthtime=283993201; mature_content=1; wants_mature_content=1; "
                          "lastagecheckage=1-0-1979",
            "Referer":    "https://store.steampowered.com/",
        }
        urls = [
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
            f"https://steamcdn-a.akamaihd.net/steam/apps/{appid}/header.jpg",
            f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg",
            f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg",
        ]
        for url in urls:
            data = _try_fetch_image(url, headers)
            if data:
                return _cache_and_return(appid, data)

        # 4. Steam appdetails API — the canonical header_image URL is sometimes
        #    served from a different host than the direct paths above.
        try:
            api_url = (f"https://store.steampowered.com/api/appdetails"
                       f"?appids={appid}&filters=basic&cc=us&l=english")
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            entry = payload.get(str(appid), {})
            if entry.get("success") and "data" in entry:
                canon = entry["data"].get("header_image")
                if canon and canon not in urls:
                    data = _try_fetch_image(canon, headers)
                    if data:
                        return _cache_and_return(appid, data)
        except Exception:
            pass

        return {"status": "notfound"}

    # ── Shortcut → collection assignments (managed inside the app) ────────

    def get_shortcuts_with_assignments(self):
        """List every non-Steam shortcut with its currently-assigned
        collections.  Used by the Manage Shortcuts screen."""
        cfg = load_config()
        assignments = cfg.get("shortcut_assignments", {})
        shortcuts = [
            {
                "appid":       sc["appid"],
                "name":        (sc.get("name") or f"Shortcut {sc['appid']}").strip()
                                 or f"Shortcut {sc['appid']}",
                "collections": assignments.get(str(sc["appid"]), []),
            }
            for sc in self._shortcuts.values()
        ]
        shortcuts.sort(key=lambda s: s["name"].lower())
        available = sorted(
            (n for n in self._collections.keys() if n not in self._EXCLUDED),
            key=str.lower,
        )
        return {
            "status":                "ok",
            "shortcuts":             shortcuts,
            "available_collections": available,
        }

    def set_shortcut_collections(self, appid_str, collection_names):
        """Persist a shortcut's collection membership, then re-merge so the
        next call to get_collections sees the change immediately."""
        try:
            appid = int(appid_str)
        except (ValueError, TypeError):
            return {"status": "error", "message": "Invalid appid."}
        names = [n for n in (collection_names or []) if n]
        cfg = load_config()
        assignments = cfg.get("shortcut_assignments", {})
        if names:
            assignments[str(appid)] = names
        else:
            assignments.pop(str(appid), None)
        cfg["shortcut_assignments"] = assignments
        save_config(cfg)

        # Re-run the full load so the merge picks up the new assignment
        if self._collections_path and os.path.isfile(self._collections_path):
            self._load_from_path(self._collections_path)
        return {
            "status":         "ok",
            "collections":    self._collections_as_list(),
            "shortcut_appids": list(self._shortcuts.keys()),
        }

    def batch_set_shortcut_collections(self, assignments):
        """Apply a list of {appid, collections} updates in one shot, then
        re-merge once.  Avoids N round-trips when the user multi-selects."""
        cfg = load_config()
        sa  = cfg.get("shortcut_assignments", {})
        for item in (assignments or []):
            try:
                appid = int(item.get("appid"))
            except (ValueError, TypeError):
                continue
            names = [n for n in (item.get("collections") or []) if n]
            if names:
                sa[str(appid)] = names
            else:
                sa.pop(str(appid), None)
        cfg["shortcut_assignments"] = sa
        save_config(cfg)
        if self._collections_path and os.path.isfile(self._collections_path):
            self._load_from_path(self._collections_path)
        _cfg = load_config()
        return {
            "status":             "ok",
            "collections":        self._collections_as_list(),
            "shortcut_appids":    [a for a in self._shortcuts.keys()
                                   if a not in set(_cfg.get("excluded_appids", []))],
            "hidden_collections": list(_cfg.get("hidden_collections", [])),
        }

    # ── Exclude / hide / settings ─────────────────────────────────────────

    def toggle_exclude(self, appid_str):
        """Toggle whether a game/shortcut is excluded from all future spins."""
        try:
            appid = int(appid_str)
        except (ValueError, TypeError):
            return {"status": "error", "message": "Invalid appid"}
        cfg = load_config()
        excluded = set(cfg.get("excluded_appids", []))
        if appid in excluded:
            excluded.discard(appid)
            action = "included"
        else:
            excluded.add(appid)
            action = "excluded"
        cfg["excluded_appids"] = sorted(excluded)
        save_config(cfg)
        if self._collections_path and os.path.isfile(self._collections_path):
            self._load_from_path(self._collections_path)
        return {
            "status":             "ok",
            "action":             action,
            "collections":        self._collections_as_list(),
            "shortcut_appids":    [a for a in self._shortcuts.keys() if a not in excluded],
            "hidden_collections": list(load_config().get("hidden_collections", [])),
        }

    # ── GOG / Epic exclusions ─────────────────────────────────────────────
    #
    # Same idea as toggle_exclude (which is Steam-only because it operates on
    # integer appids) but keyed by the full prefixed game ID ('gog_<id>' or
    # 'epic_<id>').  Stored as a flat dict {id: name} so we can show the names
    # in Settings without re-fetching from Galaxy / Epic.

    def toggle_exclude_platform_game(self, game_id, name=None):
        """Toggle whether a GOG / Epic game is excluded from future spins."""
        if not isinstance(game_id, str) or not game_id:
            return {"status": "error", "message": "Invalid game_id"}
        if "_" not in game_id:
            return {"status": "error",
                    "message": "game_id must be a prefixed platform id (e.g. gog_123)"}
        cfg = load_config()
        excluded = dict(cfg.get("excluded_platform_games", {}))
        if game_id in excluded:
            del excluded[game_id]
            action = "included"
        else:
            excluded[game_id] = (name or "").strip() or game_id
            action = "excluded"
        cfg["excluded_platform_games"] = excluded
        save_config(cfg)
        return {"status": "ok", "action": action}

    def toggle_hide_collection(self, name):
        """Toggle whether a collection is hidden from the grid/Whole Library/
        Collection Roulette."""
        cfg = load_config()
        hidden = set(cfg.get("hidden_collections", []))
        name = (name or "").strip()
        if not name:
            return {"status": "error", "message": "Empty name"}
        if name in hidden:
            hidden.discard(name)
            action = "shown"
        else:
            hidden.add(name)
            action = "hidden"
        cfg["hidden_collections"] = sorted(hidden)
        save_config(cfg)
        return {
            "status":             "ok",
            "action":             action,
            "collections":        self._collections_as_list(),
            "shortcut_appids":    [a for a in self._shortcuts.keys()
                                   if a not in set(cfg.get("excluded_appids", []))],
            "hidden_collections": sorted(hidden),
        }

    def get_settings(self):
        """List currently-hidden collections and currently-excluded games,
        with resolved names so the Settings UI can display them."""
        cfg = load_config()
        excluded_ids = sorted(cfg.get("excluded_appids", []))
        cache = load_name_cache()
        excluded = []
        for appid in excluded_ids:
            name = cache.get(str(appid))
            if not name and appid in self._shortcuts:
                name = self._shortcuts[appid].get("name")
            if not name:
                name = f"App {appid}"
            excluded.append({"appid": appid, "name": name})
        hidden_names = sorted(cfg.get("hidden_collections", []),
                              key=str.lower)
        hidden = [{"name": n,
                   "count": len(self._collections.get(n, []))} for n in hidden_names]
        # Platform-side (GOG/Epic) excludes — separate list with prefixed IDs
        platform_excluded_dict = cfg.get("excluded_platform_games", {}) or {}
        platform_excluded = []
        for gid, name in sorted(platform_excluded_dict.items(),
                                key=lambda kv: (kv[1] or kv[0]).lower()):
            platform = gid.split("_", 1)[0] if "_" in gid else ""
            platform_excluded.append({
                "id":       gid,
                "name":     name or gid,
                "platform": platform,
            })
        return {
            "status":                   "ok",
            "excluded_games":           excluded,
            "excluded_platform_games":  platform_excluded,
            "hidden_collections":       hidden,
        }

    # ── Logged-in Steam user info ─────────────────────────────────────────

    def get_user_info(self):
        """Read loginusers.vdf + avatarcache to return persona name + avatar
        for whichever account our collections JSON belongs to."""
        if not self._steam_path or not self._collections_path:
            return {"status": "notfound"}
        parts = os.path.normpath(self._collections_path).split(os.sep)
        try:
            ud_idx = parts.index("userdata")
            accountid = int(parts[ud_idx + 1])
        except (ValueError, IndexError):
            return {"status": "notfound"}

        steamid64 = accountid + 76561197960265728

        persona = None
        lu_path = os.path.join(self._steam_path, "config", "loginusers.vdf")
        if os.path.isfile(lu_path):
            try:
                with open(lu_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                # Find the block keyed by our steamid64
                m = re.search(rf'"{steamid64}"\s*\{{(.*?)\n\s*\}}',
                              content, re.DOTALL)
                if m:
                    pm = re.search(r'"PersonaName"\s*"([^"]*)"', m.group(1))
                    if pm:
                        persona = pm.group(1)
            except Exception:
                pass

        avatar = None
        for ext in ("png", "jpg"):
            ap = os.path.join(self._steam_path, "config", "avatarcache",
                              f"{steamid64}.{ext}")
            if os.path.isfile(ap):
                try:
                    with open(ap, "rb") as f:
                        data = f.read()
                    mime = "image/png" if ext == "png" else "image/jpeg"
                    b64 = base64.b64encode(data).decode("ascii")
                    avatar = f"data:{mime};base64,{b64}"
                    break
                except OSError:
                    pass

        return {
            "status":       "ok",
            "persona_name": persona or "Steam User",
            "avatar":       avatar,
            "steamid64":    str(steamid64),
        }

    # ── Per-platform user info (Steam + GOG + Epic, unified) ──────────────

    def get_platform_user_info(self):
        """Return {steam, gog, epic} each {name, avatar} for the connected user.
        Each platform may be absent or have null name/avatar if not detected.
        Result is cached for the process lifetime to avoid repeated network +
        DB hits — call reload_platform_user_info() to force-refresh."""
        if not hasattr(self, "_platform_user_cache") or self._platform_user_cache is None:
            self._platform_user_cache = {
                "steam": self._get_steam_user_compact(),
                "gog":   self._get_gog_user(),
                "epic":  self._get_epic_user(),
            }
        return {"status": "ok", **self._platform_user_cache}

    def reload_platform_user_info(self):
        """Clear the per-platform user cache so the next call re-fetches."""
        self._platform_user_cache = None
        return self.get_platform_user_info()

    def _get_steam_user_compact(self):
        """Shape Steam user info to {name, avatar} to match the unified format."""
        info = self.get_user_info()
        if info.get("status") != "ok":
            return {"name": None, "avatar": None}
        return {"name": info.get("persona_name"), "avatar": info.get("avatar")}

    def _get_gog_user(self):
        """GOG: look up the userId in Galaxy's DB, then hit GOG's public profile
        API to resolve username + avatar.  Result lives in cache/gog_user.json
        for 24h to avoid repeated network calls."""
        gog_cache = os.path.join(CACHE_DIR, "gog_user.json")
        try:
            if os.path.isfile(gog_cache):
                with open(gog_cache, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if time.time() - cached.get("fetched_at", 0) < 86400:
                    return cached.get("info") or {"name": None, "avatar": None}
        except (json.JSONDecodeError, OSError):
            pass

        info = {"name": None, "avatar": None}
        conn = _galaxy_db_open()
        if conn is None:
            return info
        try:
            row = conn.execute("SELECT id FROM Users LIMIT 1").fetchone()
            uid = row[0] if row else None
        except sqlite3.Error:
            uid = None
        finally:
            conn.close()
        if not uid:
            return info

        try:
            req = urllib.request.Request(
                f"https://users.gog.com/users/{uid}",
                headers={"User-Agent": "Mozilla/5.0 SteamRoulette"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            info["name"]   = data.get("username")
            avatar         = data.get("avatar") or {}
            # Prefer 2x medium for nice display when scaled down
            info["avatar"] = (avatar.get("medium_2x") or avatar.get("medium")
                              or avatar.get("small_2x") or avatar.get("small"))
        except Exception:
            return info

        try:
            with open(gog_cache, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": time.time(), "info": info}, f, indent=2)
        except OSError:
            pass
        return info

    def _get_epic_user(self):
        """Epic: read displayName from the encrypted OAuth token blob.  No
        avatar — Epic doesn't expose one through their basic OAuth response,
        and the avatar endpoint requires a separate scope we don't request."""
        tokens = epic_auth.load_tokens(CACHE_DIR)
        if not tokens:
            return {"name": None, "avatar": None}
        return {"name": tokens.get("displayName"), "avatar": None}

    # ── Internal ──────────────────────────────────────────────────────────

    def _load_from_path(self, path):
        try:
            self._collections = parse_collections(path)

            # Non-Steam shortcuts from shortcuts.vdf
            self._shortcuts = {}
            vdf = find_shortcuts_vdf_for(path)
            if vdf:
                shortcuts = parse_shortcuts_vdf(vdf)
                self._shortcuts = {sc["appid"]: sc for sc in shortcuts if sc.get("appid")}
                merge_shortcuts_into_collections(self._collections, shortcuts)

            cfg = load_config()

            # In-app shortcut→collection assignments
            for appid_str, collection_names in cfg.get("shortcut_assignments", {}).items():
                try:
                    appid = int(appid_str)
                except (ValueError, TypeError):
                    continue
                if appid not in self._shortcuts:
                    continue
                for cname in collection_names:
                    if cname in self._collections and appid not in self._collections[cname]:
                        self._collections[cname].append(appid)

            # Remove user-excluded games from every collection
            excluded = set(cfg.get("excluded_appids", []))
            if excluded:
                for name in list(self._collections.keys()):
                    self._collections[name] = [a for a in self._collections[name] if a not in excluded]

            # Playtimes from localconfig.vdf (same account as collections file)
            self._playtimes = {}
            lc_path = os.path.join(os.path.dirname(os.path.dirname(path)), "localconfig.vdf")
            if os.path.isfile(lc_path):
                self._playtimes = parse_playtimes_from_localconfig(lc_path)

            self._collections_path = path
            cfg["collections_path"] = path
            save_config(cfg)
            return {
                "status":             "ok",
                "collections":        self._collections_as_list(),
                "shortcut_appids":    [a for a in self._shortcuts.keys() if a not in excluded],
                "hidden_collections": list(cfg.get("hidden_collections", [])),
                "path":               path,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # Collections to hide from the UI entirely (not games).
    _EXCLUDED = frozenset({"Software", "Server"})

    def _collections_as_list(self):
        hidden = set(load_config().get("hidden_collections", []))
        return [
            {"name": name, "count": len(ids), "appids": ids}
            for name, ids in self._collections.items()
            if name not in self._EXCLUDED and name not in hidden
        ]
