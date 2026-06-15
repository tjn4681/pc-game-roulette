"""
Steam on-disk library parsing.

Everything that reads Steam's own files: locating the install, parsing custom
Collections (cloud-storage-namespace-1.json), non-Steam shortcuts
(shortcuts.vdf), installed-game manifests (appmanifest_*.acf across all library
folders), and playtimes (localconfig.vdf).

No network, no app config — just Steam's filesystem layout.  See the project
CLAUDE.md for the hard-won facts about where Collections actually live.
"""

import json
import os
import re
import winreg


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
