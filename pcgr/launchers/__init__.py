"""Game launchers behind the common ``Launcher`` interface (base.py):
Steam, GOG, Epic, RetroArch.  Each is self-contained and composed by the
js_api facade."""

from pcgr.launchers.base import Launcher
from pcgr.launchers.steam import SteamLauncher
from pcgr.launchers.gog import GogLauncher
from pcgr.launchers.epic import EpicLauncher
from pcgr.launchers.retroarch import RetroArchLauncher

__all__ = [
    "Launcher", "SteamLauncher", "GogLauncher", "EpicLauncher", "RetroArchLauncher",
]
