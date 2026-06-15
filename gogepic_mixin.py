"""
GogEpicMixin for SteamRouletteAPI.

GOG and Epic library access: reading games from GOG Galaxy / Epic
manifests / Epic OAuth, the Epic source + merge settings, the Epic OAuth
flow, and Galaxy-backed collections.

Methods here run on the single js_api object via cooperative multiple
inheritance, so they share instance state (self.*) set up in the core
SteamRouletteAPI.__init__.
"""

import epic_api
import epic_auth
import json
import os
import sqlite3
import time
import urllib.error
import winreg

from appconfig import CACHE_DIR, EPIC_LIB_CACHE, EPIC_LIB_CACHE_TTL_SECONDS, get_setting, load_config, set_setting
from platforms import find_epic_manifests_dir, _GOG_INTEGRATED_PREFIXES
from galaxy import apply_galaxy_enrichment, query_galaxy_db_for_platform, _galaxy_db_open


class GogEpicMixin:
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
