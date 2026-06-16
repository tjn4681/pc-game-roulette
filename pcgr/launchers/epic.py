"""
Epic Games launcher.

The owned library comes from one of three sources, chosen by the ``epic_source``
setting: Epic's OAuth REST API (full library, needs a one-time connect), the GOG
Galaxy DB (Epic integration plugin), or the local install manifests (installed
only).  Categories are Galaxy tags.  Games launch through the Epic Launcher URI
when we know the AppName, else via Galaxy.  Also owns the OAuth connect/disconnect
flow and the source/merge settings surfaced in Settings and the wizard.
"""

import json
import os
import time
import urllib.error

from pcgr.config import (
    CACHE_DIR, EPIC_LIB_CACHE, EPIC_LIB_CACHE_TTL_SECONDS, get_setting, set_setting,
)
from pcgr.platforms import find_epic_manifests_dir
from pcgr.sources import epic_api, epic_auth
from pcgr.sources.galaxy import (
    apply_galaxy_enrichment, query_galaxy_db_for_platform, tags_as_collections,
)
from pcgr.launchers.base import Launcher, filter_platform_excluded, launch_uri


class EpicLauncher(Launcher):
    id = "epic"
    name = "Epic Games"

    def is_present(self) -> bool:
        return bool(find_epic_manifests_dir()) or epic_auth.load_tokens(CACHE_DIR) is not None

    def oauth_connected(self) -> bool:
        return epic_auth.load_tokens(CACHE_DIR) is not None

    def in_galaxy_or_manifests(self) -> bool:
        if find_epic_manifests_dir():
            return True
        return bool(query_galaxy_db_for_platform("epic"))

    # ── Library ───────────────────────────────────────────────────────────

    def get_games(self, force_refresh=False) -> dict:
        """Return owned Epic games.  Routes to one of three sources based on
        the `epic_source` setting:

            'oauth'  — direct from Epic's API (requires OAuth connection)
            'galaxy' — GOG Galaxy SQLite DB (Epic integration plugin)
            (auto-fallback to local manifests if both above yield nothing)

        force_refresh=True bypasses the on-disk OAuth library cache.
        """
        source = get_setting("epic_source", "galaxy")

        if source == "oauth":
            result = self._get_oauth(force_refresh=force_refresh)
            if result["status"] == "ok" and result["games"]:
                result["games"] = filter_platform_excluded(result["games"])
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
            galaxy = filter_platform_excluded(galaxy)
            return {"status": "ok", "games": galaxy, "source": "galaxy"}

        # Final fallback: locally installed games via manifest folder
        result = self._get_manifests()
        apply_galaxy_enrichment(result.get("games", []), platform="epic")
        result["games"] = filter_platform_excluded(result.get("games", []))
        return result

    def _get_oauth(self, force_refresh=False):
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

    def _get_manifests(self):
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

    def get_categories(self) -> dict:
        """Epic's Galaxy tags as collection cards."""
        return {"status": "ok", "collections": tags_as_collections(("epic",))}

    def launch(self, game_id, source=None, app_name=None, **opts) -> dict:
        """Launch an Epic game.  Picks the best launch method based on what
        we know about the game:

        * source='manifests' — raw_id IS the launcher AppName, so we can
          fire the Epic Launcher URI directly.
        * source='oauth' with app_name — same, AppName came from the catalog
          API response.
        * source='galaxy' (or anything else) — fall back to GOG Galaxy URI,
          which handles both installed and uninstalled games gracefully.
        """
        if source == "manifests":
            uri = (f"com.epicgames.launcher://apps/{game_id}"
                   "?action=launch&silent=true")
        elif source == "oauth" and app_name:
            uri = (f"com.epicgames.launcher://apps/{app_name}"
                   "?action=launch&silent=true")
        else:
            release_key = game_id if game_id.startswith("epic_") else f"epic_{game_id}"
            uri = f"goggalaxy://openGameView/{release_key}"
        return launch_uri(uri)

    def connection_status(self) -> dict:
        oauth_connected = epic_auth.load_tokens(CACHE_DIR) is not None
        count = 0
        if oauth_connected:
            try:
                if os.path.isfile(EPIC_LIB_CACHE):
                    with open(EPIC_LIB_CACHE, "r", encoding="utf-8") as f:
                        count = len(json.load(f).get("games") or [])
            except Exception:
                pass
        if count == 0:
            try:
                count = len(query_galaxy_db_for_platform("epic"))
            except Exception:
                pass
        return {
            "connected": oauth_connected or self.in_galaxy_or_manifests(),
            "count":     count,
            "source":    get_setting("epic_source", "galaxy"),
            "oauth":     oauth_connected,
        }

    def user(self) -> dict:
        """Display name from the encrypted OAuth token blob (no avatar — Epic's
        basic OAuth response doesn't include one)."""
        tokens = epic_auth.load_tokens(CACHE_DIR)
        if not tokens:
            return {"name": None, "avatar": None}
        return {"name": tokens.get("displayName"), "avatar": None}

    # ── Source / merge settings ───────────────────────────────────────────

    def get_source(self):
        return {"status": "ok", "source": get_setting("epic_source", "galaxy")}

    def set_source(self, source):
        if source not in ("galaxy", "oauth"):
            return {"status": "error", "message": f"Invalid source: {source}"}
        set_setting("epic_source", source)
        return {"status": "ok", "source": source}

    def get_merge(self):
        """Whether Epic games should be folded into the GOG tab (instead of
        getting their own tab).  Useful for small Epic libraries."""
        return {"status": "ok", "enabled": bool(get_setting("merge_epic_into_gog", False))}

    def set_merge(self, enabled):
        set_setting("merge_epic_into_gog", bool(enabled))
        return {"status": "ok", "enabled": bool(enabled)}

    # ── OAuth flow ────────────────────────────────────────────────────────

    def oauth_url(self):
        return {"status": "ok", "url": epic_auth.get_login_url()}

    def oauth_complete(self, code):
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

    def oauth_status(self):
        return {"status": "ok", **epic_auth.get_status(CACHE_DIR)}

    def oauth_disconnect(self):
        epic_auth.clear_tokens(CACHE_DIR)
        try:
            if os.path.isfile(EPIC_LIB_CACHE):
                os.remove(EPIC_LIB_CACHE)
        except OSError:
            pass
        return {"status": "ok"}
