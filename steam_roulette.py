"""
Steam Roulette
Picks a random game from your Steam library, optionally filtered by collection.
Reads collections from Steam's local JSON file (same source as your collections script).
"""

import json
import random
import requests
import os
import sys

# ── CONFIG ──────────────────────────────────────────────────────────────────
STEAM_API_KEY = "A40AB3E79C7CD5A110F4DA2A417CF044"
STEAM_ID      = "76561198003181985"   # 64-bit SteamID

# Path to Steam's cloud storage collections file
COLLECTIONS_JSON = r"C:\Program Files (x86)\Steam\userdata\42916257\config\cloudstorage\cloud-storage-namespace-1.json"
# ────────────────────────────────────────────────────────────────────────────


def fetch_library(api_key, steam_id):
    """Fetch full library with game names and playtime from Steam API."""
    url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
    params = {
        "key": api_key,
        "steamid": steam_id,
        "include_appinfo": 1,
        "include_played_free_games": 1,
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    games = resp.json().get("response", {}).get("games", [])
    # Return dict keyed by appid string for easy lookup
    return {str(g["appid"]): g for g in games}


def load_collections(json_path):
    """Load collection->appid mapping from Steam's cloudstorage namespace file."""
    if not os.path.exists(json_path):
        print(f"[warn] Collections file not found: {json_path}")
        print("       Running without collection filtering.\n")
        return {}

    with open(json_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    collections = {}
    for entry in raw:
        key = entry[0]
        if not key.startswith("user-collections.uc-"):
            continue
        try:
            value = json.loads(entry[1]["value"])
        except (KeyError, json.JSONDecodeError):
            continue

        name = value.get("name", key)
        app_ids = [
            str(aid).replace("app/", "")
            for aid in value.get("added", [])
        ]
        if app_ids:
            collections[name] = app_ids

    return collections


def pick_game(games_dict, appid_pool=None):
    """Pick a random game. If appid_pool given, filter to those appids."""
    if appid_pool:
        candidates = {aid: g for aid, g in games_dict.items() if aid in appid_pool}
    else:
        candidates = games_dict

    if not candidates:
        return None

    appid, game = random.choice(list(candidates.items()))
    return game


def format_playtime(minutes):
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes / 60
    return f"{hours:.1f}h"


def main():
    print("=" * 50)
    print("         🎲  STEAM ROULETTE  🎲")
    print("=" * 50)
    print()

    # Load data
    print("Fetching your library from Steam...")
    try:
        library = fetch_library(STEAM_API_KEY, STEAM_ID)
    except Exception as e:
        print(f"Error fetching library: {e}")
        sys.exit(1)
    print(f"Loaded {len(library)} games.\n")

    collections = load_collections(COLLECTIONS_JSON)

    # Collection selection
    appid_pool = None
    if collections:
        sorted_cols = sorted(collections.keys())
        print("Your collections:")
        for i, name in enumerate(sorted_cols, 1):
            count = len(collections[name])
            print(f"  [{i:>3}] {name}  ({count} games)")
        print(f"  [  0] All games (no filter)")
        print()

        while True:
            choice = input("Pick a collection number (or 0 for all): ").strip()
            if choice == "0":
                break
            if choice.isdigit() and 1 <= int(choice) <= len(sorted_cols):
                chosen = sorted_cols[int(choice) - 1]
                appid_pool = set(collections[chosen])
                print(f"\nFiltering to: {chosen} ({len(appid_pool)} games)\n")
                break
            print("Invalid choice, try again.")
    else:
        print("No collections loaded — picking from full library.\n")

    # Spin loop
    while True:
        game = pick_game(library, appid_pool)
        if not game:
            print("No games found in that pool!")
            break

        name     = game.get("name", "Unknown")
        appid    = game.get("appid")
        playtime = game.get("playtime_forever", 0)
        store    = f"https://store.steampowered.com/app/{appid}"

        print("-" * 50)
        print(f"  🎮  {name}")
        print(f"      Playtime : {format_playtime(playtime)}")
        print(f"      Store    : {store}")
        print("-" * 50)
        print()

        again = input("Spin again? [y/N]: ").strip().lower()
        if again != "y":
            print("\nHave fun! 🎮")
            break
        print()


if __name__ == "__main__":
    main()
