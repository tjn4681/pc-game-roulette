"""
GOG Galaxy local database access.

GOG Galaxy keeps the user's full owned library — across GOG itself and every
platform integration (Epic, Steam, Battle.net, Origin, Uplay…) — in a SQLite
database under %ProgramData%.  We open it read-only so we never fight Galaxy for
its write lock.

Two things we pull out:
  * enrichment (playtime, tags, cover images) for games we already know about
  * the full game list for a given platform prefix (used to populate the GOG
    tab and to fold in integrated launchers)

All functions degrade gracefully to empty results if Galaxy isn't installed or
the DB is locked/unexpected — callers fall back to their own detection.
"""

import json
import os
import sqlite3

from appconfig import GOG_GALAXY_DB


def _galaxy_db_open():
    """Open Galaxy's SQLite DB read-only, or return None if unavailable."""
    if not os.path.isfile(GOG_GALAXY_DB):
        return None
    try:
        return sqlite3.connect(
            f"file:{GOG_GALAXY_DB}?mode=ro", uri=True, timeout=2.0,
        )
    except sqlite3.Error:
        return None


def query_galaxy_enrichment(release_keys):
    """For a list of releaseKeys (like ``['gog_1207659146', 'epic_<id>', ...]``)
    return a dict mapping releaseKey -> enrichment payload:

        {
          'playtime_minutes': int,
          'tags':             [str, ...],
          'image_background': str (url, may be empty),
          'image_vertical':   str (url),
          'image_icon':       str (url),
        }

    Missing rows simply yield empty fields — callers can blindly look up any
    releaseKey and get a usable dict back.  Single connection, three small
    queries, batched with SQL ``IN`` so it scales to thousands of games."""
    if not release_keys:
        return {}

    conn = _galaxy_db_open()
    if conn is None:
        return {}

    # Build a default-filled dict for every requested key — callers don't
    # need to .get() defensively on each field.
    result = {}
    for k in release_keys:
        result[k] = {
            "playtime_minutes": 0,
            "tags":             [],
            "image_background": "",
            "image_vertical":   "",
            "image_icon":       "",
        }

    # SQLite has a 999-parameter limit per statement by default.  Chunk safely.
    chunk_size = 800
    keys = list(release_keys)

    try:
        cur = conn.cursor()

        # GamePieceType id for originalImages — looked up once, reused.
        cur.execute(
            "SELECT id FROM GamePieceTypes WHERE type = 'originalImages' LIMIT 1"
        )
        row = cur.fetchone()
        images_type_id = row[0] if row else None

        for i in range(0, len(keys), chunk_size):
            chunk = keys[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))

            # Playtime
            cur.execute(
                f"SELECT releaseKey, minutesInGame FROM GameTimes "
                f"WHERE releaseKey IN ({placeholders})",
                chunk,
            )
            for rk, minutes in cur.fetchall():
                if rk in result and isinstance(minutes, int) and minutes > 0:
                    result[rk]["playtime_minutes"] = minutes

            # Tags (a game can have multiple tag rows)
            cur.execute(
                f"SELECT releaseKey, tag FROM UserReleaseTags "
                f"WHERE releaseKey IN ({placeholders}) AND tag IS NOT NULL",
                chunk,
            )
            for rk, tag in cur.fetchall():
                if rk in result and tag:
                    tag = tag.strip()
                    if tag and tag not in result[rk]["tags"]:
                        result[rk]["tags"].append(tag)

            # Image URLs from originalImages piece
            if images_type_id is not None:
                cur.execute(
                    f"SELECT releaseKey, value FROM GamePieces "
                    f"WHERE gamePieceTypeId = ? AND releaseKey IN ({placeholders})",
                    [images_type_id] + list(chunk),
                )
                for rk, value in cur.fetchall():
                    if rk not in result or not value:
                        continue
                    try:
                        parsed = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    result[rk]["image_background"] = parsed.get("background", "") or ""
                    result[rk]["image_vertical"]   = parsed.get("verticalCover", "") or ""
                    result[rk]["image_icon"]       = parsed.get("squareIcon", "") or ""

        # Sort tags alphabetically for stable ordering
        for rk in result:
            result[rk]["tags"].sort(key=str.lower)
    except sqlite3.Error:
        # Partial result is still useful — return what we managed to gather
        pass
    finally:
        conn.close()

    return result


def _galaxy_lookup_key(game, platform):
    """Pick the releaseKey Galaxy uses for this game.

    For Epic specifically, Galaxy keys on AppName (e.g. 'epic_Cowbird'), NOT
    the catalogItemId Epic's OAuth API returns.  So when we have both (OAuth
    library populates 'app_name'), prefer that — it's the only way to bridge
    OAuth-sourced games to Galaxy-sourced enrichment.

    For GOG, the raw_id (gameID) IS the suffix Galaxy uses.
    For Steam, the appid IS the suffix.
    """
    if platform == "epic" and game.get("app_name"):
        return f"epic_{game['app_name']}"
    gid = game.get("id") or ""
    if "_" in gid:
        return gid
    if platform and game.get("raw_id"):
        return f"{platform}_{game['raw_id']}"
    return None


def apply_galaxy_enrichment(games, platform=None):
    """Mutate `games` in place, decorating each entry with Galaxy-sourced
    playtime / image / tag data.  Safe to call regardless of whether Galaxy
    is installed — if it isn't, every game just gets the default-empty values.

    Bridges the OAuth/Galaxy ID mismatch for Epic via the game's app_name."""
    if not games:
        return games

    # Build a parallel list of lookup keys (one per game) so we can map back
    keys = []
    for g in games:
        keys.append(_galaxy_lookup_key(g, platform))

    # Dedupe before querying
    unique_keys = list({k for k in keys if k})
    enrichment = query_galaxy_enrichment(unique_keys)

    for g, key in zip(games, keys):
        if not key:
            continue
        data = enrichment.get(key)
        if not data:
            continue
        # Only fill in fields the game doesn't already define — avoids
        # clobbering richer per-source data (e.g. OAuth catalog metadata)
        g.setdefault("playtime_minutes", data["playtime_minutes"])
        g.setdefault("image_background", data["image_background"])
        g.setdefault("image_vertical",   data["image_vertical"])
        g.setdefault("image_icon",       data["image_icon"])
        g.setdefault("tags",             data["tags"])
    return games


def query_galaxy_db_for_platform(platform_prefix):
    """Return a sorted list of {id, raw_id, name, platform, source} dicts for
    every owned game whose releaseKey starts with ``<platform_prefix>_`` in
    GOG Galaxy's local SQLite database.  Works for GOG itself and for any
    platform the user has integrated with Galaxy (Epic, Steam, etc.).

    Returns ``[]`` if the DB file is missing, locked, or has an unexpected
    schema — the caller should fall back to its registry/manifest method."""
    if not os.path.isfile(GOG_GALAXY_DB):
        return []

    games = {}   # release_key -> game dict; second pass prefers 'title' rows
    conn = None
    try:
        # Read-only URI so we don't fight Galaxy for the write lock.
        conn = sqlite3.connect(
            f"file:{GOG_GALAXY_DB}?mode=ro", uri=True, timeout=2.0,
        )
        cur = conn.cursor()
        cur.execute(
            """
            SELECT lr.releaseKey, gp.value, gpt.type
              FROM LibraryReleases lr
              LEFT JOIN GamePieces      gp  ON gp.releaseKey      = lr.releaseKey
              LEFT JOIN GamePieceTypes  gpt ON gpt.id             = gp.gamePieceTypeId
             WHERE lr.releaseKey LIKE ?
               AND gpt.type IN ('title', 'originalTitle')
            """,
            (f"{platform_prefix}_%",),
        )
        rows = cur.fetchall()
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()

    for release_key, value, type_name in rows:
        if not value:
            continue
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        name = parsed.get("title") if isinstance(parsed, dict) else None
        if not name:
            continue

        # Prefer 'title' over 'originalTitle' when both exist for the same key
        existing = games.get(release_key)
        if existing is not None and type_name != "title":
            continue

        raw_id = release_key.split("_", 1)[1] if "_" in release_key else release_key
        games[release_key] = {
            "id":       release_key,
            "raw_id":   raw_id,
            "name":     name,
            "platform": platform_prefix,
            "source":   "galaxy",
        }

    return sorted(games.values(), key=lambda g: g["name"].lower())
