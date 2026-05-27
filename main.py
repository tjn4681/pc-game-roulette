"""
PC Game Roulette — entry point.
Launches the pywebview window and wires up the Python js_api.
"""

import os
import sys
import webview
from backend import SteamRouletteAPI

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR    = os.path.join(SCRIPT_DIR, "web")
ICON_PATH  = os.path.join(SCRIPT_DIR, "app.ico")


def main():
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

    # The window icon (used in the title bar and taskbar) is set via
    # webview.start's icon kwarg.  Fall back gracefully if the file is missing
    # so the app still launches on a fresh checkout that hasn't run
    # tools/generate_icon.py yet.
    start_kwargs = {"debug": ("--debug" in sys.argv)}
    if os.path.isfile(ICON_PATH):
        start_kwargs["icon"] = ICON_PATH
    webview.start(**start_kwargs)


if __name__ == "__main__":
    main()
