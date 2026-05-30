"""
PC Game Roulette — entry point.
Launches the pywebview window and wires up the Python js_api.
"""

import ctypes
import hashlib
import os
import shutil
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


def _web_fingerprint():
    """Fingerprint the bundled web/ assets (size + mtime of every html/js/css).
    Changes whenever the app's frontend is edited or updated."""
    parts = []
    for root, _, files in os.walk(WEB_DIR):
        for fn in sorted(files):
            if fn.lower().endswith((".html", ".js", ".css")):
                fp = os.path.join(root, fn)
                try:
                    st = os.stat(fp)
                    parts.append(f"{os.path.relpath(fp, WEB_DIR)}:{st.st_size}:{int(st.st_mtime)}")
                except OSError:
                    pass
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _sync_profile_cache():
    """Clear WebView2's HTTP cache whenever the web/ assets change.

    The persistent profile caches everything WebView2 fetches — including the
    app's own HTML/JS/CSS served from the local bottle server.  Without this,
    a code change or app update would keep serving the stale shell from cache.
    We only wipe when the fingerprint changes, so CDN game art stays cached
    across normal (unchanged) launches.
    """
    marker = os.path.join(PROFILE_DIR, ".shell_fingerprint")
    current = _web_fingerprint()
    try:
        previous = ""
        if os.path.isfile(marker):
            with open(marker, "r", encoding="utf-8") as f:
                previous = f.read().strip()
        if previous != current:
            default = os.path.join(PROFILE_DIR, "EBWebView", "Default")
            for sub in ("Cache", "Code Cache", "GPUCache"):
                shutil.rmtree(os.path.join(default, sub), ignore_errors=True)
            with open(marker, "w", encoding="utf-8") as f:
                f.write(current)
    except OSError:
        pass


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
        _sync_profile_cache()   # drop cache if the frontend changed
        start_kwargs["private_mode"] = False
        start_kwargs["storage_path"] = PROFILE_DIR
    except OSError:
        pass

    webview.start(**start_kwargs)


if __name__ == "__main__":
    main()
