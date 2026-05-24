# Steam Roulette

## What this is
A portable Windows desktop app that picks a random game from the user's Steam
library with a slot-machine spin animation. The user picks one of their Steam
Collections (custom categories), the app spins through that collection's games
and lands on one. Shipped as a single .exe so non-technical friends can run it.

## Stack
- Python backend: Steam file detection, collections JSON parsing, name lookups
- pywebview for the native window, exposing a Python js_api to the frontend
- HTML/CSS/JS frontend in web/ for all UI and the spin animation
- Packaged to one Windows .exe with PyInstaller

## Steam data facts (don't rediscover these)
- Custom Collections are NOT exposed by any Steam API. They live only in:
  <Steam>\userdata\<accountid>\config\cloudstorage\cloud-storage-namespace-1.json
- <accountid> differs per account, so the path must be auto-discovered.
- The file is a JSON array of [key, valueObject] pairs.
- Collection entries have keys starting with "user-collections.uc-".
- entry[1]["value"] is itself a JSON-encoded STRING — parse it a second time.
- The parsed value has "name" (collection name) and "added" (list of app IDs).
- Skip soft-deleted collections (is_deleted set) and ones with empty names.
- The file contains app IDs only, no game names.
- Header images are free, no API key, at:
  https://cdn.cloudflare.steamstatic.com/steam/apps/<appid>/header.jpg
- Game names: fetch on demand from
  https://store.steampowered.com/api/appdetails?appids=<appid> (no key needed).

## Conventions
- No Steam Web API key required anywhere. Keep it that way — friends shouldn't
  need to sign up for anything.
- Cache fetched game names to cache/names.json.
- Persist the detected Steam path to a local config file so later launches skip detection.