"""
Steam Collection Analyzer
==========================
Fetches tags for all owned games (via SteamSpy), cross-references with your
existing collections, and generates a recommendations file for re-categorization.

Usage:  python steam_analyzer.py
Output: steam_recommendations.txt  (review, delete lines you disagree with)
        steam_tag_cache.json        (cached tag data, delete to re-fetch)

Requires: requests (pip install requests)
Tag fetch takes ~30-40 minutes on first run. Cached after that.
"""

import json
import os
import sys
import time
import requests

# ── CONFIG ──────────────────────────────────────────────────────────────────
API_KEY = "A40AB3E79C7CD5A110F4DA2A417CF044"
STEAM_ID = "76561198003181985"
COLLECTIONS_JSON = r"C:\Program Files (x86)\Steam\userdata\42916257\config\cloudstorage\cloud-storage-namespace-1.json"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(SCRIPT_DIR, "steam_tag_cache.json")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "steam_recommendations.txt")

# Only consider a SteamSpy tag if it's in the top N by vote count
TOP_N_TAGS = 15
# Seconds between SteamSpy requests (be nice to their servers)
REQUEST_DELAY = 0.75
# Save cache every N games
CACHE_SAVE_INTERVAL = 100
# ────────────────────────────────────────────────────────────────────────────

# Collections being deleted
DELETING = {
    "RPG", "Action", "Adventure", "Side-scroller",
    "First Person", "Third-Person", "Top Down",
}

# Non-genre tags (having ONLY these after deletion = needs a genre tag)
NON_GENRE = {
    "Software", "Server", "Retro", "Retro Pixel", "Retro Polygonal",
    "Retro Collection", "Multiplayer-centric", "Live Service", "Adult",
    "Isometric", "Quirky", "Cozy/Cute", "Funny", "Art-House",
    "Anomaly Hunt", "Choices Matter", "Dating/Romance", "Relaxing",
    "Hidden",
}

# Tags to completely ignore (Adult content already vetted manually)
IGNORED_TAGS = {
    "Sexual Content", "Nudity", "NSFW", "Hentai",
    "Adult Only", "Mature", "Erotic",
}

# RPG subgenre indicators (for Open World dedup)
RPG_SUBGENRES = {
    "Action RPG", "CRPG", "JRPG", "JRPG - 2D", "First Person RPG",
    "Souls-Like", "Dungeon Crawler", "Tactics RPG", "RPG - Sidescrolling",
    "Tabletop RPG", "Mystery Dungeon", "Creature Collector",
}

# SteamSpy tags that indicate "designed to scare" (for Horror pruning)
HORROR_TAGS = {
    "Horror", "Survival Horror", "Psychological Horror",
    "Lovecraftian", "Gore", "Dark", "Zombies",
}
# Strong horror — if these are in the game's top tags, definitely keep
STRONG_HORROR_TAGS = {"Horror", "Survival Horror", "Psychological Horror"}
# Anti-horror — if these dominate AND no strong horror tag, flag for removal
ANTI_HORROR_TAGS = {
    "Cute", "Colorful", "Casual", "Relaxing", "Family Friendly",
    "Comedy", "Funny", "Cartoon",
}

# SteamSpy tags → Tommy's collections (for underpopulated collection scan)
TAG_COLLECTION_MAP = {
    # Exploration
    "Exploration": "Exploration",
    # Funny
    "Comedy": "Funny",
    "Funny": "Funny",
    "Parody": "Funny",
    "Satire": "Funny",
    # Romance/Dating
    "Dating Sim": "Romance/Dating",
    "Romance": "Romance/Dating",
    # Space Shooter / Shmup
    # NOTE: "Shoot 'Em Up" excluded — too broad, catches FPS games like Crysis
    # Art-House
    "Abstract": "Art-House",
    "Experimental": "Art-House",
    # NOTE: "Surreal" excluded — refers to supernatural themes, not art style
    # Bullet Hell (catch missing)
    "Bullet Hell": "Bullet Hell",
    # Creature Collector
    "Creature Collector": "Creature Collector",
    # Time
    "Time Travel": "Timeloop/Time Travel",
    "Time Loop": "Timeloop/Time Travel",
    # Mecha
    "Mecha": "Mecha",
    # FMV
    "FMV": "FMV",
    # Walking Sim
    "Walking Simulator": "Walking Simulator",
    # Farming
    "Farming Sim": "Farming Sim",
    "Farming": "Farming Sim",
    # Musou
    "Musou": "Musou",
    # Typing
    "Typing": "Typing Games",
    # Card
    "Card Game": "Card Games/Card Based Games",
    "Deckbuilding": "Card Games/Card Based Games",
    "Card Battler": "Card Games/Card Based Games",
    # Tower Defense
    "Tower Defense": "Tower Defense",
}

# Collections we consider "underpopulated" (worth scanning for additions)
UNDERPOPULATED_THRESHOLD = 60  # scan collections with fewer games than this


def load_collections(path):
    """Load collection name -> set of appids from Steam's JSON."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    colls = {}
    for entry in data:
        key = entry[0]
        # Load both user collections AND the hidden collection
        if not (key.startswith("user-collections.uc-") or key == "user-collections.hidden"):
            continue
        try:
            value = json.loads(entry[1]["value"])
            name = value.get("name", key.replace("user-collections.", ""))
            # Normalize all appids to int for consistent type matching
            raw_added = value.get("added", [])
            added = set()
            for a in raw_added:
                try:
                    added.add(int(a))
                except (ValueError, TypeError):
                    added.add(a)
            if name:
                colls[name] = added
        except (KeyError, json.JSONDecodeError):
            continue
    return colls


def fetch_owned_games():
    """Fetch all owned game names from Steam API."""
    print("Fetching owned games from Steam API...")
    url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
    params = {
        "key": API_KEY, "steamid": STEAM_ID,
        "include_appinfo": 1, "include_played_free_games": 1,
        "format": "json",
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    games = resp.json().get("response", {}).get("games", [])
    name_map = {g["appid"]: g.get("name", f"Unknown ({g['appid']})") for g in games}
    print(f"  Found {len(name_map)} games.\n")
    return name_map


def load_tag_cache():
    """Load cached SteamSpy tag data if it exists."""
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Keys might be strings from JSON serialization
        return {int(k): v for k, v in raw.items()}
    return {}


def save_tag_cache(cache):
    """Save tag cache to disk."""
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def fetch_tags_for_game(appid):
    """Fetch tags from SteamSpy for a single game. Returns dict of tag->votes."""
    url = f"https://steamspy.com/api.php?request=appdetails&appid={appid}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return {}
        data = resp.json()
        tags = data.get("tags", {})
        if isinstance(tags, dict):
            return tags
        return {}
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        return {}


def fetch_all_tags(appids, cache):
    """Fetch tags for all games, using cache where available."""
    to_fetch = [aid for aid in appids if aid not in cache]
    if not to_fetch:
        print(f"All {len(appids)} games already cached.\n")
        return cache

    print(f"Fetching tags for {len(to_fetch)} games from SteamSpy...")
    print(f"  ({len(appids) - len(to_fetch)} already cached)")
    print(f"  Estimated time: ~{len(to_fetch) * REQUEST_DELAY / 60:.0f} minutes")
    print()

    for i, appid in enumerate(to_fetch):
        tags = fetch_tags_for_game(appid)
        cache[appid] = tags

        # Progress
        if (i + 1) % 50 == 0 or (i + 1) == len(to_fetch):
            pct = (i + 1) * 100 // len(to_fetch)
            print(f"  [{pct:3d}%] {i + 1}/{len(to_fetch)} games fetched")

        # Save cache periodically
        if (i + 1) % CACHE_SAVE_INTERVAL == 0:
            save_tag_cache(cache)

        time.sleep(REQUEST_DELAY)

    save_tag_cache(cache)
    print(f"\n  Tag fetch complete. Cache saved to {CACHE_FILE}\n")
    return cache


def get_top_tags(tags_dict, n=TOP_N_TAGS):
    """Return the top N tags by vote count as a set of tag names.
    Filters out adult/NSFW tags (already vetted manually)."""
    if not tags_dict:
        return set()
    filtered = {t: v for t, v in tags_dict.items() if t not in IGNORED_TAGS}
    sorted_tags = sorted(filtered.items(), key=lambda x: x[1], reverse=True)
    return {tag for tag, _ in sorted_tags[:n]}


def get_top_tags_list(tags_dict, n=TOP_N_TAGS):
    """Return top N tags as ordered list of (tag, votes) tuples.
    Filters out adult/NSFW tags (already vetted manually)."""
    if not tags_dict:
        return []
    filtered = {t: v for t, v in tags_dict.items() if t not in IGNORED_TAGS}
    return sorted(filtered.items(), key=lambda x: x[1], reverse=True)[:n]


# ── ANALYSIS RULES ──────────────────────────────────────────────────────────

def analyze_orphans(collections, appid_to_colls, name_map, tag_cache):
    """Find games that would lose all genre collections after deletions."""
    changes = []

    for appid, colls in appid_to_colls.items():
        # Skip games not in any DELETING collection — they aren't losing anything
        if not (colls & DELETING):
            continue

        # Skip games not in the owned games list (DLC, servers, removed games)
        if appid not in name_map:
            continue

        remaining = colls - DELETING
        genre_remaining = remaining - NON_GENRE

        if len(remaining) == 0 or len(genre_remaining) == 0:
            name = name_map.get(appid, f"Unknown ({appid})")
            top_tags = get_top_tags(tag_cache.get(appid, {}))
            was_in = sorted(colls & DELETING)

            # Try to suggest a collection based on tags
            suggestions = suggest_collections(appid, top_tags, collections, appid_to_colls)
            reason_parts = [f"Was in: {', '.join(was_in)}"]
            if top_tags:
                reason_parts.append(f"Steam tags: {', '.join(list(top_tags)[:6])}")

            if suggestions:
                for coll_name in suggestions:
                    changes.append(("ADD", appid, name, coll_name,
                                    "HIGH", " | ".join(reason_parts)))
            else:
                # Default to Action-Adventure if was in both
                if "Action" in colls and "Adventure" in colls:
                    changes.append(("ADD", appid, name, "Action-Adventure",
                                    "MEDIUM", " | ".join(reason_parts)))
                else:
                    changes.append(("ADD", appid, name, "NEEDS_MANUAL_REVIEW",
                                    "LOW", " | ".join(reason_parts)))

    return changes


def suggest_collections(appid, top_tags, collections, appid_to_colls):
    """Given a game's Steam tags, suggest which existing collections it belongs in."""
    suggestions = []

    # Map common Steam tags to Tommy's collection names
    tag_map = {
        "Metroidvania": "Metroidvania",
        "Souls-like": "Souls-Like",
        "Roguelike": "Roguelike",
        "Roguelite": "Roguelike",
        "Point & Click": "Point & Click",
        "JRPG": "JRPG",
        "Turn-Based Strategy": "Turn Based Strategy",
        "Real-Time Strategy": "RTS",
        "RTS": "RTS",
        "Action RPG": "Action RPG",
        "Hack and Slash": "Action RPG",
        "CRPG": "CRPG",
        "City Builder": "City Builder/Base Builder",
        "Base Building": "City Builder/Base Builder",
        "FPS": "FPS",
        "Third-Person Shooter": "Third-Person Shooter",
        "Tactical RPG": "Tactics RPG",
        "Dungeon Crawler": "Dungeon Crawler",
        "Survival": "Survival",
        "Stealth": "Stealth",
        "Puzzle": "Puzzle",
        "Horror": "Horror",
        "Survival Horror": "Horror",
        "Platformer": "Platformer - 2D",
        "3D Platformer": "Platformer - 3D",
        "2D Platformer": "Platformer - 2D",
        "Beat 'em up": "Beat'em Up",
        "Arcade": "Arcade",
        "Racing": "Racing",
        "Simulation": "Simulation",
        "Visual Novel": "Visual Novel - Serious",
        "Card Game": "Card Games/Card Based Games",
        "Tower Defense": "Tower Defense",
        "Management": "Management",
        "Walking Simulator": "Walking Simulator",
        "Open World": "Open World",
        "Fighting": "Fighting - 2D",
    }

    for tag in top_tags:
        if tag in tag_map:
            coll_name = tag_map[tag]
            if coll_name in collections and appid not in collections.get(coll_name, set()):
                suggestions.append(coll_name)

    return suggestions[:3]  # Cap at 3 suggestions


def analyze_ow_dedup(collections, appid_to_colls, name_map, tag_cache):
    """Determine which games to keep in Open World vs Open World RPG."""
    changes = []
    ow = collections.get("Open World", set())
    owrpg = collections.get("Open World RPG", set())
    both = ow & owrpg

    for appid in sorted(both, key=lambda x: name_map.get(x, "").lower()):
        name = name_map.get(appid, f"Unknown ({appid})")
        current_colls = appid_to_colls.get(appid, set())
        top_tags = get_top_tags(tag_cache.get(appid, {}))

        # Check for RPG indicators
        has_rpg_collection = bool(current_colls & RPG_SUBGENRES)
        has_rpg_tag = bool(top_tags & {"RPG", "Action RPG", "JRPG", "CRPG",
                                        "Open World", "Souls-like"})

        rpg_tags_in_top = top_tags & {"RPG", "Action RPG", "JRPG", "CRPG",
                                       "Souls-like", "Character Customization",
                                       "Skill Tree"}

        # Decision logic
        if has_rpg_collection or "RPG" in top_tags:
            confidence = "HIGH"
            reason = f"Has RPG collection/tag → keep in OW RPG only"
            if rpg_tags_in_top:
                reason += f" (RPG tags: {', '.join(rpg_tags_in_top)})"
            changes.append(("REMOVE", appid, name, "Open World",
                            confidence, reason))
        elif "RPG" not in top_tags and not has_rpg_collection:
            # No RPG indicators at all — likely pure open world
            top_list = [t for t, _ in get_top_tags_list(tag_cache.get(appid, {}), 5)]
            confidence = "MEDIUM"
            reason = f"No RPG tags/collections → keep in Open World only"
            reason += f" (Top tags: {', '.join(top_list)})"
            changes.append(("REMOVE", appid, name, "Open World RPG",
                            confidence, reason))
        else:
            top_list = [t for t, _ in get_top_tags_list(tag_cache.get(appid, {}), 5)]
            changes.append(("REMOVE", appid, name, "NEEDS_DECISION_OW",
                            "LOW", f"Ambiguous. Tags: {', '.join(top_list)}"))

    return changes


def analyze_horror(collections, appid_to_colls, name_map, tag_cache):
    """Flag Horror collection games that aren't designed to scare."""
    changes = []
    horror = collections.get("Horror", set())

    for appid in sorted(horror, key=lambda x: name_map.get(x, "").lower()):
        name = name_map.get(appid, f"Unknown ({appid})")
        tags = tag_cache.get(appid, {})
        top_tags = get_top_tags(tags)

        has_strong_horror = bool(top_tags & STRONG_HORROR_TAGS)
        has_any_horror = bool(top_tags & HORROR_TAGS)
        has_anti_horror = bool(top_tags & ANTI_HORROR_TAGS)

        if has_strong_horror:
            continue  # Definitely horror, skip

        top_list = [t for t, _ in get_top_tags_list(tags, 8)]
        tag_str = ", ".join(top_list) if top_list else "no tags"

        if not has_any_horror:
            # No horror-related tags at all in top 15
            confidence = "HIGH" if has_anti_horror else "MEDIUM"
            reason = f"No horror tags in top {TOP_N_TAGS}. Tags: {tag_str}"
            changes.append(("REMOVE", appid, name, "Horror",
                            confidence, reason))
        elif has_any_horror and has_anti_horror:
            # Has weak horror signal AND anti-horror signals
            reason = f"Weak horror signal + anti-horror tags. Tags: {tag_str}"
            changes.append(("REMOVE", appid, name, "Horror",
                            "LOW", reason))

    return changes


def analyze_point_click(collections, appid_to_colls, name_map, tag_cache):
    """Identify Point & Click games that should move to P&C - Exploration."""
    changes = []
    pc = collections.get("Point & Click", set())

    for appid in sorted(pc, key=lambda x: name_map.get(x, "").lower()):
        name = name_map.get(appid, f"Unknown ({appid})")
        tags = tag_cache.get(appid, {})
        top_tags = get_top_tags(tags)

        # First-person perspective games in Point & Click → P&C Exploration
        has_first_person = "First-Person" in top_tags
        has_hidden_object = "Hidden Object" in top_tags
        has_escape_room = "Escape Room" in top_tags

        if has_first_person:
            top_list = [t for t, _ in get_top_tags_list(tags, 6)]
            confidence = "HIGH"
            reason = f"First-Person tag found. Tags: {', '.join(top_list)}"
            changes.append(("MOVE", appid, name,
                            "Point & Click -> Point & Click - Exploration",
                            confidence, reason))
        elif has_hidden_object:
            top_list = [t for t, _ in get_top_tags_list(tags, 6)]
            changes.append(("MOVE", appid, name,
                            "Point & Click -> Point & Click - Exploration",
                            "MEDIUM",
                            f"Hidden Object game. Tags: {', '.join(top_list)}"))

    return changes


def analyze_action_adventure(collections, appid_to_colls, name_map, tag_cache):
    """Find candidates for the new Action-Adventure collection."""
    changes = []
    action = collections.get("Action", set())
    adventure = collections.get("Adventure", set())
    both = action & adventure

    # Existing subgenres that already cover action or adventure games
    action_subs = {
        "Action RPG", "Beat'em Up", "Boomer Shooter", "FPS", "Metroidvania",
        "Musou", "Souls-Like", "Third-Person Shooter", "Shooter - 2D",
        "Military Shooter", "Survivors-Like/Auto-Battler", "Platformer - 2D",
        "Platformer - 3D", "Stealth", "Arcade", "Racing", "Driving",
        "Fighting - 2D", "Fighting - 3D", "Sports",
    }
    adventure_subs = {
        "Point & Click", "Metroidvania", "Walking Simulator",
        "Deduction/Investigation", "Mystery", "Narrative/Cinematic",
        "Escape Room", "Open World", "Open World RPG", "Exploration",
    }
    all_subs = action_subs | adventure_subs

    for appid in sorted(both, key=lambda x: name_map.get(x, "").lower()):
        name = name_map.get(appid, f"Unknown ({appid})")
        current_colls = appid_to_colls.get(appid, set())

        # Check if game has any specific subgenre
        has_subgenre = bool(current_colls & all_subs)

        if not has_subgenre:
            top_list = [t for t, _ in get_top_tags_list(
                tag_cache.get(appid, {}), 5)]
            tag_str = ", ".join(top_list) if top_list else "no tags"
            remaining = sorted(current_colls - DELETING - NON_GENRE)
            remaining_str = ", ".join(remaining) if remaining else "none"

            confidence = "HIGH"
            reason = (f"In both Action + Adventure, no specific subgenre. "
                      f"Other colls: {remaining_str} | Tags: {tag_str}")
            changes.append(("ADD", appid, name, "Action-Adventure",
                            confidence, reason))

    # Also check for games with "Action-Adventure" Steam tag not already covered
    for appid in name_map:
        if appid in both:
            continue  # Already handled above
        top_tags = get_top_tags(tag_cache.get(appid, {}))
        current_colls = appid_to_colls.get(appid, set())

        if "Action-Adventure" in top_tags:
            has_subgenre = bool(current_colls & all_subs)
            if not has_subgenre and "Action-Adventure" not in current_colls:
                name = name_map.get(appid, f"Unknown ({appid})")
                remaining = sorted(current_colls - DELETING - NON_GENRE)
                remaining_str = ", ".join(remaining) if remaining else "none"
                changes.append(("ADD", appid, name, "Action-Adventure",
                                "MEDIUM",
                                f"Steam tag 'Action-Adventure'. Colls: {remaining_str}"))

    return changes


def analyze_underpopulated(collections, appid_to_colls, name_map, tag_cache):
    """Scan for games that should be in underpopulated collections."""
    changes = []

    # Find underpopulated collections
    underpop = {}
    for coll_name, games in collections.items():
        if len(games) < UNDERPOPULATED_THRESHOLD and coll_name not in DELETING:
            underpop[coll_name] = games

    # For each game, check if its Steam tags match any underpopulated collection
    for appid in name_map:
        tags = tag_cache.get(appid, {})
        top_tags = get_top_tags(tags)
        current_colls = appid_to_colls.get(appid, set())

        for steam_tag in top_tags:
            if steam_tag in TAG_COLLECTION_MAP:
                target_coll = TAG_COLLECTION_MAP[steam_tag]
                if (target_coll in underpop and
                        appid not in collections.get(target_coll, set())):
                    name = name_map.get(appid, f"Unknown ({appid})")

                    # Get vote count for this tag
                    votes = tags.get(steam_tag, 0)
                    confidence = "HIGH" if votes > 200 else "MEDIUM" if votes > 50 else "LOW"

                    reason = (f"Steam tag '{steam_tag}' ({votes} votes) "
                              f"→ {target_coll}")
                    changes.append(("ADD", appid, name, target_coll,
                                    confidence, reason))

    # Deduplicate (same appid + same collection = keep highest confidence)
    seen = {}
    deduped = []
    for change in changes:
        key = (change[1], change[3])  # (appid, collection)
        conf_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        if key not in seen or conf_order.get(change[4], 0) > conf_order.get(seen[key][4], 0):
            seen[key] = change
    deduped = sorted(seen.values(), key=lambda x: (x[3], x[2].lower()))

    return deduped


# ── OUTPUT ──────────────────────────────────────────────────────────────────

def write_recommendations(all_changes, output_path):
    """Write recommendations to a file that's both human-readable and parseable."""
    lines = []
    lines.append("#" + "=" * 69)
    lines.append("# STEAM COLLECTION RECOMMENDATIONS")
    lines.append("#" + "=" * 69)
    lines.append("#")
    lines.append("# Review this file. DELETE any lines you DISAGREE with.")
    lines.append("# Then run: python steam_applier.py")
    lines.append("#")
    lines.append("# Format: ACTION ||| APPID ||| GAME NAME ||| COLLECTION ||| CONFIDENCE ||| REASON")
    lines.append("# NOTE: Triple pipe ||| delimiter because some game names contain |")
    lines.append("# ACTIONS: ADD (add to collection), REMOVE (remove from collection),")
    lines.append("#          MOVE (remove from first, add to second)")
    lines.append("#")
    lines.append("# CONFIDENCE: HIGH (almost certainly correct)")
    lines.append("#             MEDIUM (probably correct, worth a glance)")
    lines.append("#             LOW (uncertain, review carefully)")
    lines.append("#")

    # Group changes by section
    sections = {
        "ORPHANS": [],
        "OPEN_WORLD_DEDUP": [],
        "POINT_CLICK_SPLIT": [],
        "ACTION_ADVENTURE": [],
        "UNDERPOPULATED": [],
    }

    for section_name, section_changes in all_changes.items():
        if not section_changes:
            continue

        lines.append("")
        lines.append("#" + "=" * 69)

        if section_name == "ORPHANS":
            lines.append("# SECTION: ORPHAN RE-TAGGING")
            lines.append("# Games that would lose all genre collections after deletions.")
        elif section_name == "OPEN_WORLD_DEDUP":
            lines.append("# SECTION: OPEN WORLD DEDUP")
            lines.append("# Games in both Open World and Open World RPG.")
            lines.append("# Rule: Can you build meaningfully different characters?")
            lines.append("#   Yes → Open World RPG. No → Open World.")
        elif section_name == "POINT_CLICK_SPLIT":
            lines.append("# SECTION: POINT & CLICK → P&C EXPLORATION")
            lines.append("# First-person/room-exploration games to move out of Point & Click.")
        elif section_name == "ACTION_ADVENTURE":
            lines.append("# SECTION: ACTION-ADVENTURE CANDIDATES")
            lines.append("# Games that belong in the new Action-Adventure collection.")
        elif section_name == "UNDERPOPULATED":
            lines.append("# SECTION: UNDERPOPULATED COLLECTION SUGGESTIONS")
            lines.append("# Games that Steam tags suggest belong in small collections.")

        lines.append("#" + "=" * 69)
        lines.append("")

        # Sort by confidence (HIGH first), then by game name
        conf_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        section_changes.sort(key=lambda x: (conf_order.get(x[4], 3), x[2].lower()))

        for action, appid, name, collection, confidence, reason in section_changes:
            lines.append(
                f"{action} ||| {appid} ||| {name} ||| {collection} ||| {confidence} ||| {reason}"
            )

    # Summary
    lines.append("")
    lines.append("#" + "=" * 69)
    lines.append("# SUMMARY")
    lines.append("#" + "=" * 69)
    total = sum(len(v) for v in all_changes.values())
    lines.append(f"# Total recommendations: {total}")
    for section, changes in all_changes.items():
        if changes:
            high = sum(1 for c in changes if c[4] == "HIGH")
            med = sum(1 for c in changes if c[4] == "MEDIUM")
            low = sum(1 for c in changes if c[4] == "LOW")
            lines.append(f"#   {section}: {len(changes)} ({high} HIGH, {med} MED, {low} LOW)")
    lines.append("#")
    lines.append("# After review, run: python steam_applier.py")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ── MAIN ────────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(COLLECTIONS_JSON):
        print(f"ERROR: Collections file not found:\n  {COLLECTIONS_JSON}")
        print("Update COLLECTIONS_JSON at the top of this script.")
        return

    # Load collections
    collections = load_collections(COLLECTIONS_JSON)
    print(f"Loaded {len(collections)} collections.\n")

    # Build reverse map
    appid_to_colls = {}
    for name, apps in collections.items():
        for a in apps:
            appid_to_colls.setdefault(a, set()).add(name)

    # Fetch owned games
    name_map = fetch_owned_games()

    # Fetch tags (with caching)
    all_appids = list(name_map.keys())
    cache = load_tag_cache()
    cache = fetch_all_tags(all_appids, cache)

    # ── Run analysis ────────────────────────────────────────────────────
    print("Running analysis...\n")

    all_changes = {}

    print("  [1/5] Finding orphans...")
    all_changes["ORPHANS"] = analyze_orphans(
        collections, appid_to_colls, name_map, cache)
    print(f"         → {len(all_changes['ORPHANS'])} recommendations")

    print("  [2/5] Open World dedup...")
    all_changes["OPEN_WORLD_DEDUP"] = analyze_ow_dedup(
        collections, appid_to_colls, name_map, cache)
    print(f"         → {len(all_changes['OPEN_WORLD_DEDUP'])} recommendations")

    print("  [3/5] Point & Click split...")
    all_changes["POINT_CLICK_SPLIT"] = analyze_point_click(
        collections, appid_to_colls, name_map, cache)
    print(f"         → {len(all_changes['POINT_CLICK_SPLIT'])} recommendations")

    print("  [4/5] Action-Adventure candidates...")
    all_changes["ACTION_ADVENTURE"] = analyze_action_adventure(
        collections, appid_to_colls, name_map, cache)
    print(f"         → {len(all_changes['ACTION_ADVENTURE'])} recommendations")

    print("  [5/5] Underpopulated collection scan...")
    all_changes["UNDERPOPULATED"] = analyze_underpopulated(
        collections, appid_to_colls, name_map, cache)
    print(f"         → {len(all_changes['UNDERPOPULATED'])} recommendations")

    # ── Post-processing exclusion filters ───────────────────────────────
    print("\n  Applying exclusion filters...")
    total_before = sum(len(v) for v in all_changes.values())

    # Rule 1: Exclude Adult games from ALL recommendations
    # "Adult" = manual collection, "XXX" = copy of dynamic Adult collection
    adult_games = collections.get("Adult", set()) | collections.get("XXX", set())
    print(f"    Adult exclusion (Adult + XXX): {len(adult_games)} games")

    # Rule 2: Exclude games in Survivors-Like/Auto-Battler from ALL recommendations
    survivors_games = collections.get("Survivors-Like/Auto-Battler", set())
    print(f"    Survivors exclusion list:      {len(survivors_games)} games")

    # Combined global exclusion set
    global_exclude = adult_games | survivors_games
    print(f"    Total global exclusions: {len(global_exclude)} games")

    # Normalize appids in recommendations to int for matching
    excluded_count = 0
    for section in all_changes:
        before = len(all_changes[section])
        all_changes[section] = [
            c for c in all_changes[section]
            if int(c[1]) not in global_exclude  # force int comparison
        ]
        excluded_count += before - len(all_changes[section])
    print(f"    → Excluded {excluded_count} recommendations")

    # Rule 3: Don't suggest Exploration for games in Metroidvania OR Point & Click
    metroidvania_games = collections.get("Metroidvania", set())
    pointclick_games = collections.get("Point & Click", set())
    no_exploration = metroidvania_games | pointclick_games
    for section in all_changes:
        all_changes[section] = [
            c for c in all_changes[section]
            if not (c[3] == "Exploration" and int(c[1]) in no_exploration)
        ]

    # Rule 4: Don't suggest Funny for games already in VN-Funny or VN-Romance
    vn_funny = collections.get("Visual Novel - Funny", set())
    vn_romance = collections.get("Visual Novel - Romance/Dating", set())
    vn_no_funny = vn_funny | vn_romance
    for section in all_changes:
        all_changes[section] = [
            c for c in all_changes[section]
            if not (c[3] == "Funny" and int(c[1]) in vn_no_funny)
        ]

    total_after = sum(len(v) for v in all_changes.values())
    print(f"  Total filtered out: {total_before - total_after} recommendations.")

    # Write output (back up previous file if it exists)
    if os.path.exists(OUTPUT_FILE):
        prev_file = OUTPUT_FILE.replace(".txt", "_prev.txt")
        if os.path.exists(prev_file):
            os.remove(prev_file)
        os.rename(OUTPUT_FILE, prev_file)
        print(f"\n  Previous recommendations backed up to: steam_recommendations_prev.txt")

    write_recommendations(all_changes, OUTPUT_FILE)

    total = sum(len(v) for v in all_changes.values())
    print(f"\n{'='*50}")
    print(f"Done! {total} total recommendations written to:")
    print(f"  {OUTPUT_FILE}")
    print(f"\nNext steps:")
    print(f"  1. Open steam_recommendations.txt")
    print(f"  2. Delete any lines you disagree with")
    print(f"  3. Run: python steam_applier.py")


if __name__ == "__main__":
    main()
