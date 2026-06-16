"""
Duplicate detection across and within platforms.

Two kinds of duplicates:
  * cross-platform — the same game owned on Steam *and* GOG/Epic; hide the copy
    on the lower-priority launcher.
  * same-platform editions — "Mass Effect" vs "Mass Effect Legendary Edition";
    hide whichever side the user's edition preference disfavors.

Both work by bucketing games on their normalized title (see game_titles), so
all the fuzzy-matching smarts live in one place.
"""

from pcgr.titles import is_enhanced_edition, normalize_title


def find_cross_platform_duplicates(games_by_platform, priority):
    """For a mapping of platform -> [game dicts], return a dict mapping each
    platform to a set of game IDs that should be hidden because the same game
    exists on a higher-priority platform.

    `priority` is an ordered list like ['steam', 'gog', 'epic'] — earlier =
    more preferred.

    Algorithm:
      1. Bucket every game by its normalized title.
      2. For each bucket containing games from 2+ platforms, find the highest-
         priority platform present in the bucket.
      3. Mark every game in the bucket that's on a DIFFERENT platform as
         excluded.  (Games within the same platform but with matching titles —
         e.g. two different DOOMs on Steam — are left alone.)
    """
    excludes = {p: set() for p in games_by_platform}
    buckets = {}   # normalized title -> [(platform, game), ...]

    for platform, games in games_by_platform.items():
        for g in games:
            norm = normalize_title(g.get("name", ""))
            if not norm:
                continue
            buckets.setdefault(norm, []).append((platform, g))

    # Build a fast lookup of platform priority
    rank = {p: i for i, p in enumerate(priority)}

    for norm, entries in buckets.items():
        platforms_in_bucket = {p for p, _ in entries}
        if len(platforms_in_bucket) < 2:
            continue   # not a cross-platform duplicate
        # Pick the winning platform: lowest rank index among those present
        winner = min(platforms_in_bucket,
                     key=lambda p: rank.get(p, 99))
        for platform, game in entries:
            if platform == winner:
                continue
            gid = game.get("id")
            if gid:
                excludes[platform].add(gid)

    return {p: list(v) for p, v in excludes.items()}


def find_same_platform_edition_dupes(games, preference):
    """For a list of games (all on the same platform), return a set of game IDs
    that should be hidden because a same-game-different-edition variant exists
    and the user has expressed a preference.

    preference: 'enhanced' (hide originals) | 'original' (hide enhanced) | 'both' (hide nothing)

    Algorithm:
      1. Bucket games by normalized title.  Editions like "Mass Effect" and
         "Mass Effect Legendary Edition" land in the same bucket because
         normalize_title() strips the suffix.
      2. For each bucket with 2+ entries, classify each game as enhanced or
         original using is_enhanced_edition().
      3. Hide whichever side the preference says to hide — but only when BOTH
         sides exist in the bucket (otherwise there's no choice to make).
    """
    if preference not in ("enhanced", "original") or not games:
        return set()

    buckets = {}
    for g in games:
        norm = normalize_title(g.get("name", ""))
        if not norm:
            continue
        buckets.setdefault(norm, []).append(g)

    hidden = set()
    for norm, entries in buckets.items():
        if len(entries) < 2:
            continue
        enhanced = [g for g in entries if is_enhanced_edition(g.get("name", ""))]
        original = [g for g in entries if not is_enhanced_edition(g.get("name", ""))]
        if not enhanced or not original:
            continue   # only one side present — nothing to choose between
        losers = original if preference == "enhanced" else enhanced
        for g in losers:
            gid = g.get("id")
            if gid:
                hidden.add(gid)
    return hidden
