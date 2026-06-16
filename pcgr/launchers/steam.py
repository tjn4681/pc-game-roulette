"""
Steam launcher.

The collection-centric one: it owns the loaded Collections, non-Steam shortcuts,
and playtimes, and exposes everything that hangs off them — Collection cards,
the installed-games fallback, per-appid name resolution, the optional Web API
key + full owned library, shortcut→collection editing, exclude/hide toggles, and
the signed-in user.  Launches via the ``steam://`` URI scheme.

Holds a reference to NameService only to fold freshly-fetched names into the
shared cache.
"""

import base64
import os
import re

from pcgr.config import CACHE_DIR, load_config, save_config
from pcgr.sources.steam_files import (
    find_collections_files, find_shortcuts_vdf_for, find_steam_path,
    lookup_acf_name, merge_shortcuts_into_collections, parse_collections,
    parse_playtimes_from_localconfig, parse_shortcuts_vdf, scan_all_acf,
)
from pcgr.sources.store import (
    fetch_name_from_api, fetch_name_from_steamspy, load_name_cache, save_name_cache,
)
from pcgr.sources import epic_auth, steam_api
from pcgr.launchers.base import Launcher


class SteamLauncher(Launcher):
    id = "steam"
    name = "Steam"

    # Collections to hide from the UI entirely (not games).
    _EXCLUDED = frozenset({"Software", "Server"})

    def __init__(self, names):
        self.names = names
        self._collections_path = None
        self._collections = {}
        self._shortcuts   = {}   # {appid: {'name': str, 'tags': [str,...]}}
        self._playtimes   = {}   # {appid: minutes_played} from localconfig.vdf
        self._steam_path  = find_steam_path()

    # ── Public accessors (used by the facade / other services) ────────────

    @property
    def steam_path(self):
        return self._steam_path

    @property
    def collections(self):
        return self._collections

    @property
    def shortcuts(self):
        return self._shortcuts

    @property
    def collections_path(self):
        return self._collections_path

    def load_from_path(self, path):
        """Public entry point to load a specific collections file (used by the
        file-picker fallback)."""
        return self._load_from_path(path)

    # ── Launcher interface ────────────────────────────────────────────────

    def is_present(self) -> bool:
        return bool(self._steam_path)

    def get_categories(self) -> dict:
        """Steam Collections as collection cards."""
        return {"status": "ok", "collections": self._collections_as_list()}

    def get_games(self) -> dict:
        """Installed games in the standard game-dict shape (interface
        completeness — the frontend builds the Steam grid from Collections /
        owned-library appids rather than calling this directly)."""
        if not self._steam_path:
            return {"status": "error", "games": [], "message": "Steam not found."}
        games = scan_all_acf(self._steam_path)
        return {"status": "ok", "games": [
            {"id": f"steam_{appid}", "raw_id": appid, "name": name,
             "platform": "steam", "source": "installed"}
            for appid, name in sorted(games.items(), key=lambda kv: kv[1].lower())
        ]}

    def launch(self, game_id, **opts) -> dict:
        """Launch a game via the steam:// URI protocol.  Non-Steam shortcuts
        need the 64-bit `rungameid` form, not plain `run`."""
        try:
            appid = int(game_id)
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

    def connection_status(self) -> dict:
        steam_count = len({a for ids in (self._collections or {}).values()
                           for a in ids})
        return {"connected": bool(self._steam_path), "count": steam_count,
                "source": "library JSON"}

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

    def _collections_as_list(self):
        hidden = set(load_config().get("hidden_collections", []))
        return [
            {"name": name, "count": len(ids), "appids": ids}
            for name, ids in self._collections.items()
            if name not in self._EXCLUDED and name not in hidden
        ]

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
        self.names.merge_into_cache({str(g["appid"]): g["name"]
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
            self.names.merge_into_cache({str(g["appid"]): g["name"]
                                         for g in result["games"] if g["name"]})
        return {"status": result["status"], "games": result.get("games", [])}

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

    # ── Exclude / hide (operate on Steam collections) ─────────────────────

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

    # ── Logged-in Steam user ──────────────────────────────────────────────

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

    def user(self):
        """Steam user shaped to {name, avatar} for the unified platform-user view."""
        info = self.get_user_info()
        if info.get("status") != "ok":
            return {"name": None, "avatar": None}
        return {"name": info.get("persona_name"), "avatar": info.get("avatar")}
