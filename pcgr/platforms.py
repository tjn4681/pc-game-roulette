"""
Launcher registry and launcher-discovery glue.

The platform metadata table plus the few constants and the Epic-manifest
locator that several mixins need.  Lives in its own module (rather than in
backend.py) so the js_api mixins can import it without a circular dependency
back onto the class that combines them.
"""

import os
import winreg

from pcgr.config import _PROGRAM_DATA


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
