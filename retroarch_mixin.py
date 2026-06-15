"""
RetroArchMixin for SteamRouletteAPI.

RetroArch support: locating the install, parsing playlists, serving
downscaled box art over a local HTTP server, and launching games.

Methods here run on the single js_api object via cooperative multiple
inheritance, so they share instance state (self.*) set up in the core
SteamRouletteAPI.__init__.
"""

import hashlib
import io
import os
import retroarch
import threading

from appconfig import CACHE_DIR, load_config, save_config


class RetroArchMixin:
    # ── RetroArch ─────────────────────────────────────────────────────────

    def _get_retroarch_dir(self):
        """Locate (and cache) the RetroArch install dir, honouring a saved
        path from config so the drive scan is skipped on later launches."""
        if self._retroarch_dir:
            return self._retroarch_dir
        if self._ra_dir_checked:
            # Already scanned and came up empty — don't re-walk every drive on
            # each detect_platforms() call (most users won't have RetroArch).
            return None
        self._ra_dir_checked = True
        saved = (load_config() or {}).get("retroarch_path")
        d = retroarch.find_retroarch_dir(saved)
        if d:
            self._retroarch_dir = d
            if d != saved:                 # persist for next time
                cfg = load_config()
                cfg["retroarch_path"] = d
                save_config(cfg)
        return d

    def _ensure_retroarch(self):
        """Parse playlists once and build an id->game index.  Cached for the
        process lifetime; reload_retroarch() refreshes it."""
        if self._ra_playlists is not None:
            return self._ra_playlists
        d = self._get_retroarch_dir()
        playlists = retroarch.load_playlists(d) if d else []
        index = {}
        for pl in playlists:
            for g in pl["games"]:
                index[g["id"]] = g
        self._ra_playlists = playlists
        self._ra_index = index
        return playlists

    def reload_retroarch(self):
        """Drop caches so the next call re-scans playlists (e.g. after the
        user scans new ROMs in RetroArch).  Also re-arms install detection in
        case RetroArch was installed after this session started."""
        self._ra_playlists = None
        self._ra_index = None
        if not self._retroarch_dir:
            self._ra_dir_checked = False
        return self.get_retroarch_playlists()

    @staticmethod
    def _ra_public(g):
        """Trim a game dict to what the frontend needs — no filesystem paths.
        Art and launch are resolved server-side by id."""
        return {
            "id":        g["id"],
            "raw_id":    g["id"],
            "name":      g["name"],
            "platform":  "retroarch",
            "system":    g["system"],
            "has_thumb": bool(g.get("thumb_path")),
        }

    def get_retroarch_playlists(self):
        """Lightweight grid data: one entry per system playlist (name, count,
        and a sample game id whose boxart can back the card).  No per-game
        payload, so this stays tiny even with a 10k-ROM library."""
        playlists = self._ensure_retroarch()
        if not self._get_retroarch_dir():
            return {"status": "notfound",
                    "message": "RetroArch not found on this PC."}
        out = []
        for pl in playlists:
            sample = next((g["id"] for g in pl["games"] if g.get("thumb_path")), None)
            out.append({"name": pl["name"], "system": pl["system"],
                        "count": pl["count"], "sample_id": sample})
        total = sum(pl["count"] for pl in playlists)
        port = self._ensure_art_server()
        art_base = f"http://127.0.0.1:{port}/ra" if port else None
        return {"status": "ok", "total": total, "playlists": out,
                "art_base": art_base}

    def get_retroarch_games(self, system=None):
        """Return trimmed game dicts for one system, or every system when
        `system` is None (RetroArch Library card and Leave It To Fate)."""
        playlists = self._ensure_retroarch()
        games = []
        for pl in playlists:
            if system is None or pl["system"] == system:
                games.extend(self._ra_public(g) for g in pl["games"])
        return {"status": "ok", "games": games}

    # Local boxart server ----------------------------------------------------
    # RetroArch boxarts are big PNGs (hundreds of KB) and a big library has
    # thousands of them, so we can't ship them over the js_api bridge as base64
    # for every card/reel tile.  Instead we run a tiny localhost HTTP server
    # that serves each game's boxart by id, downscaled (Pillow) and disk-cached
    # to a few tens of KB.  The browser then loads them like any <img> URL —
    # in parallel, lazily, and cached in the WebView2 profile — so cards and
    # the whole reel can show art without choking the app.

    def _art_bytes(self, thumb_path, max_w):
        """Return (bytes, content_type) for a boxart, downscaled to max_w px
        wide and disk-cached.  Falls back to the original PNG if Pillow is
        unavailable or anything goes wrong."""
        if not max_w:
            try:
                with open(thumb_path, "rb") as f:
                    return f.read(), "image/png"
            except OSError:
                return None, None
        # Cache key includes mtime so a re-scanned / replaced boxart at the
        # same path invalidates the old downscaled copy instead of serving it
        # forever.
        try:
            mtime = int(os.path.getmtime(thumb_path))
        except OSError:
            mtime = 0
        key = hashlib.sha1(
            f"{thumb_path}|{max_w}|{mtime}".encode("utf-8", "replace")).hexdigest()[:16]
        cache_dir = os.path.join(CACHE_DIR, "ra_thumbs")
        cpath = os.path.join(cache_dir, f"{key}.jpg")
        if os.path.isfile(cpath):
            try:
                with open(cpath, "rb") as f:
                    return f.read(), "image/jpeg"
            except OSError:
                pass
        try:
            from PIL import Image
            im = Image.open(thumb_path)
            # Flatten any transparency onto white (JPEG has no alpha; the
            # default convert("RGB") would turn transparent pixels black).
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
            if im.width > max_w:
                im = im.resize((max_w, max(1, round(im.height * max_w / im.width))),
                               Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82)
            data = buf.getvalue()
            # Atomic write (temp + replace) so a concurrent request can't read
            # a half-written file.
            try:
                os.makedirs(cache_dir, exist_ok=True)
                tmp = f"{cpath}.{os.getpid()}.tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, cpath)
            except OSError:
                pass
            return data, "image/jpeg"
        except Exception:
            try:
                with open(thumb_path, "rb") as f:
                    return f.read(), "image/png"
            except OSError:
                return None, None

    def _ensure_art_server(self):
        """Start the localhost boxart server (once) and return its port."""
        with self._ra_art_lock:
            if self._ra_art_port:
                return self._ra_art_port
            import http.server
            import socketserver
            api = self

            class _Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass  # stay quiet
                def do_GET(self):
                    # /ra/<game_id>[/<max_w>]
                    parts = self.path.split("?")[0].strip("/").split("/")
                    if len(parts) < 2 or parts[0] != "ra":
                        self.send_response(404); self.end_headers(); return
                    gid  = parts[1]
                    maxw = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                    g = (api._ra_index or {}).get(gid)
                    if not g or not g.get("thumb_path"):
                        self.send_response(404); self.end_headers(); return
                    data, ctype = api._art_bytes(g["thumb_path"], maxw)
                    if not data:
                        self.send_response(404); self.end_headers(); return
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    try:
                        self.wfile.write(data)
                    except OSError:
                        pass

            class _ArtServer(socketserver.ThreadingTCPServer):
                allow_reuse_address = True   # rebind cleanly after a restart
                daemon_threads = True

            # Prefer a fixed port so art URLs stay stable between launches and
            # WebView2's persistent HTTP cache can reuse them; fall back to an
            # ephemeral port if it's taken.
            srv = None
            for port in (47653, 0):
                try:
                    srv = _ArtServer(("127.0.0.1", port), _Handler)
                    break
                except OSError:
                    continue
            if srv is None:
                self._ra_art_port = None
                return None
            self._ra_art_port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True,
                             name="ra-art-server").start()
            return self._ra_art_port

    def launch_retroarch_game(self, game_id):
        """Launch a ROM: `retroarch.exe -L <core> <rom>`.  ROM + core are
        resolved from the server-side index by id; -L is omitted if we
        couldn't resolve a core (RetroArch then auto-picks one)."""
        self._ensure_retroarch()
        g = (self._ra_index or {}).get(game_id)
        if not g:
            return {"status": "error", "message": "Unknown RetroArch game."}
        ra_dir = self._get_retroarch_dir() or ""
        exe = os.path.join(ra_dir, "retroarch.exe")
        if not os.path.isfile(exe):
            return {"status": "error", "message": "retroarch.exe not found."}
        rom  = g.get("rom_path")
        core = g.get("core_path")
        if not rom or not os.path.isfile(rom):
            return {"status": "error", "message": "ROM file not found on disk."}
        try:
            import subprocess
            args = [exe]
            if core and os.path.isfile(core):
                args += ["-L", core]
            args.append(rom)
            subprocess.Popen(args, cwd=ra_dir,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
