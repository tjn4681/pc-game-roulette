"""
Bulk Steam app-name resolver.

The Steam Web API's ISteamApps/GetAppList endpoint returns 404 since some
2024+ change.  IStoreService/GetAppList requires an API key (we deliberately
don't ask users for one).  So we use SteamSpy's bulk endpoint instead — it
serves paginated lists of Steam apps with names, no key required, no rate
limit on the cached pages.

We use this for cross-platform duplicate detection: to know whether a
user-owned Steam appid is also present on GOG/Epic, we need the appid's
title — and the regular names.json cache only fills lazily as the user spins
games.  This module pulls down a large bulk index once (covers ~30k of the
most popular games), caches it for a week, and merges with the lazy cache.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


# SteamSpy "all" endpoint returns top 1000 by playtime per page.
# Hard limit on pages — top 30k covers virtually every commercial game any
# user would have cross-platform duplicates for.  Long-tail / obscure titles
# fall back to the lazy names.json cache (populated by spinning the game).
SPY_URL       = "https://steamspy.com/api.php?request=all&page={page}"
MAX_PAGES     = 30
CACHE_TTL_SEC = 7 * 86400   # 7 days

USER_AGENT = "SteamRoulette/1.0 (+local app)"


# ─────────────────────────────────────────────────────────────────────────────
#  Fetch
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_page(page, timeout=15):
    """Fetch one SteamSpy 'all' page.  Returns dict appid_str -> {name, ...}
    or empty dict on failure."""
    url = SPY_URL.format(page=page)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return {}


def fetch_steamspy_bulk(max_pages=MAX_PAGES, progress_cb=None,
                        needed_appids=None):
    """Fetch SteamSpy pages 0..max_pages-1.  Returns dict appid_str -> name.

    If `needed_appids` is a set of appid ints/strs we want names for, we stop
    early once every needed appid is covered.  This makes small libraries
    finish much faster.

    `progress_cb(page_index, total_pages, names_so_far)` is called after each
    page fetch — used by the UI to show a progress indicator."""
    names = {}
    needed = None
    if needed_appids:
        needed = {str(a) for a in needed_appids}

    for page in range(max_pages):
        if progress_cb:
            try:
                progress_cb(page, max_pages, len(names))
            except Exception:
                pass

        page_data = _fetch_page(page)
        if not page_data:
            # Page failed — usually means we've gone past the end of the list.
            # Stop fetching; whatever we have is what we have.
            break

        added = 0
        for appid_str, info in page_data.items():
            if not isinstance(info, dict):
                continue
            name = (info.get("name") or "").strip()
            if not name:
                continue
            if appid_str not in names:
                names[appid_str] = name
                added += 1

        # If we have everything the caller needs, stop early
        if needed and needed.issubset(names.keys()):
            break

        # Empty page → end of catalogue
        if added == 0 and page > 0:
            break

        # Light politeness delay between pages
        time.sleep(1.1)

    if progress_cb:
        try:
            progress_cb(page + 1, max_pages, len(names))
        except Exception:
            pass

    return names


# ─────────────────────────────────────────────────────────────────────────────
#  Cache
# ─────────────────────────────────────────────────────────────────────────────

def _cache_path(cache_dir):
    return os.path.join(cache_dir, "steam_names.json")


def load_cache(cache_dir):
    """Load the cached {appid: name} dict, or empty dict if missing/expired."""
    path = _cache_path(cache_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        age = time.time() - cached.get("fetched_at", 0)
        if age > CACHE_TTL_SEC:
            return {}
        return cached.get("names") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def load_cache_any_age(cache_dir):
    """Load the cached {appid: name} dict ignoring the TTL.

    Used for stale-while-revalidate: dedup can keep using slightly-stale names
    (game titles don't change) while a fresh copy is fetched in the background,
    instead of dropping to an empty bulk and blocking the UI."""
    path = _cache_path(cache_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        return cached.get("names") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache_dir, names):
    """Persist the {appid: name} dict to disk (atomic write)."""
    path = _cache_path(cache_dir)
    os.makedirs(cache_dir, exist_ok=True)
    payload = {"fetched_at": time.time(), "names": names}
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


def cache_age_seconds(cache_dir):
    """Return age of the cache file in seconds, or None if no cache."""
    path = _cache_path(cache_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        return time.time() - cached.get("fetched_at", 0)
    except (json.JSONDecodeError, OSError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Public helper combining cache + fetch
# ─────────────────────────────────────────────────────────────────────────────

def get_steam_app_names(cache_dir, force_refresh=False,
                        needed_appids=None, progress_cb=None):
    """Return {appid_str: name} dict.  Uses cache if fresh; otherwise refetches
    from SteamSpy (lazy — only when called and cache is empty/expired)."""
    if not force_refresh:
        cached = load_cache(cache_dir)
        if cached:
            return cached
    names = fetch_steamspy_bulk(
        progress_cb=progress_cb, needed_appids=needed_appids,
    )
    if names:
        save_cache(cache_dir, names)
    return names
