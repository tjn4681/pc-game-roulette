"""
PC Game Roulette — entry point.
Launches the pywebview window and wires up the Python js_api.
"""

import ctypes
import os
import sys
import webview
from backend import SteamRouletteAPI

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR    = os.path.join(SCRIPT_DIR, "web")
ICON_PATH  = os.path.join(SCRIPT_DIR, "app.ico")
# Persistent WebView2 profile.  By default pywebview runs in private_mode,
# which spins up a throw-away profile in a random %TEMP% folder every launch —
# so WebView2's HTTP cache is discarded and every game header image is
# re-downloaded from the Steam CDN on each run (and %TEMP% slowly fills with
# leftover EBWebView folders).  Pinning a persistent profile here lets the
# browser cache survive between launches: card art loads from disk on repeat
# runs instead of the network.  Kept under cache/ so the app stays portable.
PROFILE_DIR = os.path.join(SCRIPT_DIR, "cache", "webview")

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
    start_kwargs = {
        "debug": ("--debug" in sys.argv),
        # Persist the WebView2 profile so its HTTP cache (game header images,
        # etc.) survives between launches instead of being thrown away.
        "private_mode": False,
        "storage_path": PROFILE_DIR,
    }
    if os.path.isfile(ICON_PATH):
        start_kwargs["icon"] = ICON_PATH

    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
    except OSError:
        # If we can't create the profile dir (e.g. read-only location), fall
        # back to pywebview's default ephemeral profile rather than failing.
        start_kwargs.pop("private_mode", None)
        start_kwargs.pop("storage_path", None)

    webview.start(**start_kwargs)


if __name__ == "__main__":
    main()
