"""
PC Game Roulette — entry point.
Launches the pywebview window and wires up the Python js_api.
"""

import os
import sys
import webview
from backend import SteamRouletteAPI

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(SCRIPT_DIR, "web")


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
    webview.start(debug=("--debug" in sys.argv))


if __name__ == "__main__":
    main()
