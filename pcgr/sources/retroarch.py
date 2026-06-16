"""
RetroArch integration for PC Game Roulette.

RetroArch is an emulator frontend, not a store — its "library" is a set of
playlists (one per console/system), each a JSON ``.lpl`` file listing ROM
entries.  This module discovers the install, parses the playlists, resolves
emulator cores and local box-art thumbnails, and builds launch commands.

Nothing here touches the network: thumbnails come straight off disk.

Key facts about RetroArch's on-disk layout (don't rediscover these):
  • Playlists live in  <install>/playlists/*.lpl  (JSON, "version" + "items").
  • Each item has: path (ROM), label, core_path, core_name, crc32, db_name.
  • core_path is frequently the literal string "DETECT" — in that case fall
    back to the playlist's top-level "default_core_path".
  • retroarch.cfg uses ":" as shorthand for the install directory, e.g.
    playlist_directory = ":\\playlists".
  • Thumbnails:  <install>/thumbnails/<System>/Named_Boxarts/<label>.png
    where <label> is the item label with a fixed set of characters replaced
    by "_" (see _sanitize_thumb_name).  Also Named_Snaps / Named_Titles.
  • Core/ROM paths inside playlists can be stale if the install was moved
    between drives — we remap any *_directory and core paths to the real
    install dir we actually found.
"""

import json
import os
import re
import zlib

# Characters RetroArch strips from a label to form a thumbnail filename.
# Mirrors libretro's gfx_thumbnail filename rule: & * / : ` < > ? \ | "
_THUMB_BAD_CHARS = re.compile(r'[&*/:`<>?\\|"]')

# Common install locations to probe when we have no saved path.  We also scan
# every drive letter for RetroArch-Win64 / RetroArch since portable installs
# (like a dedicated ROM SSD) land on non-system drives.
_COMMON_SUBDIRS = ("RetroArch-Win64", "RetroArch")


def _candidate_dirs():
    """Yield plausible RetroArch install directories, cheapest first."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        yield os.path.join(appdata, "RetroArch")
    # Steam install of RetroArch (appid 1118310)
    for pf in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if pf:
            yield os.path.join(pf, "Steam", "steamapps", "common", "RetroArch")
    # Every drive letter, common portable folder names
    for drive in "DCEFGHIJKLMNOPQRSTUVWXYZAB":
        root = f"{drive}:\\"
        if not os.path.isdir(root):
            continue
        for sub in _COMMON_SUBDIRS:
            yield os.path.join(root, sub)


def find_retroarch_dir(saved_path=None):
    """Return the RetroArch install directory, or None if not found.

    A directory qualifies only if it contains retroarch.exe.  Honours a
    previously-saved path first so detection is skipped on later launches.
    """
    if saved_path and os.path.isfile(os.path.join(saved_path, "retroarch.exe")):
        return saved_path
    for d in _candidate_dirs():
        try:
            if os.path.isfile(os.path.join(d, "retroarch.exe")):
                return d
        except OSError:
            continue
    return None


def _resolve_ra_path(install_dir, value):
    """Resolve a retroarch.cfg path value against the install dir.

    Handles RetroArch's ":" install-dir shorthand and relative paths, and
    leaves absolute paths alone.
    """
    if not value:
        return None
    value = value.strip().strip('"')
    if not value:
        return None
    # ":" or ":\foo" -> install_dir [ + \foo ]
    if value == ":" or value.startswith(":\\") or value.startswith(":/"):
        rest = value[1:].lstrip("\\/")
        return os.path.normpath(os.path.join(install_dir, rest)) if rest else install_dir
    # Already absolute (has a drive letter or UNC)
    if os.path.isabs(value) or (len(value) >= 2 and value[1] == ":"):
        return os.path.normpath(value)
    # Otherwise treat as relative to the install dir
    return os.path.normpath(os.path.join(install_dir, value))


_CFG_LINE_RE = re.compile(r'^\s*([A-Za-z0-9_]+)\s*=\s*"?(.*?)"?\s*$')


def parse_config(install_dir):
    """Parse retroarch.cfg and return resolved key directories.

    Falls back to the conventional <install>/<name> layout for any directory
    the config doesn't specify.  Always returns a usable dict.
    """
    cfg = {}
    cfg_path = os.path.join(install_dir, "retroarch.cfg")
    wanted = {
        "playlist_directory", "thumbnails_directory",
        "libretro_directory", "savestate_directory",
    }
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _CFG_LINE_RE.match(line)
                if m and m.group(1) in wanted:
                    cfg[m.group(1)] = m.group(2)
    except OSError:
        pass

    def resolved(key, default_subdir):
        return (_resolve_ra_path(install_dir, cfg.get(key))
                or os.path.join(install_dir, default_subdir))

    return {
        "install_dir":  install_dir,
        "exe":          os.path.join(install_dir, "retroarch.exe"),
        "playlists":    resolved("playlist_directory",   "playlists"),
        "thumbnails":   resolved("thumbnails_directory",  "thumbnails"),
        "cores":        resolved("libretro_directory",    "cores"),
    }


def _sanitize_thumb_name(label):
    """Convert a playlist label into RetroArch's thumbnail filename stem."""
    return _THUMB_BAD_CHARS.sub("_", label or "")


def _remap_core(core_path, default_core_path, cores_dir):
    """Pick a usable core .dll for an item.

    Order: the item's own core_path (if a real existing file) → the playlist
    default → either of those remapped into the actual cores dir by basename
    (handles installs moved between drives, where stored paths are stale).
    """
    for cand in (core_path, default_core_path):
        if not cand or cand == "DETECT":
            continue
        if os.path.isfile(cand):
            return cand
        # Stale path (e.g. C:\... after moving to D:\) — try the real cores dir
        remapped = os.path.join(cores_dir, os.path.basename(cand))
        if os.path.isfile(remapped):
            return remapped
    return ""   # let RetroArch auto-detect at launch


def _game_id(rom_path):
    """Stable unique id for a ROM entry (survives restarts; used by the UI
    and the per-platform exclusion list)."""
    crc = zlib.crc32((rom_path or "").encode("utf-8", "replace")) & 0xFFFFFFFF
    return f"retroarch_{crc:08x}"


def _system_from_filename(lpl_filename):
    """'Nintendo - Game Boy.lpl' -> 'Nintendo - Game Boy'."""
    base = os.path.basename(lpl_filename)
    return base[:-4] if base.lower().endswith(".lpl") else base


def parse_playlist_file(lpl_path, cfg):
    """Parse one .lpl file into a list of game dicts.

    Each game: {id, raw_id, name, platform, system, rom_path, core_path,
                core_name, thumb_path}.  thumb_path is the resolved local
    boxart PNG if it exists, else None.
    """
    try:
        with open(lpl_path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(data, dict):
        return []
    items = data.get("items") or []
    default_core = data.get("default_core_path") or ""
    system = _system_from_filename(lpl_path)
    box_dir = os.path.join(cfg["thumbnails"], system, "Named_Boxarts")

    games = []
    for it in items:
        if not isinstance(it, dict):
            continue
        rom_path = it.get("path") or ""
        label    = it.get("label") or os.path.splitext(os.path.basename(rom_path))[0]
        if not rom_path or not label:
            continue
        core = _remap_core(it.get("core_path", ""), default_core, cfg["cores"])
        thumb = os.path.join(box_dir, _sanitize_thumb_name(label) + ".png")
        if not os.path.isfile(thumb):
            thumb = None
        games.append({
            "id":        _game_id(rom_path),
            "raw_id":    rom_path,
            "name":      label,
            "platform":  "retroarch",
            "system":    system,
            "rom_path":  rom_path,
            "core_path": core,
            "core_name": it.get("core_name") or "",
            "thumb_path": thumb,
        })
    return games


def load_playlists(install_dir):
    """Parse every .lpl in the install and return per-system playlists.

    Returns a list of {name, system, count, games:[...]} sorted by system
    name, skipping empty playlists.  Returns [] if nothing is found.
    """
    cfg = parse_config(install_dir)
    pl_dir = cfg["playlists"]
    if not os.path.isdir(pl_dir):
        return []
    out = []
    try:
        names = sorted(fn for fn in os.listdir(pl_dir) if fn.lower().endswith(".lpl"))
    except OSError:
        return []
    for fn in names:
        # Skip RetroArch's auto-generated history/favorites bookkeeping lists?
        # Favorites is genuinely useful, keep it; only skip clearly-internal ones.
        games = parse_playlist_file(os.path.join(pl_dir, fn), cfg)
        if games:
            system = _system_from_filename(fn)
            out.append({"name": system, "system": system,
                        "count": len(games), "games": games})
    return out
