"""
Winner-art image helpers.

Small utilities for the game art shown on the spin result: sniffing JPEG/PNG
bytes, turning a local image into a data URL, finding user-uploaded grid art for
non-Steam shortcuts, fetching a remote image (only if it's really an image),
and caching fetched bytes to disk.
"""

import base64
import os
import urllib.error
import urllib.request

from appconfig import ART_CACHE_DIR

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC  = b"\x89PNG\r\n\x1a\n"


def _image_mime(data):
    if data[:3] == _JPEG_MAGIC:
        return "image/jpeg"
    if data[:8] == _PNG_MAGIC:
        return "image/png"
    return None


def _read_image_as_data_url(path):
    """Return data URL for a local image, or None if not a valid JPEG/PNG."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    mime = _image_mime(data)
    if not mime or len(data) < 2000:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _find_grid_images(steam_path, appid):
    """Yield paths to user-uploaded grid art for a (non-Steam) shortcut.
    Steam stores custom artwork at userdata/<id>/config/grid/<appid>.<ext>."""
    userdata = os.path.join(steam_path, "userdata")
    if not os.path.isdir(userdata):
        return
    try:
        accounts = os.listdir(userdata)
    except OSError:
        return
    # Steam grids use several filename conventions — try the header-shaped ones
    candidates = [f"{appid}.jpg", f"{appid}.png", f"{appid}_header.jpg", f"{appid}_header.png"]
    for account_id in accounts:
        grid_dir = os.path.join(userdata, account_id, "config", "grid")
        if not os.path.isdir(grid_dir):
            continue
        for fname in candidates:
            path = os.path.join(grid_dir, fname)
            if os.path.isfile(path):
                yield path


def _try_fetch_image(url, headers, timeout=8):
    """Fetch URL via urllib, return raw bytes only if they're a real image."""
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        if _image_mime(data) and len(data) > 2000:
            return data
    except Exception:
        pass
    return None


def _cache_and_return(appid, data):
    """Save image bytes to disk cache and return a {status, data} response."""
    mime = _image_mime(data)
    ext  = "png" if mime == "image/png" else "jpg"
    cache_path = os.path.join(ART_CACHE_DIR, f"{appid}.{ext}")
    try:
        with open(cache_path, "wb") as f:
            f.write(data)
    except OSError:
        pass
    b64 = base64.b64encode(data).decode("ascii")
    return {"status": "ok", "data": f"data:{mime};base64,{b64}"}
