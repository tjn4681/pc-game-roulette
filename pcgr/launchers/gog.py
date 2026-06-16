"""
GOG launcher.

Reads the owned library from the GOG Galaxy SQLite DB — which also carries any
launchers the user integrated into Galaxy (Battle.net, EA App, Ubisoft
Connect) — and falls back to the Windows registry (installed GOG games only)
when Galaxy isn't present.  Categories are the user's Galaxy tags.  Games launch
through Galaxy's ``goggalaxy://`` URI; the integrated launchers ride the same
URI scheme with their own prefixes.
"""

import json
import os
import sqlite3
import time
import urllib.request
import winreg

from pcgr.config import CACHE_DIR, GOG_GALAXY_DB
from pcgr.platforms import _GOG_INTEGRATED_PREFIXES
from pcgr.sources.galaxy import (
    apply_galaxy_enrichment, query_galaxy_db_for_platform, tags_as_collections,
    _galaxy_db_open,
)
from pcgr.launchers.base import Launcher, filter_platform_excluded, launch_uri


class GogLauncher(Launcher):
    id = "gog"
    name = "GOG"

    def is_present(self) -> bool:
        return os.path.isfile(GOG_GALAXY_DB)

    def get_games(self) -> dict:
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
            galaxy = filter_platform_excluded(galaxy)
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
        filtered = filter_platform_excluded(
            sorted(unique, key=lambda g: g["name"].lower())
        )
        return {"status": "ok", "games": filtered, "source": "registry"}

    def get_categories(self) -> dict:
        """Galaxy tags as collection cards.  Includes the integrated-launcher
        prefixes so a game tagged in GOG Galaxy (e.g. an EA game tagged "FPS")
        appears in the GOG tag categories alongside native GOG games."""
        prefixes = ("gog",) + _GOG_INTEGRATED_PREFIXES
        return {"status": "ok", "collections": tags_as_collections(prefixes)}

    def launch(self, game_id, source=None, **opts) -> dict:
        """Launch a GOG game via the GOG Galaxy URI scheme.  Galaxy opens the
        game page, where the user clicks Play (installed) or Install (not).

        The correct verb is ``openGameView`` (not ``openGame``) — the latter
        is silently ignored by recent Galaxy versions."""
        return self._launch_galaxy("gog", game_id)

    # Integrated launchers (no own tab) — all routed through GOG Galaxy.
    def launch_battlenet(self, raw_id): return self._launch_galaxy("battlenet", raw_id)
    def launch_origin(self, raw_id):    return self._launch_galaxy("origin", raw_id)
    def launch_uplay(self, raw_id):     return self._launch_galaxy("uplay", raw_id)

    @staticmethod
    def _launch_galaxy(prefix, raw_id):
        release_key = raw_id if raw_id.startswith(f"{prefix}_") else f"{prefix}_{raw_id}"
        return launch_uri(f"goggalaxy://openGameView/{release_key}")

    def connection_status(self) -> dict:
        try:
            count = len(query_galaxy_db_for_platform("gog"))
        except Exception:
            count = 0
        return {"connected": self.is_present(), "count": count,
                "source": "GOG Galaxy DB"}

    def user(self) -> dict:
        """Look up the userId in Galaxy's DB, then hit GOG's public profile API
        to resolve username + avatar.  Result lives in cache/gog_user.json for
        24h to avoid repeated network calls."""
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
