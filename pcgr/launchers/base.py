"""
The common launcher contract.

Steam, GOG, Epic and RetroArch each wrap a very different backend (Steam's
Collections files, the GOG Galaxy SQLite DB, Epic's OAuth API, RetroArch's
playlists) but the app only needs a handful of operations from each.  Expressing
that as one `Launcher` interface means the facade can treat them uniformly —
detect them, gather their games, build their category cards, launch a game — and
adding a new launcher is a matter of implementing this contract and registering
it, rather than touching code in a dozen places.

Concrete launchers also expose launcher-specific extras (Steam's API key, Epic's
OAuth flow, RetroArch's art server); those are called directly off the concrete
instance by the facade and aren't part of the shared contract.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod

from pcgr.config import load_config


class Launcher(ABC):
    """A game launcher the app can detect, enumerate, and launch from.

    Implementations carry their own state and never reach back into the js_api
    facade — the facade composes them, not the other way around.

    Game dicts returned by ``get_games`` follow the shape the frontend already
    expects:  ``{"id", "raw_id", "name", "platform", "source", ...}``.
    Category dicts from ``get_categories`` use the collection-card shape:
    ``{"name", "count", "appids"}``.
    """

    #: stable launcher key used in ids, settings, and the frontend tabs.
    id: str = ""
    #: human-readable launcher name.
    name: str = ""

    @abstractmethod
    def is_present(self) -> bool:
        """Whether this launcher is installed / usable on this PC.  Cheap —
        called on every ``detect_platforms`` and should only stat paths /
        registry, never hit the network."""

    @abstractmethod
    def get_games(self) -> dict:
        """Return ``{"status": "ok", "games": [...], ...}`` — the owned library
        for this launcher."""

    @abstractmethod
    def get_categories(self) -> dict:
        """Return ``{"status": "ok", "collections": [...]}`` — the launcher's
        groupings (Steam Collections, Galaxy tags, RetroArch playlists) in the
        collection-card shape the grid renders."""

    @abstractmethod
    def launch(self, game_id: str, **opts) -> dict:
        """Launch a game by its raw id.  Returns ``{"status": "ok"}`` or
        ``{"status": "error", "message": ...}``."""

    def connection_status(self) -> dict:
        """Lightweight ``{connected, name, count, source}`` for the Settings UI.
        Defaults to just the presence flag; launchers override to add counts and
        the signed-in account name."""
        return {"connected": self.is_present()}


def launch_uri(uri: str) -> dict:
    """Open a protocol URI through the Windows shell.

    Both GOG Galaxy and the Epic Launcher register URL-protocol handlers that
    resolve via ShellExecute.  ``cmd /c start`` is the most reliable
    cross-context way to fire a URI on Windows — it works even when the caller
    is a non-foreground thread, which is exactly what pywebview's js_api
    callbacks are.  The URI is echoed back so the frontend can surface it in a
    toast for debugging."""
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", uri],
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return {"status": "ok", "uri": uri}
    except Exception as e:
        return {"status": "error", "message": str(e), "uri": uri}


def filter_platform_excluded(games):
    """Strip games the user has manually excluded (the per-platform exclusion
    list keyed by full game id, e.g. ``gog_123`` / ``epic_<id>``).  Shared by
    the GOG and Epic launchers."""
    if not games:
        return games
    cfg = load_config()
    excluded = set((cfg.get("excluded_platform_games") or {}).keys())
    if not excluded:
        return games
    return [g for g in games if g.get("id") not in excluded]
