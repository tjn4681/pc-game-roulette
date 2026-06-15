"""
SettingsMixin for SteamRouletteAPI.

Settings, user info, onboarding, and exclude toggles surfaced to the
frontend's settings screen.

Methods here run on the single js_api object via cooperative multiple
inheritance, so they share instance state (self.*) set up in the core
SteamRouletteAPI.__init__.
"""

import base64
import epic_auth
import json
import os
import re
import sqlite3
import time
import urllib.request

from appconfig import CACHE_DIR, get_setting, load_config, save_config, set_setting
from game_names import load_name_cache
from galaxy import _galaxy_db_open


class SettingsMixin:
    def get_sound_enabled(self):
        """Whether reel tick / landing sounds are enabled."""
        return {"status": "ok", "enabled": bool(get_setting("sound_enabled", True))}

    def set_sound_enabled(self, enabled):
        """Toggle reel tick / landing sounds."""
        set_setting("sound_enabled", bool(enabled))
        return {"status": "ok", "enabled": bool(enabled)}

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
