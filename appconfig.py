"""
Filesystem paths and config persistence for PC Game Roulette.

Single source of truth for *where* writable data lives (config.json, the
caches, the WebView2 profile, Epic tokens) plus the helpers that read and write
config.json.  Every other module imports these constants rather than
recomputing paths, so there's exactly one place that knows the on-disk layout.
"""

import json
import os
import sys


def _data_dir():
    """Directory for writable runtime data (config, cache, Epic tokens, the
    WebView2 profile).

    Development → the source tree.

    Frozen (the installed app) → per-user %LOCALAPPDATA%\\PC Game Roulette.
    This is robust no matter where the app is installed (Program Files is
    read-only for standard users) and survives updates.  We deliberately do NOT
    use the onefile temp-extraction dir (sys._MEIPASS), which is wiped every
    launch.

    Portable escape hatch: drop an empty 'portable.flag' file next to the .exe
    and data is stored beside it instead (e.g. for a USB-stick copy)."""
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(__file__))
    exe_dir = os.path.dirname(sys.executable)
    if os.path.isfile(os.path.join(exe_dir, "portable.flag")):
        return exe_dir
    base = os.environ.get("LOCALAPPDATA") or exe_dir
    return os.path.join(base, "PC Game Roulette")


SCRIPT_DIR      = _data_dir()
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

# Make sure the writable cache directories exist before anything reads/writes
# them.  Done at import so every consumer can assume they're present.
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(ART_CACHE_DIR, exist_ok=True)


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
    """Persist config.json atomically (temp file + replace) so a crash or
    concurrent writer can't leave a half-written, unparseable config."""
    tmp = CONFIG_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except OSError:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


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
