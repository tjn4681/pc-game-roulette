"""
LaunchStatusMixin for SteamRouletteAPI.

Platform detection / status and game launching: which launchers are
present and connected, enabling/disabling tabs, and the per-launcher
launch URIs.

Methods here run on the single js_api object via cooperative multiple
inheritance, so they share instance state (self.*) set up in the core
SteamRouletteAPI.__init__.
"""

import epic_auth
import json
import os

from appconfig import CACHE_DIR, EPIC_LIB_CACHE, GOG_GALAXY_DB, get_setting, set_setting
from platforms import PLATFORMS, find_epic_manifests_dir
from steam_library import find_steam_path
from galaxy import query_galaxy_db_for_platform


class LaunchStatusMixin:
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
