"""
Optional Steam Web API client.

The keyless paths (collections, installed manifests) only see installed or
manually-categorised games.  Steam locked down anonymous profile scraping, so
the only reliable way to read a user's FULL owned library is the official Steam
Web API — which needs a free key the user generates once at
https://steamcommunity.com/dev/apikey .

This is entirely opt-in: with no key the app behaves exactly as before.  When a
key is provided we call IPlayerService/GetOwnedGames (which returns appids AND
names), cache the result, and use it as the "Whole Library".

Stdlib only.  The key travels as a query parameter because that's how Steam's
API works — but only ever over HTTPS straight to api.steampowered.com, never to
any third party, and it's stored DPAPI-encrypted on disk (see backend).
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

OWNED_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
CACHE_TTL_SEC = 6 * 3600          # re-fetch owned library at most every 6h
_CACHE_NAME = "steam_owned.json"


def validate_key_format(key):
    """Steam Web API keys are 32 hex characters.  Cheap client-side sanity
    check before we bother hitting the network."""
    if not key:
        return False
    key = key.strip()
    return len(key) == 32 and all(c in "0123456789abcdefABCDEF" for c in key)


def fetch_owned(api_key, steamid64, timeout=15):
    """Call GetOwnedGames.  Returns (games, status):

      games  = [{'appid': int, 'name': str}, ...]
      status = 'ok' | 'unauthorized' | 'private' | 'error'

    'unauthorized' = bad/expired key; 'private' = key works but the target's
    game details aren't visible (GetOwnedGames returns an empty response)."""
    params = urllib.parse.urlencode({
        "key": api_key.strip(),
        "steamid": str(steamid64),
        "include_appinfo": 1,
        "include_played_free_games": 1,
        "format": "json",
    })
    try:
        req = urllib.request.Request(OWNED_URL + "?" + params,
                                     headers={"User-Agent": "SteamRoulette/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 401/403 = invalid key; 429 = rate-limited (treat as transient error)
        if e.code in (401, 403):
            return [], "unauthorized"
        return [], "error"
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return [], "error"

    response = data.get("response")
    if not isinstance(response, dict):
        return [], "error"
    # A valid key against a profile with private game details returns an empty
    # response object ({} — no 'games', no 'game_count').
    if "games" not in response:
        return [], "private"
    games = []
    for g in (response.get("games") or []):
        appid = g.get("appid")
        if appid:
            games.append({"appid": int(appid), "name": (g.get("name") or "").strip()})
    return games, "ok"


# ── Disk cache (keyed to the steamid so switching accounts doesn't mix up) ────

def _cache_path(cache_dir):
    return os.path.join(cache_dir, _CACHE_NAME)


def load_cache(cache_dir, steamid64):
    path = _cache_path(cache_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if str(data.get("steamid64")) != str(steamid64):
        return None
    if time.time() - data.get("fetched_at", 0) > CACHE_TTL_SEC:
        return None
    return data


def save_cache(cache_dir, steamid64, status, games):
    os.makedirs(cache_dir, exist_ok=True)
    path = _cache_path(cache_dir)
    payload = {"steamid64": str(steamid64), "fetched_at": time.time(),
               "status": status, "games": games}
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


def clear_cache(cache_dir):
    path = _cache_path(cache_dir)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def get_owned(cache_dir, api_key, steamid64, force_refresh=False):
    """Cached GetOwnedGames.  Returns {status, games, cached}.

    Caches successful AND 'private' results (so we don't hammer the API), but
    not transient network errors or auth failures."""
    if not force_refresh:
        cached = load_cache(cache_dir, steamid64)
        if cached is not None:
            return {"status": cached.get("status", "ok"),
                    "games": cached.get("games", []), "cached": True}
    games, status = fetch_owned(api_key, steamid64)
    if status in ("ok", "private"):
        save_cache(cache_dir, steamid64, status, games)
    return {"status": status, "games": games, "cached": False}
