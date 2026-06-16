"""
PC Game Roulette backend package.

Detects the user's PC game libraries (Steam, GOG, Epic, RetroArch), parses
their collections / playlists / tags, resolves names and art, and computes
duplicate filters — all behind a single js_api facade (SteamRouletteAPI) that
the pywebview frontend calls.

Public entry points (imported by main.py):
  * SteamRouletteAPI — the js_api object
  * CACHE_DIR        — where the persistent WebView2 profile and caches live
"""

from pcgr.config import CACHE_DIR
from pcgr.api import SteamRouletteAPI

__all__ = ["SteamRouletteAPI", "CACHE_DIR"]
