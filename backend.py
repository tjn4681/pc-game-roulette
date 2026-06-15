"""
PC Game Roulette backend — Steam / GOG / Epic detection, collections parsing,
name fetching, cross-platform dedup, OAuth, config.

The class kept its historical SteamRouletteAPI name because it's referenced
by main.py and any future migrations would just churn diffs.

Exposed to the frontend via js_api in main.py.
"""

# ── Relocated helpers & js_api mixins ──────────────────
#
# Module-level helpers live in focused modules (appconfig, steam_library, …);
# the js_api surface is split into mixins, one concern per file.  This core
# class owns lifecycle + Steam Collection loading and combines the mixins so
# pywebview still sees every method on one object.

import os
import threading

from appconfig import load_config, save_config
from steam_library import find_collections_files, find_shortcuts_vdf_for, find_steam_path, merge_shortcuts_into_collections, parse_collections, parse_playtimes_from_localconfig, parse_shortcuts_vdf

from debug_mixin import DebugMixin
from names_mixin import NameWarmingMixin
from gogepic_mixin import GogEpicMixin
from retroarch_mixin import RetroArchMixin
from filters_mixin import FiltersMixin
from launchstatus_mixin import LaunchStatusMixin
from steam_mixin import SteamMixin
from settings_mixin import SettingsMixin


class SteamRouletteAPI(
    DebugMixin,
    NameWarmingMixin,
    GogEpicMixin,
    RetroArchMixin,
    FiltersMixin,
    LaunchStatusMixin,
    SteamMixin,
    SettingsMixin,
):
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

    def get_collections(self):
        return self._collections_as_list()

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

