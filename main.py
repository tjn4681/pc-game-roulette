"""
PC Game Roulette — entry point.
Launches the pywebview window and wires up the Python js_api.
"""

import ctypes
import os
import sys
import webview
from pcgr import CACHE_DIR, SteamRouletteAPI


def _resource_dir():
    """Read-only bundled assets (web/, app.ico).  PyInstaller unpacks these to
    sys._MEIPASS at runtime; in development it's the source tree."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


WEB_DIR    = os.path.join(_resource_dir(), "web")
ICON_PATH  = os.path.join(_resource_dir(), "app.ico")
# Persistent WebView2 profile.  By default pywebview runs in private_mode,
# which spins up a throw-away profile in a random %TEMP% folder every launch —
# so WebView2's HTTP cache is discarded and every game header image is
# re-downloaded from the Steam CDN on each run (and %TEMP% slowly fills with
# leftover EBWebView folders).  Pinning a persistent profile here lets the
# browser cache survive between launches: card art loads from disk on repeat
# runs instead of the network.  Shares backend's data dir (per-user
# %LOCALAPPDATA% when installed, or the source tree in dev) so there's a single
# source of truth for where writable data lives.
#
# We don't need to manually evict the cache when the frontend changes: pywebview
# serves the app's own HTML/JS/CSS through its bundled HTTP server with
# 'Cache-Control: no-store', so WebView2 always re-fetches the shell fresh.
# Only the remote CDN game art (which sends its own cacheable headers) persists
# across launches — exactly the split we want.
PROFILE_DIR = os.path.join(CACHE_DIR, "webview")

# Unique AppUserModelID so Windows groups our window under the custom icon in
# the taskbar instead of the generic python.exe icon.
APP_USER_MODEL_ID = "PCGameRoulette.App.1"


def _set_taskbar_icon():
    """Tell Windows to use our custom icon for this process in the taskbar.

    Without this, the Python interpreter's icon appears in the taskbar even
    when pywebview's own icon= kwarg correctly sets the window titlebar icon.
    """
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except AttributeError:
        pass   # Not on Windows (shouldn't happen, but be defensive)


def main():
    _set_taskbar_icon()
    api = SteamRouletteAPI()

    window = webview.create_window(
        title="PC Game Roulette",
        url=os.path.join(WEB_DIR, "index.html"),
        js_api=api,
        width=1000,
        height=680,
        min_size=(640, 480),
        background_color="#0e1117",
    )

    api.set_window(window)

    # The window icon (title bar + taskbar) is set via webview.start's icon
    # kwarg.  When packaged with PyInstaller (--icon=app.ico) the .exe embeds
    # the icon so the taskbar button shows it directly; running unpackaged via
    # python/pythonw will still show the interpreter's icon in the taskbar,
    # which is expected.  Fall back gracefully if the file is missing so the
    # app still launches on a fresh checkout that hasn't run
    # tools/generate_icon.py yet.
    start_kwargs = {"debug": ("--debug" in sys.argv)}
    if os.path.isfile(ICON_PATH):
        start_kwargs["icon"] = ICON_PATH

    # Persist the WebView2 profile so its HTTP cache (game header images, etc.)
    # survives between launches instead of being thrown away each run.  Only
    # enable it if we can create the dir; otherwise fall back to pywebview's
    # default ephemeral profile.
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        start_kwargs["private_mode"] = False
        start_kwargs["storage_path"] = PROFILE_DIR
    except OSError:
        pass

    webview.start(**start_kwargs)


if __name__ == "__main__":
    main()
