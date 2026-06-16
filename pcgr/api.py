"""
The js_api facade exposed to the frontend.

pywebview bridges the methods of a single object to ``window.pywebview.api.*``,
so this class is the flat API surface the frontend calls.  It owns no domain
logic: it *composes* the launchers and services and delegates each call to the
one that owns it.  Cross-cutting calls that span launchers (detection, combined
connection/user info, settings summaries, debug dumps) are assembled here.

Adding a launcher: implement ``Launcher``, construct it in ``__init__``, add it
to ``self.launchers``, and add the handful of delegators its tab needs.
"""

import json
import os

from pcgr.config import get_setting, load_config, save_config, set_setting
from pcgr.platforms import PLATFORMS
from pcgr.sources.steam_files import parse_shortcuts_vdf
from pcgr.sources.store import load_name_cache
from pcgr.sources.galaxy import tags_as_collections
from pcgr.services.names import NameService
from pcgr.services.art import ArtService
from pcgr.services.filters import FilterService
from pcgr.launchers.steam import SteamLauncher
from pcgr.launchers.gog import GogLauncher
from pcgr.launchers.epic import EpicLauncher
from pcgr.launchers.retroarch import RetroArchLauncher


class SteamRouletteAPI:
    """Methods callable from JavaScript via window.pywebview.api.*"""

    def __init__(self):
        # Services and launchers — composed, not inherited.
        self.names     = NameService()
        self.steam     = SteamLauncher(names=self.names)
        self.gog       = GogLauncher()
        self.epic      = EpicLauncher()
        self.retroarch = RetroArchLauncher()
        self.launchers = {l.id: l for l in
                          (self.steam, self.gog, self.epic, self.retroarch)}
        self.filters   = FilterService(self.steam, self.gog, self.epic, self.names)
        self.art       = ArtService(steam=self.steam)
        self._window   = None
        self._platform_user_cache = None

    def set_window(self, window):
        self._window = window

    # ════════════════════════════════════════════════════════════════════
    #  Steam  (collections, names, installed, API key, shortcuts, excludes)
    # ════════════════════════════════════════════════════════════════════

    def auto_load(self):                       return self.steam.auto_load()
    def select_account(self, path):            return self.steam.select_account(path)
    def reload_collections(self):              return self.steam.reload_collections()
    def get_collections(self):                 return self.steam.get_categories()["collections"]
    def get_installed_games(self):             return self.steam.get_installed_games()
    def get_game_name(self, appid_str):        return self.steam.get_game_name(appid_str)
    def launch_game(self, appid_str):          return self.steam.launch(appid_str)
    def get_game_art(self, appid_str):         return self.art.get_game_art(appid_str)
    def set_steam_api_key(self, key):          return self.steam.set_steam_api_key(key)
    def get_steam_api_key_status(self):        return self.steam.get_steam_api_key_status()
    def clear_steam_api_key(self):             return self.steam.clear_steam_api_key()
    def get_steam_owned_games(self, force_refresh=False):
        return self.steam.get_steam_owned_games(force_refresh)
    def get_shortcuts_with_assignments(self):  return self.steam.get_shortcuts_with_assignments()
    def set_shortcut_collections(self, appid_str, collection_names):
        return self.steam.set_shortcut_collections(appid_str, collection_names)
    def batch_set_shortcut_collections(self, assignments):
        return self.steam.batch_set_shortcut_collections(assignments)
    def toggle_exclude(self, appid_str):       return self.steam.toggle_exclude(appid_str)
    def toggle_hide_collection(self, name):    return self.steam.toggle_hide_collection(name)
    def get_user_info(self):                   return self.steam.get_user_info()

    # ════════════════════════════════════════════════════════════════════
    #  Names / metadata
    # ════════════════════════════════════════════════════════════════════

    def get_steam_names_status(self):          return self.names.get_steam_names_status()
    def refresh_steam_names(self):             return self.names.refresh_steam_names()
    def get_hltb_data(self, appid_str, game_name):
        return self.names.get_hltb_data(appid_str, game_name)

    # ════════════════════════════════════════════════════════════════════
    #  GOG  (and the GOG-Galaxy-integrated launchers)
    # ════════════════════════════════════════════════════════════════════

    def get_gog_games(self):                   return self.gog.get_games()
    def launch_gog_game(self, raw_id, source=None):
        return self.gog.launch(raw_id, source=source)
    def launch_battlenet_game(self, raw_id, source=None):
        return self.gog.launch_battlenet(raw_id)
    def launch_origin_game(self, raw_id, source=None):
        return self.gog.launch_origin(raw_id)
    def launch_uplay_game(self, raw_id, source=None):
        return self.gog.launch_uplay(raw_id)

    # ════════════════════════════════════════════════════════════════════
    #  Epic  (library, OAuth, source/merge settings)
    # ════════════════════════════════════════════════════════════════════

    def get_epic_games(self, force_refresh=False):
        return self.epic.get_games(force_refresh)
    def launch_epic_game(self, raw_id, source=None, app_name=None):
        return self.epic.launch(raw_id, source=source, app_name=app_name)
    def get_epic_source(self):                 return self.epic.get_source()
    def set_epic_source(self, source):         return self.epic.set_source(source)
    def get_epic_merge(self):                  return self.epic.get_merge()
    def set_epic_merge(self, enabled):         return self.epic.set_merge(enabled)
    def epic_oauth_url(self):                  return self.epic.oauth_url()
    def epic_oauth_complete(self, code):       return self.epic.oauth_complete(code)
    def epic_oauth_status(self):               return self.epic.oauth_status()
    def epic_oauth_disconnect(self):           return self.epic.oauth_disconnect()

    def get_galaxy_collections(self, platform):
        """Galaxy tags as collection cards for the GOG or Epic tab (or an
        integrated launcher)."""
        if platform == "gog":
            return self.gog.get_categories()
        if platform == "epic":
            return self.epic.get_categories()
        if platform in ("battlenet", "origin", "uplay"):
            return {"status": "ok", "collections": tags_as_collections((platform,))}
        return {"status": "error", "message": "Invalid platform"}

    # ════════════════════════════════════════════════════════════════════
    #  RetroArch
    # ════════════════════════════════════════════════════════════════════

    def reload_retroarch(self):                return self.retroarch.reload()
    def get_retroarch_playlists(self):         return self.retroarch.get_categories()
    def get_retroarch_games(self, system=None): return self.retroarch.get_games(system)
    def launch_retroarch_game(self, game_id):  return self.retroarch.launch(game_id)

    # ════════════════════════════════════════════════════════════════════
    #  Filters (cross-platform dedup + same-platform editions)
    # ════════════════════════════════════════════════════════════════════

    def get_dedup_settings(self):              return self.filters.get_dedup_settings()
    def set_dedup_settings(self, enabled, priority):
        return self.filters.set_dedup_settings(enabled, priority)
    def get_edition_preference(self):          return self.filters.get_edition_preference()
    def set_edition_preference(self, preference):
        return self.filters.set_edition_preference(preference)
    def get_edition_filter(self, _gog_games=None, _epic_games=None):
        return self.filters.get_edition_filter(_gog_games, _epic_games)
    def get_duplicate_filter(self, _gog_games=None, _epic_games=None):
        return self.filters.get_duplicate_filter(_gog_games, _epic_games)
    def get_all_filters(self):                 return self.filters.get_all_filters()

    # ════════════════════════════════════════════════════════════════════
    #  Cross-cutting: detection, status, user info, settings
    # ════════════════════════════════════════════════════════════════════

    def detect_platforms(self):
        """Report which of the supported launchers we found on this PC.  Used by
        the frontend to pick the default tab and show a friendly empty state.
        Cheap — only stats file paths and registry."""
        present = {pid: l.is_present() for pid, l in self.launchers.items()}
        return {
            "status": "ok",
            **present,
            "any": any(present.values()),
            "epic_oauth_connected": self.epic.oauth_connected(),
        }

    def get_launcher_connection_status(self):
        """Per-launcher {connected, name, count, source} for the Settings UI."""
        info = self.get_platform_user_info()
        out = {"status": "ok"}
        for pid in ("steam", "gog", "epic"):
            status = self.launchers[pid].connection_status()
            status["name"] = info.get(pid, {}).get("name")
            out[pid] = status
        return out

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

    def get_platform_user_info(self):
        """Return {steam, gog, epic} each {name, avatar} for the connected user.
        Cached for the process lifetime; reload_platform_user_info() refreshes."""
        if self._platform_user_cache is None:
            self._platform_user_cache = {
                "steam": self.steam.user(),
                "gog":   self.gog.user(),
                "epic":  self.epic.user(),
            }
        return {"status": "ok", **self._platform_user_cache}

    def reload_platform_user_info(self):
        """Clear the per-platform user cache so the next call re-fetches."""
        self._platform_user_cache = None
        return self.get_platform_user_info()

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

    # ── Sound / onboarding ────────────────────────────────────────────────

    def get_sound_enabled(self):
        """Whether reel tick / landing sounds are enabled."""
        return {"status": "ok", "enabled": bool(get_setting("sound_enabled", True))}

    def set_sound_enabled(self, enabled):
        """Toggle reel tick / landing sounds."""
        set_setting("sound_enabled", bool(enabled))
        return {"status": "ok", "enabled": bool(enabled)}

    def get_onboarding_state(self):
        """Whether to show the first-run welcome (with the optional Steam API
        key prompt)."""
        return {
            "status":         "ok",
            "onboarded":      bool(get_setting("onboarded", False)),
            "steam_detected": self.steam.is_present(),
            "has_key":        self.steam.get_steam_api_key_status()["has_key"],
        }

    def dismiss_onboarding(self):
        """Mark the welcome as seen so it doesn't show again."""
        set_setting("onboarded", True)
        return {"status": "ok"}

    # ── GOG / Epic exclusions (config-only; keyed by prefixed game id) ─────

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

    def get_settings(self):
        """List currently-hidden collections and currently-excluded games,
        with resolved names so the Settings UI can display them."""
        cfg = load_config()
        excluded_ids = sorted(cfg.get("excluded_appids", []))
        cache = load_name_cache()
        shortcuts = self.steam.shortcuts
        excluded = []
        for appid in excluded_ids:
            name = cache.get(str(appid))
            if not name and appid in shortcuts:
                name = shortcuts[appid].get("name")
            if not name:
                name = f"App {appid}"
            excluded.append({"appid": appid, "name": name})
        hidden_names = sorted(cfg.get("hidden_collections", []), key=str.lower)
        collections = self.steam.collections
        hidden = [{"name": n, "count": len(collections.get(n, []))} for n in hidden_names]
        # Platform-side (GOG/Epic) excludes — separate list with prefixed IDs
        platform_excluded_dict = cfg.get("excluded_platform_games", {}) or {}
        platform_excluded = []
        for gid, name in sorted(platform_excluded_dict.items(),
                                key=lambda kv: (kv[1] or kv[0]).lower()):
            platform = gid.split("_", 1)[0] if "_" in gid else ""
            platform_excluded.append({"id": gid, "name": name or gid, "platform": platform})
        return {
            "status":                  "ok",
            "excluded_games":          excluded,
            "excluded_platform_games": platform_excluded,
            "hidden_collections":      hidden,
        }

    # ════════════════════════════════════════════════════════════════════
    #  Diagnostics (Steam Collections / shortcuts dumps + file dialogs)
    # ════════════════════════════════════════════════════════════════════

    def debug_shortcuts(self):
        """Inspect shortcuts.vdf: does it exist, how many shortcuts, what tags?"""
        collections_path = self.steam.collections_path
        if not collections_path:
            return {"status": "error", "message": "No collections loaded yet."}

        config_dir = os.path.dirname(os.path.dirname(collections_path))
        vdf_path   = os.path.join(config_dir, "shortcuts.vdf")
        collections = self.steam.collections
        result = {
            "status":           "ok",
            "vdf_path":         vdf_path,
            "vdf_exists":       os.path.isfile(vdf_path),
            "collection_names": sorted(collections.keys()),
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
        coll_lower = {n.lower() for n in collections.keys()}
        result["unique_tags"]               = sorted(all_tags)
        result["tags_matching_collections"] = sorted(t for t in all_tags if t.strip().lower() in coll_lower)
        result["tags_not_matching"]         = sorted(t for t in all_tags if t.strip().lower() not in coll_lower)
        return result

    def debug_all_keys(self):
        """Comprehensive debug: prefix breakdown + searches for where Steam
        actually stores shortcut→collection memberships."""
        collections_path = self.steam.collections_path
        if not collections_path or not os.path.isfile(collections_path):
            return {"status": "error", "message": "No collections file loaded."}
        try:
            with open(collections_path, "r", encoding="utf-8") as f:
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
        shortcuts = self.steam.shortcuts
        shortcut_id_strs = set()
        for a in shortcuts.keys():
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
        folder = os.path.dirname(collections_path)
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
        parts = os.path.normpath(collections_path).split(os.sep)
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
            collection_names = list(self.steam.collections.keys())
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
        collections_path = self.steam.collections_path
        if not collections_path or not os.path.isfile(collections_path):
            return {"status": "error", "message": "No collections file loaded."}
        try:
            with open(collections_path, "r", encoding="utf-8") as f:
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
        return self.steam.load_from_path(path)
