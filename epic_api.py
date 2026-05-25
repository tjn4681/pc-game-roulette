"""
Epic Games library API client.

Once we have a valid access token (via epic_auth), this module talks to two
Epic endpoints to assemble the user's full owned library:

  * Launcher API — returns the raw list of owned "assets" (catalogItemId +
    namespace + appName for each), but no human-readable titles.
  * Catalog API  — given a namespace and a batch of catalog item IDs, returns
    full metadata including titles, categories, image URLs, etc.

The output is shaped to match what backend.get_epic_games() already returns
from the GOG-Galaxy and manifests sources, so the frontend treats all three
the same way.

Stdlib only — no external HTTP libraries.
"""

import json
import urllib.error
import urllib.parse
import urllib.request


USER_AGENT = ("EpicGamesLauncher/14.0.8-22004686+++Portal+Release-Live "
              "Windows/10.0.19044.1.768.64bit")

LAUNCHER_API = ("https://launcher-public-service-prod06.ol.epicgames.com"
                "/launcher/api/public/assets/Windows")
CATALOG_API  = ("https://catalog-public-service-prod06.ol.epicgames.com"
                "/catalog/api/shared")

# Titles containing any of these substrings are treated as non-game items and
# dropped from the library list (engine installers, redistributables, etc.).
SKIP_KEYWORDS = {
    "epic games launcher", "directx", "vcredist", "redistributable",
    "prerequisites", "unreal engine", "support-a-creator",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Low-level HTTP
# ─────────────────────────────────────────────────────────────────────────────

def _authed_get(url, access_token):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"bearer {access_token}")
    req.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
#  Endpoint wrappers
# ─────────────────────────────────────────────────────────────────────────────

def get_owned_assets(access_token):
    """Return the raw list of owned launcher assets.

    Each entry looks like:
      { 'appName': '...', 'labelName': 'Live', 'buildVersion': '...',
        'catalogItemId': '...', 'namespace': '...', 'assetId': '...' }
    """
    return _authed_get(f"{LAUNCHER_API}?label=Live", access_token)


def get_catalog_items(access_token, namespace, catalog_item_ids):
    """Bulk-fetch catalog metadata for a list of catalogItemIds in one namespace.
    Returns a dict mapping catalogItemId -> item metadata.

    The catalog API accepts up to ~50 IDs per request; we chunk if needed."""
    if not catalog_item_ids:
        return {}

    ids = list(catalog_item_ids)
    result = {}
    chunk_size = 50

    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        query = "&".join(f"id={urllib.parse.quote(cid)}" for cid in chunk)
        url = (f"{CATALOG_API}/namespace/{urllib.parse.quote(namespace)}"
               f"/bulk/items?{query}"
               f"&country=US&locale=en-US&includeMainGameDetails=true")
        try:
            chunk_data = _authed_get(url, access_token)
            if isinstance(chunk_data, dict):
                result.update(chunk_data)
        except urllib.error.HTTPError:
            # Skip namespaces that return errors — typically expired catalog
            # entries or namespaces no longer accessible.  Better to ship a
            # partial library than fail the whole fetch.
            continue

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  High-level: owned games with titles
# ─────────────────────────────────────────────────────────────────────────────

def _is_game(item):
    """Heuristic: is this catalog item an actual game, or a tool/engine/SDK?

    Items have a 'categories' array of {path: '...'} objects.  The path 'games'
    marks something as a game (vs 'engines', 'plugins', 'audio', etc.)."""
    cats = item.get("categories") or []
    paths = {(c.get("path") or "").lower() for c in cats}

    # If explicitly tagged as a game, keep it
    if "games" in paths:
        return True
    # If clearly an engine or tool, drop it
    if paths & {"engines", "software", "plugins", "assets", "audio",
                "modificators", "applications/devtools"}:
        return False
    # Default: keep (Epic is inconsistent about tagging older entries)
    return True


def _app_name_for(item):
    """Extract the launcher AppName for an item, used for direct Epic launching
    via the com.epicgames.launcher://apps/<AppName> URI scheme."""
    release_info = item.get("releaseInfo") or []
    if release_info and isinstance(release_info, list):
        # Prefer the most recent release
        return (release_info[0].get("appId") or "").strip()
    return ""


def get_owned_games_with_titles(access_token):
    """Top-level call used by the backend: fetch every owned Epic game and
    resolve its display title.  Returns a sorted list of game dicts in our
    standard shape:

        { id:       'epic_<catalogItemId>',
          raw_id:   '<catalogItemId>',
          name:     'Display Title',
          platform: 'epic',
          source:   'oauth',
          app_name: '<launcher AppName, may be empty>',
          namespace: '<Epic namespace>' }

    Drops non-game items (engines, tools, redistributables)."""
    assets = get_owned_assets(access_token)
    if not isinstance(assets, list):
        return []

    # Group asset IDs by namespace so we can bulk-fetch their metadata
    by_namespace = {}
    for asset in assets:
        ns = (asset.get("namespace") or "").strip()
        cid = (asset.get("catalogItemId") or "").strip()
        if ns and cid:
            by_namespace.setdefault(ns, set()).add(cid)

    games = {}   # catalogItemId -> game dict (deduped across namespaces)

    for namespace, ids in by_namespace.items():
        items = get_catalog_items(access_token, namespace, ids)
        for catalog_item_id, item in items.items():
            title = (item.get("title") or "").strip()
            if not title:
                continue
            if any(kw in title.lower() for kw in SKIP_KEYWORDS):
                continue
            if not _is_game(item):
                continue

            games[catalog_item_id] = {
                "id":        f"epic_{catalog_item_id}",
                "raw_id":    catalog_item_id,
                "name":      title,
                "platform":  "epic",
                "source":    "oauth",
                "app_name":  _app_name_for(item),
                "namespace": namespace,
            }

    return sorted(games.values(), key=lambda g: g["name"].lower())
