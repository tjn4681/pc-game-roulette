"""
Genre bucketing for the optional auto-collections feature.

Pure logic — no I/O.  Turns a genre cache ({appid_str: [genre, ...]}) plus the
user's library into spinnable genre collection cards, keeping only a curated set
of real game genres (the noise — Indie, Early Access, content descriptors,
software "genres" — is intentionally excluded) and hiding buckets that are too
small to be worth spinning.
"""

# Curated Steam genres worth spinning within.  One game can match several, and
# it lands in every matching bucket.  This is the one knob to tweak.
CURATED_GENRES = [
    "Action", "Adventure", "RPG", "Strategy", "Simulation",
    "Sports", "Racing", "Casual", "Massively Multiplayer",
]

# Genres with fewer than this many games are hidden (avoids a wall of one-game
# cards).
MIN_BUCKET_SIZE = 2

_CANON = {g.lower(): g for g in CURATED_GENRES}


def build_genre_buckets(genres_by_appid, library_appids, min_size=MIN_BUCKET_SIZE):
    """Build genre collection cards.

    genres_by_appid : {appid_str: [genre_str, ...]} — the genre cache.
    library_appids  : iterable of appid ints — the user's library.
    Returns [{"name", "count", "appids"}] for curated genres present in the
    library with at least ``min_size`` games, ordered by the curated list.  A
    game appears in every curated genre it matches.  appids come out sorted ints.
    """
    buckets = {}  # canonical genre name -> set(appid_int)
    for appid in library_appids:
        genres = genres_by_appid.get(str(appid))
        if not genres:
            continue
        for g in genres:
            canon = _CANON.get((g or "").strip().lower())
            if canon:
                buckets.setdefault(canon, set()).add(appid)
    out = []
    for name in CURATED_GENRES:          # stable, curated order
        ids = buckets.get(name)
        if ids and len(ids) >= min_size:
            out.append({"name": name, "count": len(ids), "appids": sorted(ids)})
    return out


# Curated community tags — the "vibes" that genres miss.  {canonical: [synonyms]}.
# A game's raw SteamSpy tags are matched case-insensitively against the canonical
# name and its synonyms, and counted under the canonical name.  One knob to tweak.
CURATED_TAGS = {
    "Roguelike":      ["Rogue-like", "Rogue-lite", "Roguelite", "Action Roguelike", "Traditional Roguelike"],
    "Metroidvania":   [],
    "Soulslike":      ["Souls-like"],
    "Open World":     ["Open-World"],
    "Survival":       ["Survival Craft", "Open World Survival Craft"],
    "Horror":         ["Survival Horror", "Psychological Horror"],
    "Shooter":        ["FPS", "First-Person Shooter", "Third-Person Shooter", "Looter Shooter"],
    "Platformer":     ["2D Platformer", "3D Platformer", "Precision Platformer"],
    "Visual Novel":   [],
    "Deckbuilder":    ["Deckbuilding", "Card Battler"],
    "City Builder":   ["Base Building", "Building", "Colony Sim"],
    "Stealth":        [],
    "Tower Defense":  [],
    "Hack and Slash": ["Hack 'n' Slash"],
    "JRPG":           [],
    "Sandbox":        [],
    "Co-op":          ["Online Co-Op", "Local Co-Op", "Co-operative"],
}

# lowercased synonym/canonical -> canonical
_TAG_CANON = {}
for _canon, _syns in CURATED_TAGS.items():
    _TAG_CANON[_canon.lower()] = _canon
    for _s in _syns:
        _TAG_CANON[_s.lower()] = _canon


def build_tag_buckets(tags_by_appid, library_appids, min_size=MIN_BUCKET_SIZE):
    """Build curated community-tag collection cards.

    tags_by_appid : {appid_str: [tag_str, ...]} — the tag cache (already the
                    game's top-N tags).
    library_appids : iterable of appid ints — the user's library.
    Returns [{"name","count","appids"}] for curated tags present in the library
    with at least ``min_size`` games, ordered by the curated map.  Raw tags are
    canonicalized (case-insensitive) through CURATED_TAGS; a game lands in every
    distinct curated tag it matches.  Same card shape as build_genre_buckets.
    """
    buckets = {}  # canonical tag -> set(appid_int)
    for appid in library_appids:
        tags = tags_by_appid.get(str(appid))
        if not tags:
            continue
        for t in tags:
            canon = _TAG_CANON.get((t or "").strip().lower())
            if canon:
                buckets.setdefault(canon, set()).add(appid)
    out = []
    for name in CURATED_TAGS:            # stable, curated order
        ids = buckets.get(name)
        if ids and len(ids) >= min_size:
            out.append({"name": name, "count": len(ids), "appids": sorted(ids)})
    return out
