"""
Steam Roulette backend — Steam detection, collections parsing, name fetching, config.
Exposed to the frontend via js_api in main.py.
"""

import base64
import json
import os
import re
import sys
import urllib.request
import winreg


# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE     = os.path.join(SCRIPT_DIR, "config.json")
CACHE_DIR       = os.path.join(SCRIPT_DIR, "cache")
NAMES_CACHE     = os.path.join(CACHE_DIR,  "names.json")
ART_CACHE_DIR   = os.path.join(CACHE_DIR,  "art")

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
            save_filename="steam-roulette-debug.txt",
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
        return {
            "status":             "ok",
            "excluded_games":     excluded,
            "hidden_collections": hidden,
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
