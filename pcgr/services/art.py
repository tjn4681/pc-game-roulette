"""
Winner-panel game art.

Resolves a Steam game's header image for the spin-result panel, where
reliability matters more than latency: disk cache → user-uploaded grid art (for
non-Steam shortcuts) → the Steam CDN hosts → the appdetails canonical URL.

Holds a reference to the Steam launcher only to reach its install path (where
non-Steam shortcut grid art lives); all the byte-level work is in
``pcgr.sources.images``.
"""

import json
import os
import urllib.request

from pcgr.config import ART_CACHE_DIR
from pcgr.sources.images import (
    _cache_and_return, _find_grid_images, _read_image_as_data_url, _try_fetch_image,
)


class ArtService:
    def __init__(self, steam):
        self.steam = steam

    def get_game_art(self, appid_str):
        """
        Fetch a game's header image via Python's network stack.  Used for the
        winner panel where reliability matters more than latency.
        Resolution order:
          1. Disk cache       (cache/art/<appid>.<ext>)
          2. Steam grid folder — user-uploaded art for non-Steam shortcuts
          3. Steam CDN URLs   (with age-gate cookies, validates magic bytes)
          4. Steam appdetails API — returns the canonical header_image URL,
                                    which works for some restricted games the
                                    direct CDN paths don't.
        Returns {"status": "ok",  "data": "data:image/<mime>;base64,..."}
             or {"status": "notfound"}
        """
        try:
            appid = int(appid_str)
        except (ValueError, TypeError):
            return {"status": "notfound"}

        # 1. Disk cache (jpg or png)
        for ext in ("jpg", "png"):
            cache_path = os.path.join(ART_CACHE_DIR, f"{appid}.{ext}")
            if os.path.isfile(cache_path):
                hit = _read_image_as_data_url(cache_path)
                if hit:
                    return {"status": "ok", "data": hit}

        # 2. Steam grid folder (non-Steam shortcut user-uploaded art)
        steam_path = self.steam.steam_path
        if steam_path:
            for grid_path in _find_grid_images(steam_path, appid):
                hit = _read_image_as_data_url(grid_path)
                if hit:
                    # Cache it
                    ext = "png" if grid_path.lower().endswith(".png") else "jpg"
                    try:
                        with open(grid_path, "rb") as src, \
                             open(os.path.join(ART_CACHE_DIR, f"{appid}.{ext}"), "wb") as dst:
                            dst.write(src.read())
                    except OSError:
                        pass
                    return {"status": "ok", "data": hit}

        # 3. Steam CDN URLs with mature-content cookies
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Cookie":     "birthtime=283993201; mature_content=1; wants_mature_content=1; "
                          "lastagecheckage=1-0-1979",
            "Referer":    "https://store.steampowered.com/",
        }
        urls = [
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
            f"https://steamcdn-a.akamaihd.net/steam/apps/{appid}/header.jpg",
            f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg",
            f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg",
        ]
        for url in urls:
            data = _try_fetch_image(url, headers)
            if data:
                return _cache_and_return(appid, data)

        # 4. Steam appdetails API — the canonical header_image URL is sometimes
        #    served from a different host than the direct paths above.
        try:
            api_url = (f"https://store.steampowered.com/api/appdetails"
                       f"?appids={appid}&filters=basic&cc=us&l=english")
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            entry = payload.get(str(appid), {})
            if entry.get("success") and "data" in entry:
                canon = entry["data"].get("header_image")
                if canon and canon not in urls:
                    data = _try_fetch_image(canon, headers)
                    if data:
                        return _cache_and_return(appid, data)
        except Exception:
            pass

        return {"status": "notfound"}
