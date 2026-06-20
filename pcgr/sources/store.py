"""
Resolving and caching game display names.

On-demand lookups against Steam's public endpoints (no API key):
  * appdetails  — appid → name
  * storesearch — name  → appid (the reverse, used to bridge a GOG/Epic title
    to its Steam appid for cross-platform dedup)
  * SteamSpy    — fallback for old/delisted titles the store API misses

Plus the two on-disk caches that back them: the appid→name cache (names.json)
and the "already searched on the store" ledger that keeps the reverse-search
from re-crawling the whole library every launch.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from pcgr.config import CACHE_DIR, NAMES_CACHE


# ── Steam store API + SteamSpy (name fallbacks) ──────────────────────────────

def fetch_name_from_api(appid):
    """Call Steam store appdetails API. Returns name str or None.
    Sends mature-content cookies so age-gated titles resolve instead of
    falling through to 'App <appid>'."""
    url = (f"https://store.steampowered.com/api/appdetails"
           f"?appids={appid}&filters=basic&cc=us&l=english")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Cookie":     "birthtime=283993201; mature_content=1; "
                          "wants_mature_content=1; lastagecheckage=1-0-1979",
            "Referer":    "https://store.steampowered.com/",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        entry = data.get(str(appid), {})
        if entry.get("success") and "data" in entry:
            return entry["data"].get("name")
    except Exception:
        pass
    return None


def search_steam_store(term, timeout=8):
    """Search the Steam store by title.  Returns [(appid:int, name:str), ...].

    No API key.  Used to resolve a cross-platform game's Steam appid from its
    name — the reverse of fetch_name_from_api (which goes appid -> name)."""
    url = ("https://store.steampowered.com/api/storesearch/?"
           + urllib.parse.urlencode({"term": term, "cc": "us", "l": "en"}))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    out = []
    for it in (data.get("items") or []):
        aid, nm = it.get("id"), it.get("name")
        if isinstance(aid, int) and nm:
            out.append((aid, nm))
    return out


def fetch_name_from_steamspy(appid):
    """SteamSpy fallback — works for old/delisted games the store API misses."""
    url = f"https://steamspy.com/api.php?request=appdetails&appid={appid}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        name = data.get("name", "")
        if name and name not in ("0", ""):
            return name
    except Exception:
        pass
    return None


def fetch_genres(appid):
    """Return a list of Steam genre names for `appid`, or None on failure.

    An empty list [] means 'fetched successfully, but the game has no genres' —
    callers should cache that so it isn't re-fetched.  None means the lookup
    failed (network/again-later) and should NOT be cached.

    Primary source: Steam appdetails (filters=genres, small payload).  Fallback:
    SteamSpy (comma-separated genre string), which covers some delisted games."""
    url = (f"https://store.steampowered.com/api/appdetails"
           f"?appids={appid}&filters=genres&cc=us&l=english")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Cookie":     "birthtime=283993201; mature_content=1; "
                          "wants_mature_content=1; lastagecheckage=1-0-1979",
            "Referer":    "https://store.steampowered.com/",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        entry = data.get(str(appid), {})
        if entry.get("success"):
            genres = (entry.get("data") or {}).get("genres") or []
            return [g.get("description", "").strip()
                    for g in genres if g.get("description")]
    except Exception:
        pass
    # SteamSpy fallback
    try:
        url2 = f"https://steamspy.com/api.php?request=appdetails&appid={appid}"
        req2 = urllib.request.Request(url2, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = (data.get("genre") or "").strip()
        if raw:
            return [x.strip() for x in raw.split(",") if x.strip()]
    except Exception:
        pass
    return None


# ── Name cache ────────────────────────────────────────────────────────────────

def load_name_cache():
    if os.path.isfile(NAMES_CACHE):
        try:
            with open(NAMES_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


_NAME_CACHE_LOCK = threading.Lock()


def save_name_cache(cache):
    """Atomically persist the name cache.

    Atomic (write-temp-then-replace) + locked so the background name-warmer
    thread and the main js_api thread can't corrupt the JSON by writing
    concurrently.
    """
    with _NAME_CACHE_LOCK:
        tmp = NAMES_CACHE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp, NAMES_CACHE)
        except OSError:
            # Best-effort: clean up the temp file if the replace failed
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass


# ── Genre cache ───────────────────────────────────────────────────────────────

GENRES_CACHE = os.path.join(CACHE_DIR, "genres.json")
_GENRE_CACHE_LOCK = threading.Lock()


def load_genre_cache():
    if os.path.isfile(GENRES_CACHE):
        try:
            with open(GENRES_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_genre_cache(cache):
    """Atomically persist the genre cache (write-temp-then-replace), locked so
    the background warmer and the js_api thread can't corrupt the JSON."""
    with _GENRE_CACHE_LOCK:
        tmp = GENRES_CACHE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp, GENRES_CACHE)
        except OSError:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass


def merge_genre_cache(new_genres):
    """Read-merge-write the genre cache; only adds appids not already cached."""
    if not new_genres:
        return
    cache = load_genre_cache()
    changed = False
    for k, v in new_genres.items():
        if k not in cache:
            cache[k] = v
            changed = True
    if changed:
        save_genre_cache(cache)


# ── Cross-platform "already searched" cache ──────────────────────────────────
# Normalized GOG/Epic titles we've already looked up on the Steam store, with a
# timestamp.  Lets the reverse-search skip titles it has seen — both the ones
# that matched (now in the name cache) and the GOG-exclusive ones that didn't —
# instead of re-searching the whole library on every launch.  TTL'd so a game
# you later buy on Steam still gets re-checked.
_XPLAT_SEARCHED = os.path.join(CACHE_DIR, "xplat_searched.json")
_XPLAT_SEARCH_TTL = 21 * 86400   # 3 weeks


def load_xplat_searched():
    try:
        with open(_XPLAT_SEARCHED, "r", encoding="utf-8") as f:
            data = json.load(f)
        cutoff = time.time() - _XPLAT_SEARCH_TTL
        return {k: v for k, v in data.items() if isinstance(v, (int, float)) and v >= cutoff}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def save_xplat_searched(d):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = _XPLAT_SEARCHED + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _XPLAT_SEARCHED)
    except OSError:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
