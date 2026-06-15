"""
SteamMixin for SteamRouletteAPI.

Steam-specific js_api: the optional Web API key + full owned library,
installed games, winner art, and non-Steam shortcut collection editing.

Methods here run on the single js_api object via cooperative multiple
inheritance, so they share instance state (self.*) set up in the core
SteamRouletteAPI.__init__.
"""

import epic_auth
import json
import os
import re
import steam_api
import urllib.request

from appconfig import ART_CACHE_DIR, CACHE_DIR, load_config, save_config
from steam_library import scan_all_acf
from game_names import load_name_cache, save_name_cache
from images import _cache_and_return, _find_grid_images, _read_image_as_data_url, _try_fetch_image


class SteamMixin:
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
