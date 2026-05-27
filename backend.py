"""
PC Game Roulette backend — Steam / GOG / Epic detection, collections parsing,
name fetching, cross-platform dedup, OAuth, config.

The class kept its historical SteamRouletteAPI name because it's referenced
by main.py and any future migrations would just churn diffs.

Exposed to the frontend via js_api in main.py.
"""

import base64
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import winreg

import epic_auth
import epic_api
import steam_names


# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE     = os.path.join(SCRIPT_DIR, "config.json")
CACHE_DIR       = os.path.join(SCRIPT_DIR, "cache")
NAMES_CACHE     = os.path.join(CACHE_DIR,  "names.json")
ART_CACHE_DIR   = os.path.join(CACHE_DIR,  "art")
HLTB_CACHE      = os.path.join(CACHE_DIR,  "hltb.json")
EPIC_LIB_CACHE  = os.path.join(CACHE_DIR,  "epic_library.json")

# How long to trust the cached Epic library before re-fetching from the API.
# Reloading manually from the UI bypasses this cache.
EPIC_LIB_CACHE_TTL_SECONDS = 60 * 60   # 1 hour

# GOG Galaxy stores its full owned library — across GOG itself and any platform
# integrations the user has set up (Epic, Steam, Origin, etc.) — in a SQLite DB
# under %ProgramData%.  We use the env var so this works for users whose
# Windows install puts ProgramData on a non-C: drive.
_PROGRAM_DATA = os.environ.get("ProgramData") or r"C:\ProgramData"
GOG_GALAXY_DB = os.path.join(
    _PROGRAM_DATA, "GOG.com", "Galaxy", "storage", "galaxy-2.0.db",
)


# ── Platform registry ─────────────────────────────────────────────────────
#
# Single source of truth for which launchers we support and the metadata each
# one needs across the rest of the codebase.  Adding a new launcher should
# only require: adding an entry here, adding its detector + games getter, and
# (in the frontend) adding the tab markup + a brand color in CSS.
#
# Note: only describes *metadata*.  Detection and fetching live in dedicated
# methods on SteamRouletteAPI because each launcher has unique quirks.

PLATFORMS = {
    "steam": {"id": "steam", "name": "Steam",
              "galaxy_prefix": "steam",
              "default_priority": 0},
    "gog":   {"id": "gog",   "name": "GOG",
              "galaxy_prefix": "gog",
              "default_priority": 1},
    "epic":  {"id": "epic",  "name": "Epic Games",
              "galaxy_prefix": "epic",
              "default_priority": 2},
}

# Launchers that are NOT first-class tabs but whose games appear in the GOG
# tab when the user has integrated them in GOG Galaxy.
_GOG_INTEGRATED_PREFIXES = ("battlenet", "origin", "uplay")

# Battle.net URL-protocol codes (used when launching via the GOG Galaxy URI).
# Galaxy stores internal codenames; the protocol expects marketing codes.
BATTLENET_LAUNCH_CODES = {
    "wow":         "WoW",   "wow_classic": "WoWC",
    "d2":          "D2",    "d2LOD":       "D2",
    "osi":         "OSI",   "diablo3":     "D3",
    "fenrispup":   "Fen",   "fenris":      "Fen",
    "prometheus":  "Pro",   "s1":          "S1",
    "s2":          "S2",    "w3":          "W3",
    "w3tft":       "War3",  "heroes":      "Hero",
    "hs_beta":     "WTCG",  "odin":        "ODIN",
    "zeus":        "ZEUS",  "fore":        "FORE",
    "lazr":        "LAZR",  "viper":       "VIPER",
}


def find_epic_manifests_dir():
    """Locate Epic Games Launcher's Manifests folder.

    Epic helpfully stores its AppDataPath under
      HKLM\\SOFTWARE\\WOW6432Node\\Epic Games\\EpicGamesLauncher\\AppDataPath
    so we consult the registry first (works even if Epic is installed to a
    non-default drive).  Falls back to the standard %ProgramData% location."""
    candidates = []
    for hive, subkey in [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Epic Games\EpicGamesLauncher"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Epic Games\EpicGamesLauncher"),
    ]:
        try:
            with winreg.OpenKey(hive, subkey) as k:
                val, _ = winreg.QueryValueEx(k, "AppDataPath")
                if val:
                    candidates.append(os.path.join(val, "Manifests"))
        except OSError:
            continue
    # Fallback: %ProgramData%\Epic\EpicGamesLauncher\Data\Manifests
    candidates.append(os.path.join(
        _PROGRAM_DATA, "Epic", "EpicGamesLauncher", "Data", "Manifests",
    ))
    for p in candidates:
        if os.path.isdir(p):
            return p
    return None

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(ART_CACHE_DIR, exist_ok=True)


# ── Steam path detection ──────────────────────────────────────────────────────

def find_steam_path_registry():
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Valve\Steam"),
    ]
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as k:
                val, _ = winreg.QueryValueEx(k, "InstallPath")
                if val and os.path.isdir(val):
                    return val
        except OSError:
            continue
    return None


def find_steam_path():
    path = find_steam_path_registry()
    if path:
        return path
    for p in [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]:
        if os.path.isdir(p):
            return p
    return None


# ── Collections file discovery ────────────────────────────────────────────────

COLLECTIONS_FILENAME = "cloud-storage-namespace-1.json"


def find_collections_files(steam_path):
    """Return list of (account_id, absolute_path) tuples."""
    userdata = os.path.join(steam_path, "userdata")
    if not os.path.isdir(userdata):
        return []
    results = []
    try:
        for account_id in os.listdir(userdata):
            candidate = os.path.join(
                userdata, account_id, "config", "cloudstorage", COLLECTIONS_FILENAME
            )
            if os.path.isfile(candidate):
                results.append((account_id, candidate))
    except OSError:
        pass
    return results


# ── Collections parsing ───────────────────────────────────────────────────────

def parse_collections(path):
    """
    Parse cloud-storage-namespace-1.json.
    Returns {collection_name: [app_id_int, ...]} sorted by name.
    Skips deleted entries and empty-named collections.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    collections = {}
    for entry in data:
        key = entry[0]
        if not key.startswith("user-collections.uc-"):
            continue
        meta = entry[1]
        if meta.get("is_deleted"):
            continue
        if "value" not in meta:
            continue
        try:
            value = json.loads(meta["value"])
        except (json.JSONDecodeError, KeyError):
            continue

        name = value.get("name", "").strip()
        if not name:
            continue

        app_ids = []
        for aid in value.get("added", []):
            # Steam stores entries in a few different shapes:
            #   plain int  (Steam game)            : 12345
            #   string     (Steam game)            : "12345"
            #   prefixed   (Steam game)            : "app/12345"
            #   prefixed   (non-Steam shortcut)    : "shortcut/3713503516"
            #   dict       (some recent versions)  : {"appid": 12345, "type": "..."}
            if isinstance(aid, dict):
                aid = aid.get("appid") or aid.get("id") or aid.get("game_id")
                if aid is None:
                    continue
            aid_str = str(aid).strip()
            if "/" in aid_str:
                aid_str = aid_str.rsplit("/", 1)[-1]
            try:
                app_ids.append(int(aid_str))
            except (ValueError, TypeError):
                pass

        collections[name] = app_ids

    return dict(sorted(collections.items(), key=lambda kv: kv[0].lower()))


# ── shortcuts.vdf (non-Steam shortcuts) ───────────────────────────────────────
#
# Steam stores non-Steam shortcut collection memberships in this binary VDF
# file under userdata/<account>/config/shortcuts.vdf, NOT in cloud-storage-
# namespace-1.json.  Each shortcut has a `tags` object whose values are the
# collection names the user added it to.

def parse_shortcuts_vdf(path):
    """Parse Steam's binary shortcuts.vdf.
    Returns list of {'appid': unsigned int, 'name': str, 'tags': [str, ...]}."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []

    pos = [0]

    def read_cstring():
        if pos[0] >= len(data):
            return None
        end = data.find(b"\x00", pos[0])
        if end < 0:
            return None
        s = data[pos[0]:end]
        pos[0] = end + 1
        return s.decode("utf-8", errors="replace")

    def parse_obj():
        result = {}
        while pos[0] < len(data):
            t = data[pos[0]]
            pos[0] += 1
            if t == 0x08:   # end of compound
                return result
            key = read_cstring()
            if key is None:
                return result
            if t == 0x00:   # nested object
                result[key] = parse_obj()
            elif t == 0x01: # string
                v = read_cstring()
                if v is None:
                    return result
                result[key] = v
            elif t == 0x02: # int32 — read unsigned so shortcut IDs come out positive
                if pos[0] + 4 > len(data):
                    return result
                result[key] = int.from_bytes(data[pos[0]:pos[0]+4], "little", signed=False)
                pos[0] += 4
            elif t == 0x07: # uint64
                if pos[0] + 8 > len(data):
                    return result
                result[key] = int.from_bytes(data[pos[0]:pos[0]+8], "little", signed=False)
                pos[0] += 8
            else:
                return result  # unknown type — bail
        return result

    if not data:
        return []
    if data[pos[0]] != 0x00:
        return []
    pos[0] += 1
    read_cstring()              # consume top-level key ("shortcuts")
    top = parse_obj()

    shortcuts = []
    for entry in top.values():
        if not isinstance(entry, dict):
            continue
        appid = entry.get("appid")
        if appid is None:
            continue
        name = (entry.get("AppName") or entry.get("appname") or
                entry.get("Appname") or "").strip()
        tags_obj = entry.get("tags") or {}
        tags = []
        if isinstance(tags_obj, dict):
            for k in sorted(tags_obj.keys(),
                            key=lambda x: int(x) if str(x).isdigit() else 0):
                v = tags_obj[k]
                if isinstance(v, str):
                    tags.append(v)
        shortcuts.append({"appid": appid, "name": name, "tags": tags})
    return shortcuts


def find_shortcuts_vdf_for(collections_path):
    """Return the shortcuts.vdf that sits in the same account folder as the
    given cloud-storage collections JSON, or None."""
    # collections_path = .../userdata/<id>/config/cloudstorage/cloud-storage-...json
    # shortcuts.vdf    = .../userdata/<id>/config/shortcuts.vdf
    config_dir = os.path.dirname(os.path.dirname(collections_path))
    candidate = os.path.join(config_dir, "shortcuts.vdf")
    return candidate if os.path.isfile(candidate) else None


def merge_shortcuts_into_collections(collections, shortcuts):
    """For each shortcut, add its appid to every collection whose name matches
    one of the shortcut's tags (case-insensitive)."""
    name_to_key = {name.lower(): name for name in collections.keys()}
    for sc in shortcuts:
        appid = sc.get("appid")
        if not appid:
            continue
        for tag in sc.get("tags", []):
            key = name_to_key.get(tag.strip().lower())
            if key and appid not in collections[key]:
                collections[key].append(appid)
    return collections


# ── Steam library folders + ACF manifests ────────────────────────────────────

def find_library_folders(steam_path):
    """Return all Steam library root paths (default + extras from libraryfolders.vdf)."""
    folders = [steam_path]
    vdf = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(vdf):
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            for m in re.finditer(r'"path"\s+"([^"]+)"', content):
                path = m.group(1).replace("\\\\", "\\")
                if os.path.isdir(path) and path not in folders:
                    folders.append(path)
        except OSError:
            pass
    return folders


def read_acf_name(acf_path):
    """Return (appid_int, name_str) from an appmanifest .acf file, or (None, None)."""
    try:
        with open(acf_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        appid_m = re.search(r'"appid"\s+"(\d+)"', content)
        name_m  = re.search(r'"name"\s+"([^"]+)"', content)
        if appid_m and name_m:
            return int(appid_m.group(1)), name_m.group(1)
    except OSError:
        pass
    return None, None


def lookup_acf_name(steam_path, appid):
    """Fast O(1) lookup of a single game's name from its appmanifest file."""
    for folder in find_library_folders(steam_path):
        acf = os.path.join(folder, "steamapps", f"appmanifest_{appid}.acf")
        if os.path.isfile(acf):
            _, name = read_acf_name(acf)
            if name:
                return name
    return None


def scan_all_acf(steam_path):
    """Scan all Steam libraries and return {appid_int: name} for every installed game."""
    games = {}
    for folder in find_library_folders(steam_path):
        steamapps = os.path.join(folder, "steamapps")
        if not os.path.isdir(steamapps):
            continue
        try:
            for fname in os.listdir(steamapps):
                if fname.startswith("appmanifest_") and fname.endswith(".acf"):
                    appid, name = read_acf_name(os.path.join(steamapps, fname))
                    if appid and name:
                        games[appid] = name
        except OSError:
            pass
    return games


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


# ── Config persistence ────────────────────────────────────────────────────────

def load_config():
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def get_setting(key, default=None):
    """Read a single key out of config.json, returning default if missing.
    Use this for any per-user setting that needs to persist across launches."""
    cfg = load_config()
    return cfg.get(key, default)


def set_setting(key, value):
    """Persist a single key/value pair to config.json without disturbing other
    keys.  Safe to call concurrently with other config reads/writes (last
    writer wins — there's no multi-process contention to worry about)."""
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)


# ── Name cache ────────────────────────────────────────────────────────────────

def load_name_cache():
    if os.path.isfile(NAMES_CACHE):
        try:
            with open(NAMES_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_name_cache(cache):
    with open(NAMES_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ── Playtime parser (localconfig.vdf) ─────────────────────────────────────────

_VDF_BLOCK_RE    = re.compile(r'"(\d+)"\s*\{')
_VDF_PLAYTIME_RE = re.compile(r'"Playtime"\s*"(\d+)"')


def parse_playtimes_from_localconfig(path):
    """Return {appid: minutes} by scanning Steam's localconfig.vdf.
    Walks each `"<digits>" { ... }` block (the per-app config entries) and
    pulls out the Playtime value if present."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return {}

    playtimes = {}
    for m in _VDF_BLOCK_RE.finditer(content):
        try:
            appid = int(m.group(1))
        except ValueError:
            continue
        if appid < 10:  # skip tag indices like "0", "1"
            continue
        start = m.end()
        depth = 1
        i = start
        n = len(content)
        while i < n and depth > 0:
            c = content[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            continue
        block = content[start:i]
        pm = _VDF_PLAYTIME_RE.search(block)
        if not pm:
            continue
        try:
            minutes = int(pm.group(1))
        except ValueError:
            continue
        if minutes > 0:
            playtimes[appid] = minutes
    return playtimes


# ── Image helpers (winner art) ────────────────────────────────────────────────

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC  = b"\x89PNG\r\n\x1a\n"


def _image_mime(data):
    if data[:3] == _JPEG_MAGIC:
        return "image/jpeg"
    if data[:8] == _PNG_MAGIC:
        return "image/png"
    return None


def _read_image_as_data_url(path):
    """Return data URL for a local image, or None if not a valid JPEG/PNG."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    mime = _image_mime(data)
    if not mime or len(data) < 2000:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _find_grid_images(steam_path, appid):
    """Yield paths to user-uploaded grid art for a (non-Steam) shortcut.
    Steam stores custom artwork at userdata/<id>/config/grid/<appid>.<ext>."""
    userdata = os.path.join(steam_path, "userdata")
    if not os.path.isdir(userdata):
        return
    try:
        accounts = os.listdir(userdata)
    except OSError:
        return
    # Steam grids use several filename conventions — try the header-shaped ones
    candidates = [f"{appid}.jpg", f"{appid}.png", f"{appid}_header.jpg", f"{appid}_header.png"]
    for account_id in accounts:
        grid_dir = os.path.join(userdata, account_id, "config", "grid")
        if not os.path.isdir(grid_dir):
            continue
        for fname in candidates:
            path = os.path.join(grid_dir, fname)
            if os.path.isfile(path):
                yield path


def _try_fetch_image(url, headers, timeout=8):
    """Fetch URL via urllib, return raw bytes only if they're a real image."""
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        if _image_mime(data) and len(data) > 2000:
            return data
    except Exception:
        pass
    return None


def _cache_and_return(appid, data):
    """Save image bytes to disk cache and return a {status, data} response."""
    mime = _image_mime(data)
    ext  = "png" if mime == "image/png" else "jpg"
    cache_path = os.path.join(ART_CACHE_DIR, f"{appid}.{ext}")
    try:
        with open(cache_path, "wb") as f:
            f.write(data)
    except OSError:
        pass
    b64 = base64.b64encode(data).decode("ascii")
    return {"status": "ok", "data": f"data:{mime};base64,{b64}"}


# ── GOG Galaxy database (full owned library across all integrations) ─────────

def _galaxy_db_open():
    """Open Galaxy's SQLite DB read-only, or return None if unavailable."""
    if not os.path.isfile(GOG_GALAXY_DB):
        return None
    try:
        return sqlite3.connect(
            f"file:{GOG_GALAXY_DB}?mode=ro", uri=True, timeout=2.0,
        )
    except sqlite3.Error:
        return None


def query_galaxy_enrichment(release_keys):
    """For a list of releaseKeys (like ``['gog_1207659146', 'epic_<id>', ...]``)
    return a dict mapping releaseKey -> enrichment payload:

        {
          'playtime_minutes': int,
          'tags':             [str, ...],
          'image_background': str (url, may be empty),
          'image_vertical':   str (url),
          'image_icon':       str (url),
        }

    Missing rows simply yield empty fields — callers can blindly look up any
    releaseKey and get a usable dict back.  Single connection, three small
    queries, batched with SQL ``IN`` so it scales to thousands of games."""
    if not release_keys:
        return {}

    conn = _galaxy_db_open()
    if conn is None:
        return {}

    # Build a default-filled dict for every requested key — callers don't
    # need to .get() defensively on each field.
    result = {}
    for k in release_keys:
        result[k] = {
            "playtime_minutes": 0,
            "tags":             [],
            "image_background": "",
            "image_vertical":   "",
            "image_icon":       "",
        }

    # SQLite has a 999-parameter limit per statement by default.  Chunk safely.
    chunk_size = 800
    keys = list(release_keys)

    try:
        cur = conn.cursor()

        # GamePieceType id for originalImages — looked up once, reused.
        cur.execute(
            "SELECT id FROM GamePieceTypes WHERE type = 'originalImages' LIMIT 1"
        )
        row = cur.fetchone()
        images_type_id = row[0] if row else None

        for i in range(0, len(keys), chunk_size):
            chunk = keys[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))

            # Playtime
            cur.execute(
                f"SELECT releaseKey, minutesInGame FROM GameTimes "
                f"WHERE releaseKey IN ({placeholders})",
                chunk,
            )
            for rk, minutes in cur.fetchall():
                if rk in result and isinstance(minutes, int) and minutes > 0:
                    result[rk]["playtime_minutes"] = minutes

            # Tags (a game can have multiple tag rows)
            cur.execute(
                f"SELECT releaseKey, tag FROM UserReleaseTags "
                f"WHERE releaseKey IN ({placeholders}) AND tag IS NOT NULL",
                chunk,
            )
            for rk, tag in cur.fetchall():
                if rk in result and tag:
                    tag = tag.strip()
                    if tag and tag not in result[rk]["tags"]:
                        result[rk]["tags"].append(tag)

            # Image URLs from originalImages piece
            if images_type_id is not None:
                cur.execute(
                    f"SELECT releaseKey, value FROM GamePieces "
                    f"WHERE gamePieceTypeId = ? AND releaseKey IN ({placeholders})",
                    [images_type_id] + list(chunk),
                )
                for rk, value in cur.fetchall():
                    if rk not in result or not value:
                        continue
                    try:
                        parsed = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    result[rk]["image_background"] = parsed.get("background", "") or ""
                    result[rk]["image_vertical"]   = parsed.get("verticalCover", "") or ""
                    result[rk]["image_icon"]       = parsed.get("squareIcon", "") or ""

        # Sort tags alphabetically for stable ordering
        for rk in result:
            result[rk]["tags"].sort(key=str.lower)
    except sqlite3.Error:
        # Partial result is still useful — return what we managed to gather
        pass
    finally:
        conn.close()

    return result


def _galaxy_lookup_key(game, platform):
    """Pick the releaseKey Galaxy uses for this game.

    For Epic specifically, Galaxy keys on AppName (e.g. 'epic_Cowbird'), NOT
    the catalogItemId Epic's OAuth API returns.  So when we have both (OAuth
    library populates 'app_name'), prefer that — it's the only way to bridge
    OAuth-sourced games to Galaxy-sourced enrichment.

    For GOG, the raw_id (gameID) IS the suffix Galaxy uses.
    For Steam, the appid IS the suffix.
    """
    if platform == "epic" and game.get("app_name"):
        return f"epic_{game['app_name']}"
    gid = game.get("id") or ""
    if "_" in gid:
        return gid
    if platform and game.get("raw_id"):
        return f"{platform}_{game['raw_id']}"
    return None


# ── Cross-platform duplicate detection ───────────────────────────────────────
#
# Goal: identify when "Batman: Arkham City" on Steam is the same product as
# "Batman Arkham City Game of the Year Edition" on Epic — different IDs,
# different exact strings, same game.  The trick is normalizing titles
# aggressively before comparing.

# Edition / variant suffixes that should be stripped before comparison.
# Order matters: LONGER phrases are listed first so that e.g.
# "pixel remaster" is matched before the bare word "remaster".
_EDITION_PHRASES = [
    # Multi-word, edition-class suffixes
    "game of the year edition", "goty edition", "definitive edition",
    "enhanced edition", "deluxe edition", "ultimate edition",
    "complete edition", "collector's edition", "collectors edition",
    "anniversary edition", "director's cut", "directors cut",
    "special edition", "premium edition", "gold edition",
    "remastered edition", "standard edition", "platinum edition",
    "legendary edition", "pixel remaster", "game of the year",
    # Single-word suffixes
    "definitive", "remastered", "remaster", "redux", "hd", "goty",
    "anniversary", "enhanced", "legendary", "ultimate", "complete",
    "deluxe", "premium", "gold", "edition",
    # Markers that explicitly identify a "base/original" variant.  Stripping
    # these makes "Final Fantasy VI (Classic)" bucket with "Final Fantasy VI
    # Pixel Remaster" so the edition-preference filter can choose between them.
    "classic", "original",
]

# Used by edition-preference detection to classify each game as
# "enhanced"/remastered or base/original.  Matches the word anywhere in the
# title, not just as a suffix (e.g. "Pixel Remaster" can appear mid-string).
_ENHANCED_MARKER_RE = re.compile(
    r"\b(remastered?|remaster|definitive|complete|goty|game\s+of\s+the\s+year|"
    r"enhanced|legendary|ultimate|anniversary|director'?s\s+cut|redux|"
    r"pixel\s+remaster|hd)\b",
    re.IGNORECASE,
)

def is_enhanced_edition(title):
    """Heuristic: does this title look like an enhanced/remastered/GOTY edition?
    Used to choose between same-game variants on the same platform."""
    return bool(_ENHANCED_MARKER_RE.search(title or ""))
_TRADEMARK_CHARS = re.compile(r"[™®©℠]")  # ™ ® © ℠
_PUNCT = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")


def normalize_title(title):
    """Reduce a game title to a fuzzy-comparable canonical form.

    Examples:
      "Batman: Arkham City"                          -> "batman arkham city"
      "Batman Arkham City Game of the Year Edition" -> "batman arkham city"
      "DOOM (1993)"                                   -> "doom 1993"
      "The Witcher 3: Wild Hunt - Complete Edition"  -> "witcher 3 wild hunt"
    """
    if not title:
        return ""
    s = title.lower()
    s = _TRADEMARK_CHARS.sub("", s)
    s = s.replace("&", "and")
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()

    # Strip edition phrases — must be done after punctuation normalization
    # so "Game-of-the-Year" matches.  Repeat until stable to handle stacked
    # suffixes like "Definitive Edition - GOTY".
    changed = True
    while changed:
        changed = False
        for phrase in _EDITION_PHRASES:
            if s.endswith(" " + phrase):
                s = s[: -(len(phrase) + 1)].strip()
                changed = True
            elif s == phrase:
                s = ""
                changed = True

    # Strip leading article
    if s.startswith("the "):
        s = s[4:]
    return s


def find_cross_platform_duplicates(games_by_platform, priority):
    """For a mapping of platform -> [game dicts], return a dict mapping each
    platform to a set of game IDs that should be hidden because the same game
    exists on a higher-priority platform.

    `priority` is an ordered list like ['steam', 'gog', 'epic'] — earlier =
    more preferred.

    Algorithm:
      1. Bucket every game by its normalized title.
      2. For each bucket containing games from 2+ platforms, find the highest-
         priority platform present in the bucket.
      3. Mark every game in the bucket that's on a DIFFERENT platform as
         excluded.  (Games within the same platform but with matching titles —
         e.g. two different DOOMs on Steam — are left alone.)
    """
    excludes = {p: set() for p in games_by_platform}
    buckets = {}   # normalized title -> [(platform, game), ...]

    for platform, games in games_by_platform.items():
        for g in games:
            norm = normalize_title(g.get("name", ""))
            if not norm:
                continue
            buckets.setdefault(norm, []).append((platform, g))

    # Build a fast lookup of platform priority
    rank = {p: i for i, p in enumerate(priority)}

    for norm, entries in buckets.items():
        platforms_in_bucket = {p for p, _ in entries}
        if len(platforms_in_bucket) < 2:
            continue   # not a cross-platform duplicate
        # Pick the winning platform: lowest rank index among those present
        winner = min(platforms_in_bucket,
                     key=lambda p: rank.get(p, 99))
        for platform, game in entries:
            if platform == winner:
                continue
            gid = game.get("id")
            if gid:
                excludes[platform].add(gid)

    return {p: list(v) for p, v in excludes.items()}


def find_same_platform_edition_dupes(games, preference):
    """For a list of games (all on the same platform), return a set of game IDs
    that should be hidden because a same-game-different-edition variant exists
    and the user has expressed a preference.

    preference: 'enhanced' (hide originals) | 'original' (hide enhanced) | 'both' (hide nothing)

    Algorithm:
      1. Bucket games by normalized title.  Editions like "Mass Effect" and
         "Mass Effect Legendary Edition" land in the same bucket because
         normalize_title() strips the suffix.
      2. For each bucket with 2+ entries, classify each game as enhanced or
         original using is_enhanced_edition().
      3. Hide whichever side the preference says to hide — but only when BOTH
         sides exist in the bucket (otherwise there's no choice to make).
    """
    if preference not in ("enhanced", "original") or not games:
        return set()

    buckets = {}
    for g in games:
        norm = normalize_title(g.get("name", ""))
        if not norm:
            continue
        buckets.setdefault(norm, []).append(g)

    hidden = set()
    for norm, entries in buckets.items():
        if len(entries) < 2:
            continue
        enhanced = [g for g in entries if is_enhanced_edition(g.get("name", ""))]
        original = [g for g in entries if not is_enhanced_edition(g.get("name", ""))]
        if not enhanced or not original:
            continue   # only one side present — nothing to choose between
        losers = original if preference == "enhanced" else enhanced
        for g in losers:
            gid = g.get("id")
            if gid:
                hidden.add(gid)
    return hidden


def apply_galaxy_enrichment(games, platform=None):
    """Mutate `games` in place, decorating each entry with Galaxy-sourced
    playtime / image / tag data.  Safe to call regardless of whether Galaxy
    is installed — if it isn't, every game just gets the default-empty values.

    Bridges the OAuth/Galaxy ID mismatch for Epic via the game's app_name."""
    if not games:
        return games

    # Build a parallel list of lookup keys (one per game) so we can map back
    keys = []
    for g in games:
        keys.append(_galaxy_lookup_key(g, platform))

    # Dedupe before querying
    unique_keys = list({k for k in keys if k})
    enrichment = query_galaxy_enrichment(unique_keys)

    for g, key in zip(games, keys):
        if not key:
            continue
        data = enrichment.get(key)
        if not data:
            continue
        # Only fill in fields the game doesn't already define — avoids
        # clobbering richer per-source data (e.g. OAuth catalog metadata)
        g.setdefault("playtime_minutes", data["playtime_minutes"])
        g.setdefault("image_background", data["image_background"])
        g.setdefault("image_vertical",   data["image_vertical"])
        g.setdefault("image_icon",       data["image_icon"])
        g.setdefault("tags",             data["tags"])
    return games


def query_galaxy_db_for_platform(platform_prefix):
    """Return a sorted list of {id, raw_id, name, platform, source} dicts for
    every owned game whose releaseKey starts with ``<platform_prefix>_`` in
    GOG Galaxy's local SQLite database.  Works for GOG itself and for any
    platform the user has integrated with Galaxy (Epic, Steam, etc.).

    Returns ``[]`` if the DB file is missing, locked, or has an unexpected
    schema — the caller should fall back to its registry/manifest method."""
    if not os.path.isfile(GOG_GALAXY_DB):
        return []

    games = {}   # release_key -> game dict; second pass prefers 'title' rows
    try:
        # Read-only URI so we don't fight Galaxy for the write lock.
        conn = sqlite3.connect(
            f"file:{GOG_GALAXY_DB}?mode=ro", uri=True, timeout=2.0,
        )
        cur = conn.cursor()
        cur.execute(
            """
            SELECT lr.releaseKey, gp.value, gpt.type
              FROM LibraryReleases lr
              LEFT JOIN GamePieces      gp  ON gp.releaseKey      = lr.releaseKey
              LEFT JOIN GamePieceTypes  gpt ON gpt.id             = gp.gamePieceTypeId
             WHERE lr.releaseKey LIKE ?
               AND gpt.type IN ('title', 'originalTitle')
            """,
            (f"{platform_prefix}_%",),
        )
        rows = cur.fetchall()
        conn.close()
    except sqlite3.Error:
        return []

    for release_key, value, type_name in rows:
        if not value:
            continue
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        name = parsed.get("title") if isinstance(parsed, dict) else None
        if not name:
            continue

        # Prefer 'title' over 'originalTitle' when both exist for the same key
        existing = games.get(release_key)
        if existing is not None and type_name != "title":
            continue

        raw_id = release_key.split("_", 1)[1] if "_" in release_key else release_key
        games[release_key] = {
            "id":       release_key,
            "raw_id":   raw_id,
            "name":     name,
            "platform": platform_prefix,
            "source":   "galaxy",
        }

    return sorted(games.values(), key=lambda g: g["name"].lower())


# ── js_api class ──────────────────────────────────────────────────────────────

class SteamRouletteAPI:
    """Methods callable from JavaScript via window.pywebview.api.*"""

    def __init__(self):
        self._collections_path = None
        self._collections = {}
        self._shortcuts   = {}   # {appid: {'name': str, 'tags': [str,...]}}
        self._playtimes   = {}   # {appid: minutes_played} from localconfig.vdf
        self._steam_path  = find_steam_path()
        self._window      = None

    def set_window(self, window):
        self._window = window

    # ── Collection loading ────────────────────────────────────────────────

    def auto_load(self):
        """
        Auto-detect and load collections.
        Returns {"status": "ok", "collections": [...], "path": "..."}
             or {"status": "notfound", "message": "..."}
        """
        cfg = load_config()
        saved = cfg.get("collections_path")
        if saved and os.path.isfile(saved):
            return self._load_from_path(saved)

        if not self._steam_path:
            return {"status": "notfound", "message": "Steam installation not found."}

        accounts = find_collections_files(self._steam_path)

        if not accounts:
            return {"status": "notfound", "message": "No collections file found under Steam userdata."}

        if len(accounts) == 1:
            return self._load_from_path(accounts[0][1])

        # Multiple accounts: pick the one with the most collections.
        # Ties broken by lower (older) account ID.
        def score(item):
            _, path = item
            try:
                count = len(parse_collections(path))
            except Exception:
                count = 0
            acct_id = item[0]
            lower_is_better = -int(acct_id) if acct_id.isdigit() else 0
            return (count, lower_is_better)

        best = max(accounts, key=score)
        return self._load_from_path(best[1])

    def select_account(self, path):
        """User manually picked an account path."""
        return self._load_from_path(path)

    def reload_collections(self):
        """Re-parse the cached collections file.  Use this after adding games or
        editing collections in Steam — the JSON is rewritten by Steam Cloud
        Sync but the app only parses it on startup."""
        if not self._collections_path or not os.path.isfile(self._collections_path):
            return {"status": "error", "message": "No collections file loaded yet."}
        return self._load_from_path(self._collections_path)

    def debug_shortcuts(self):
        """Inspect shortcuts.vdf: does it exist, how many shortcuts, what tags?"""
        if not self._collections_path:
            return {"status": "error", "message": "No collections loaded yet."}

        config_dir = os.path.dirname(os.path.dirname(self._collections_path))
        vdf_path   = os.path.join(config_dir, "shortcuts.vdf")
        result = {
            "status":           "ok",
            "vdf_path":         vdf_path,
            "vdf_exists":       os.path.isfile(vdf_path),
            "collection_names": sorted(self._collections.keys()),
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
        coll_lower = {n.lower() for n in self._collections.keys()}
        result["unique_tags"]               = sorted(all_tags)
        result["tags_matching_collections"] = sorted(t for t in all_tags if t.strip().lower() in coll_lower)
        result["tags_not_matching"]         = sorted(t for t in all_tags if t.strip().lower() not in coll_lower)
        return result

    def debug_all_keys(self):
        """Comprehensive debug: prefix breakdown + searches for where Steam
        actually stores shortcut→collection memberships."""
        if not self._collections_path or not os.path.isfile(self._collections_path):
            return {"status": "error", "message": "No collections file loaded."}
        try:
            with open(self._collections_path, "r", encoding="utf-8") as f:
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
        shortcut_id_strs = set()
        for a in self._shortcuts.keys():
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
        folder = os.path.dirname(self._collections_path)
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
        parts = os.path.normpath(self._collections_path).split(os.sep)
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
            collection_names = list(self._collections.keys())
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
        if not self._collections_path or not os.path.isfile(self._collections_path):
            return {"status": "error", "message": "No collections file loaded."}
        try:
            with open(self._collections_path, "r", encoding="utf-8") as f:
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
        return self._load_from_path(path)

    def get_collections(self):
        return self._collections_as_list()

    # ── Game name resolution ──────────────────────────────────────────────

    def get_game_name(self, appid_str):
        """
        Resolve a game's name. Resolution order:
          1. Name cache (instant)
          2. Local appmanifest .acf file (instant, no network)
          3. Steam store API (network, ~1-2 s)
        Result is cached to disk after any successful fetch.
        """
        try:
            appid = int(appid_str)
        except (ValueError, TypeError):
            return {"status": "error", "name": f"App {appid_str}"}

        key   = str(appid)
        cache = load_name_cache()

        if key in cache:
            return {"status": "ok", "name": cache[key], "source": "cache",
                    "playtime_minutes": self._playtimes.get(appid, 0)}

        # Non-Steam shortcut?  Its real name lives in shortcuts.vdf — Steam's
        # store API would have no idea what to do with the synthetic appid.
        sc = self._shortcuts.get(appid)
        if sc and sc.get("name"):
            cache[key] = sc["name"]
            save_name_cache(cache)
            return {"status": "ok", "name": sc["name"], "source": "shortcut",
                    "playtime_minutes": self._playtimes.get(appid, 0)}

        # Try local .acf first (free, instant)
        if self._steam_path:
            name = lookup_acf_name(self._steam_path, appid)
            if name:
                cache[key] = name
                save_name_cache(cache)
                return {"status": "ok", "name": name, "source": "acf",
                        "playtime_minutes": self._playtimes.get(appid, 0)}

        # Try Steam store API, then SteamSpy for old/delisted games
        name = fetch_name_from_api(appid) or fetch_name_from_steamspy(appid)
        if not name:
            name = f"App {appid}"

        cache[key] = name
        save_name_cache(cache)
        return {"status": "ok", "name": name, "source": "api",
                "playtime_minutes": self._playtimes.get(appid, 0)}

    # ── HowLongToBeat completion times ───────────────────────────────────

    # Symbols Steam appends that confuse HLTB's similarity scorer
    _TRADEMARK_RE = re.compile(r'[™®©℠]')
    # Common subtitle separators — everything after these is stripped for the
    # fallback search term (e.g. "Game Name: Subtitle" → "Game Name")
    _SUBTITLE_RE  = re.compile(r'\s+[-–—:]\s+|\s+\(')

    def get_hltb_data(self, appid_str, game_name):
        """
        Fetch HowLongToBeat completion times for a game.
        Requires: pip install howlongtobeatpy
        Returns main_story, main_extra, completionist hours (floats).
        Successful results are cached to cache/hltb.json.
        Failures are NOT cached so that transient errors (network, stale API
        key, name mismatch) are automatically retried next spin.
        """
        # Accept any non-empty string as the cache key — works for both Steam
        # appids (plain numeric strings) and GOG / Epic ids like "gog_1207659146"
        # or "epic_Hades" so that all three platforms share the same cache file.
        key = str(appid_str).strip()
        if not key:
            return {"status": "error", "message": "Invalid id"}

        game_name = (game_name or "").strip()
        if not game_name or game_name.startswith("App "):
            return {"status": "not_found"}

        # Strip ™ ® © etc. that trip up HLTB's string similarity
        clean_name = self._TRADEMARK_RE.sub('', game_name)
        clean_name = ' '.join(clean_name.split())   # normalise whitespace

        # Load cache — only positive (dict) hits are stored; None entries are
        # legacy stale failures that should be retried, so we ignore them.
        cache = {}
        try:
            if os.path.isfile(HLTB_CACHE):
                with open(HLTB_CACHE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
        except Exception:
            pass

        hit = cache.get(key)
        if isinstance(hit, dict):           # valid cached result
            return {"status": "ok", **hit}
        # (None / missing → fall through and search)

        # Build search terms: full clean name first, then bare title as fallback
        search_terms = [clean_name]
        short = self._SUBTITLE_RE.split(clean_name)[0].strip()
        if short and short != clean_name:
            search_terms.append(short)

        try:
            from howlongtobeatpy import HowLongToBeat
            hltb = HowLongToBeat()
        except ImportError:
            return {"status": "unavailable"}

        def _best_result(term, threshold):
            try:
                results = hltb.search(term)
            except Exception:
                return None
            if not results:
                return None
            candidate = max(results, key=lambda r: r.similarity)
            return candidate if candidate.similarity >= threshold else None

        # Try full name at 0.55, then shorter title at 0.50
        best = _best_result(search_terms[0], 0.55)
        if best is None and len(search_terms) > 1:
            best = _best_result(search_terms[1], 0.50)

        if best is None:
            return {"status": "not_found"}   # not cached — will retry next time

        def safe_hours(v):
            if v is None or (isinstance(v, (int, float)) and v <= 0):
                return None
            return float(v)

        data = {
            "matched_name": best.game_name,
            "main_story":    safe_hours(best.main_story),
            "main_extra":    safe_hours(best.main_extra),
            "completionist": safe_hours(best.completionist),
        }
        # Only cache successes — failures stay uncached for automatic retry
        cache[key] = data
        self._write_hltb_cache(cache)
        return {"status": "ok", **data}

    def _write_hltb_cache(self, cache):
        try:
            with open(HLTB_CACHE, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2)
        except Exception:
            pass

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

        tag_to_keys = {}
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT releaseKey, tag FROM UserReleaseTags "
                "WHERE releaseKey LIKE ? AND tag IS NOT NULL",
                (f"{platform}_%",),
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

    # ── Cross-platform duplicate filtering ────────────────────────────────

    def get_dedup_settings(self):
        """Return the user's cross-platform duplicate hide settings."""
        # Default priority follows the registry order — Steam first, then GOG,
        # Epic, Battle.net.  Users can reorder in Settings.
        default_priority = sorted(
            PLATFORMS.keys(),
            key=lambda p: PLATFORMS[p]["default_priority"],
        )
        stored = get_setting("platform_priority", None)
        if not isinstance(stored, list):
            stored = default_priority
        else:
            # Top up with any newly-supported launchers the user's saved
            # priority predates (e.g. they had a priority saved before we
            # added Battle.net) — append missing ones at the end.
            for p in default_priority:
                if p not in stored:
                    stored.append(p)
        return {
            "status":   "ok",
            "enabled":  bool(get_setting("hide_duplicates", False)),
            "priority": [p for p in stored if p in PLATFORMS],
        }

    def set_dedup_settings(self, enabled, priority):
        """Persist the dedup toggle and priority order."""
        if not isinstance(priority, list):
            return {"status": "error", "message": "priority must be a list"}
        # Sanitise: keep only known platforms, dedupe, and ensure every
        # supported platform appears at least once.
        seen = []
        for p in priority:
            if p in PLATFORMS and p not in seen:
                seen.append(p)
        for p in sorted(PLATFORMS.keys(),
                        key=lambda x: PLATFORMS[x]["default_priority"]):
            if p not in seen:
                seen.append(p)
        set_setting("hide_duplicates", bool(enabled))
        set_setting("platform_priority", seen)
        return self.get_dedup_settings()

    # ── Edition preference (newer/enhanced vs original) ──────────────────

    def get_edition_preference(self):
        """Return the user's preference for same-game edition variants:
            'both'     — show everything (default)
            'enhanced' — hide originals when an enhanced edition exists
            'original' — hide enhanced editions when an original exists
        """
        pref = get_setting("edition_preference", "both")
        if pref not in ("both", "enhanced", "original"):
            pref = "both"
        return {"status": "ok", "preference": pref}

    def set_edition_preference(self, preference):
        if preference not in ("both", "enhanced", "original"):
            return {"status": "error",
                    "message": "preference must be 'both', 'enhanced', or 'original'"}
        set_setting("edition_preference", preference)
        return {"status": "ok", "preference": preference}

    def _apply_edition_preference(self, games):
        """Strip same-game duplicates within the given list per the user's
        edition_preference setting.  Safe to call on any platform's games."""
        pref = get_setting("edition_preference", "both")
        if pref not in ("enhanced", "original") or not games:
            return games
        hidden = find_same_platform_edition_dupes(games, pref)
        if not hidden:
            return games
        return [g for g in games if g.get("id") not in hidden]

    def get_edition_filter(self):
        """Same shape as get_duplicate_filter but for same-platform edition
        variants (e.g. Mass Effect vs Mass Effect Legendary Edition).  Returns
        per-platform game IDs to hide based on the edition_preference setting.

        Disabled (preference='both') returns all empty lists."""
        pref = get_setting("edition_preference", "both")
        empty = {"status": "ok", "preference": pref}
        for pid in PLATFORMS:
            empty[pid] = []
        empty["counts"] = {f"{pid}_hidden": 0 for pid in PLATFORMS}
        if pref not in ("enhanced", "original"):
            return empty

        # Steam — synthesise game dicts using cached names (lazy + bulk)
        steam_appids = set()
        for ids in (self._collections or {}).values():
            steam_appids.update(ids)
        name_cache = load_name_cache()
        bulk_names = steam_names.get_steam_app_names(CACHE_DIR)
        def _name_for(a):
            return name_cache.get(str(a)) or bulk_names.get(str(a)) or ""
        steam_games = [
            {"id": f"steam_{a}", "raw_id": a, "platform": "steam",
             "name": _name_for(a)}
            for a in steam_appids
            if _name_for(a)
        ]

        per_platform_games = {
            "steam": steam_games,
            "gog":   self.get_gog_games().get("games", []),
            "epic":  self.get_epic_games().get("games", []),
        }

        out = {"status": "ok", "preference": pref}
        counts = {}
        for pid, games in per_platform_games.items():
            hidden = find_same_platform_edition_dupes(games, pref)
            out[pid] = list(hidden)
            counts[f"{pid}_hidden"] = len(hidden)
        out["counts"] = counts
        return out

    def detect_platforms(self):
        """Report which of the supported launchers we found on this PC.

        Used by the frontend to decide which tab to auto-select on a fresh
        install and to show a friendly empty state when nothing is detected.
        Cheap to call — only stats file paths and registry."""
        steam_path = self._steam_path or find_steam_path()
        galaxy_ok  = os.path.isfile(GOG_GALAXY_DB)
        epic_dir   = find_epic_manifests_dir()
        epic_oauth = epic_auth.load_tokens(CACHE_DIR) is not None
        return {
            "status": "ok",
            "steam":              bool(steam_path),
            "gog":                galaxy_ok,
            "epic":               bool(epic_dir) or epic_oauth,
            "any":                bool(steam_path or galaxy_ok or epic_dir or epic_oauth),
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

    def get_sound_enabled(self):
        """Whether reel tick / landing sounds are enabled."""
        return {"status": "ok", "enabled": bool(get_setting("sound_enabled", True))}

    def set_sound_enabled(self, enabled):
        """Toggle reel tick / landing sounds."""
        set_setting("sound_enabled", bool(enabled))
        return {"status": "ok", "enabled": bool(enabled)}

    def get_steam_names_status(self):
        """Return whether the bulk SteamSpy name cache exists and how big it is.
        The frontend uses this to warn users that the first dedup computation
        will take ~30 seconds (the SteamSpy pages fetch sequentially)."""
        age = steam_names.cache_age_seconds(CACHE_DIR)
        bulk = steam_names.load_cache(CACHE_DIR)
        return {
            "status": "ok",
            "cached": len(bulk) > 0,
            "count":  len(bulk),
            "age_seconds": age,
            "ttl_seconds": steam_names.CACHE_TTL_SEC,
        }

    def refresh_steam_names(self):
        """Force-refresh the bulk Steam name cache (blocking, ~30 sec)."""
        names = steam_names.get_steam_app_names(CACHE_DIR, force_refresh=True)
        return {"status": "ok", "count": len(names)}

    def get_duplicate_filter(self):
        """Load every owned game across Steam / GOG / Epic, compute which IDs
        should be hidden based on the priority order, and return:

            { steam: [excluded_ids], gog: [excluded_ids], epic: [excluded_ids],
              counts: { steam_hidden: N, gog_hidden: N, epic_hidden: N } }

        Frontend caches this and applies the exclude-lists locally to its game
        lists.  Returns empty lists if dedup is disabled."""
        settings = self.get_dedup_settings()
        empty = {pid: [] for pid in PLATFORMS}
        empty_counts = {f"{pid}_hidden": 0 for pid in PLATFORMS}
        if not settings["enabled"]:
            return {"status": "ok", **empty, "counts": empty_counts}

        priority = settings["priority"]

        # Steam: synthesise game dicts from the cached + bulk name pool
        steam_appids = set()
        for ids in (self._collections or {}).values():
            steam_appids.update(ids)
        name_cache = load_name_cache()
        bulk_names = steam_names.get_steam_app_names(CACHE_DIR)
        def _name_for(appid):
            return (name_cache.get(str(appid))
                    or bulk_names.get(str(appid))
                    or "")
        steam_games = [
            {"id": f"steam_{a}", "raw_id": a, "platform": "steam",
             "name": _name_for(a)}
            for a in steam_appids
            if _name_for(a)
        ]

        excludes = find_cross_platform_duplicates({
            "steam": steam_games,
            "gog":   self.get_gog_games().get("games", []),
            "epic":  self.get_epic_games().get("games", []),
        }, priority)

        out = {"status": "ok"}
        counts = {}
        for pid in PLATFORMS:
            out[pid] = excludes.get(pid, [])
            counts[f"{pid}_hidden"] = len(out[pid])
        out["counts"] = counts
        return out

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

    # ── Internal ──────────────────────────────────────────────────────────

    def _load_from_path(self, path):
        try:
            self._collections = parse_collections(path)

            # Non-Steam shortcuts from shortcuts.vdf
            self._shortcuts = {}
            vdf = find_shortcuts_vdf_for(path)
            if vdf:
                shortcuts = parse_shortcuts_vdf(vdf)
                self._shortcuts = {sc["appid"]: sc for sc in shortcuts if sc.get("appid")}
                merge_shortcuts_into_collections(self._collections, shortcuts)

            cfg = load_config()

            # In-app shortcut→collection assignments
            for appid_str, collection_names in cfg.get("shortcut_assignments", {}).items():
                try:
                    appid = int(appid_str)
                except (ValueError, TypeError):
                    continue
                if appid not in self._shortcuts:
                    continue
                for cname in collection_names:
                    if cname in self._collections and appid not in self._collections[cname]:
                        self._collections[cname].append(appid)

            # Remove user-excluded games from every collection
            excluded = set(cfg.get("excluded_appids", []))
            if excluded:
                for name in list(self._collections.keys()):
                    self._collections[name] = [a for a in self._collections[name] if a not in excluded]

            # Playtimes from localconfig.vdf (same account as collections file)
            self._playtimes = {}
            lc_path = os.path.join(os.path.dirname(os.path.dirname(path)), "localconfig.vdf")
            if os.path.isfile(lc_path):
                self._playtimes = parse_playtimes_from_localconfig(lc_path)

            self._collections_path = path
            cfg["collections_path"] = path
            save_config(cfg)
            return {
                "status":             "ok",
                "collections":        self._collections_as_list(),
                "shortcut_appids":    [a for a in self._shortcuts.keys() if a not in excluded],
                "hidden_collections": list(cfg.get("hidden_collections", [])),
                "path":               path,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # Collections to hide from the UI entirely (not games).
    _EXCLUDED = frozenset({"Software", "Server"})

    def _collections_as_list(self):
        hidden = set(load_config().get("hidden_collections", []))
        return [
            {"name": name, "count": len(ids), "appids": ids}
            for name, ids in self._collections.items()
            if name not in self._EXCLUDED and name not in hidden
        ]
