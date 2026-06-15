"""
Game-title normalization and matching helpers.

Pure string functions — no I/O, no globals beyond compiled regexes.  These turn
messy, platform-specific titles into a canonical form so the same game can be
recognized across Steam / GOG / Epic, and generate alternate search spellings
for HowLongToBeat lookups.

normalize_title() is the workhorse: it's memoized because a dedup pass calls it
tens of thousands of times on a small set of repeated titles.
"""

import functools
import re

# Edition / variant suffixes that should be stripped before comparison.
# Order matters: LONGER phrases are listed first so that e.g.
# "pixel remaster" is matched before the bare word "remaster".
_EDITION_PHRASES = [
    # Multi-word, edition-class suffixes
    "game of the year edition", "goty edition", "definitive edition",
    "enhanced edition", "deluxe edition", "ultimate edition",
    "complete edition", "collector's edition", "collectors edition",
    "anniversary edition", "director's cut", "directors cut",
    "special edition", "premium edition", "gold edition",
    "remastered edition", "standard edition", "platinum edition",
    "legendary edition", "pixel remaster", "game of the year",
    # Single-word suffixes
    "definitive", "remastered", "remaster", "redux", "hd", "goty",
    "anniversary", "enhanced", "legendary", "ultimate", "complete",
    "deluxe", "premium", "gold", "edition",
    # Markers that explicitly identify a "base/original" variant.  Stripping
    # these makes "Final Fantasy VI (Classic)" bucket with "Final Fantasy VI
    # Pixel Remaster" so the edition-preference filter can choose between them.
    "classic", "original",
]

# Used by edition-preference detection to classify each game as
# "enhanced"/remastered or base/original.  Matches the word anywhere in the
# title, not just as a suffix (e.g. "Pixel Remaster" can appear mid-string).
_ENHANCED_MARKER_RE = re.compile(
    r"\b(remastered?|remaster|definitive|complete|goty|game\s+of\s+the\s+year|"
    r"enhanced|legendary|ultimate|anniversary|director'?s\s+cut|redux|"
    r"pixel\s+remaster|hd)\b",
    re.IGNORECASE,
)

def is_enhanced_edition(title):
    """Heuristic: does this title look like an enhanced/remastered/GOTY edition?
    Used to choose between same-game variants on the same platform."""
    return bool(_ENHANCED_MARKER_RE.search(title or ""))
_TRADEMARK_CHARS = re.compile(r"[™®©℠]")  # ™ ® © ℠
_PUNCT = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")

# Roman → Arabic normalization for cross-platform dedup.
# Bare "i" is intentionally excluded to avoid treating the English pronoun
# as the numeral 1 (e.g. "I Have No Mouth And I Must Scream").
# Longest tokens listed first so the alternation is greedy-safe.
_NORMALIZE_ROMAN_RE = re.compile(
    r"\b(xx|xix|xviii|xvii|xvi|xv|xiv|xiii|xii|xi|ix|viii|vii|vi|iv|iii|ii|x|v)\b",
    re.IGNORECASE,
)
_ROMAN_TO_ARABIC_LOWER = {
    "ii": "2",  "iii": "3",  "iv": "4",  "v": "5",
    "vi": "6",  "vii": "7",  "viii": "8", "ix": "9",
    "x": "10", "xi": "11", "xii": "12", "xiii": "13",
    "xiv": "14", "xv": "15", "xvi": "16", "xvii": "17",
    "xviii": "18", "xix": "19", "xx": "20",
}


@functools.lru_cache(maxsize=50000)
def normalize_title(title):
    """Reduce a game title to a fuzzy-comparable canonical form.

    Cached: it's a pure function called repeatedly on the same titles across a
    dedup pass (bucketing, the Steam name set, the cross-platform candidate
    scan), so memoizing turns those repeats into instant lookups.

    Examples:
      "Batman: Arkham City"                          -> "batman arkham city"
      "Batman Arkham City Game of the Year Edition" -> "batman arkham city"
      "DOOM (1993)"                                   -> "doom 1993"
      "The Witcher 3: Wild Hunt - Complete Edition"  -> "witcher 3 wild hunt"
    """
    if not title:
        return ""
    s = title.lower()
    s = _TRADEMARK_CHARS.sub("", s)
    s = s.replace("&", "and")
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()

    # Strip edition phrases — must be done after punctuation normalization
    # so "Game-of-the-Year" matches.  Repeat until stable to handle stacked
    # suffixes like "Definitive Edition - GOTY".
    changed = True
    while changed:
        changed = False
        for phrase in _EDITION_PHRASES:
            if s.endswith(" " + phrase):
                s = s[: -(len(phrase) + 1)].strip()
                changed = True
            elif s == phrase:
                s = ""
                changed = True

    # Strip leading article
    if s.startswith("the "):
        s = s[4:]

    # Normalise Roman numerals → Arabic so platform mismatches don't break
    # dedup: "Breath of Fire IV" (Steam) == "Breath of Fire 4" (GOG Galaxy).
    s = _NORMALIZE_ROMAN_RE.sub(
        lambda m: _ROMAN_TO_ARABIC_LOWER.get(m.group().lower(), m.group()), s
    )
    return s


# ── HowLongToBeat numeral / ampersand search variants ────────────────────────
#
# Many game titles differ between launcher databases and HLTB only in how they
# represent series numbers (Arabic vs Roman) or conjunctions (& vs "and").
# "Might & Magic 6" on GOG Galaxy → HLTB stores it as "Might & Magic VI".
# We generate the alternate forms and try them as fallback search terms.

_ARABIC_TO_ROMAN = {
    '1': 'I',  '2': 'II',   '3': 'III', '4': 'IV',  '5': 'V',
    '6': 'VI', '7': 'VII',  '8': 'VIII','9': 'IX',  '10': 'X',
    '11': 'XI','12': 'XII', '13': 'XIII','14': 'XIV','15': 'XV',
    '16': 'XVI','17': 'XVII','18': 'XVIII','19': 'XIX','20': 'XX',
}
_ROMAN_TO_ARABIC = {v: k for k, v in _ARABIC_TO_ROMAN.items()}

# Matches standalone Arabic numerals 1–20 (word boundaries on both sides)
_STANDALONE_ARABIC_RE = re.compile(r'\b(20|1[0-9]|[1-9])\b')
# Matches standalone Roman numerals I–XX longest-match first, case-insensitive
_STANDALONE_ROMAN_RE  = re.compile(
    r'\b(XX|XIX|XVIII|XVII|XVI|XV|XIV|XIII|XII|XI|IX|VIII|VII|VI|IV|III|II|X|V|I)\b',
    re.IGNORECASE,
)


def _arabic_to_roman(s):
    """Replace standalone Arabic numerals 1–20 with Roman equivalents."""
    return _STANDALONE_ARABIC_RE.sub(
        lambda m: _ARABIC_TO_ROMAN.get(m.group(), m.group()), s
    )


def _roman_to_arabic(s):
    """Replace standalone Roman numerals I–XX with Arabic equivalents."""
    return _STANDALONE_ROMAN_RE.sub(
        lambda m: _ROMAN_TO_ARABIC.get(m.group().upper(), m.group()), s
    )


def _hltb_search_variants(clean_name, subtitle_re):
    """Build an ordered list of HLTB search terms to try for *clean_name*.

    Priority:
      1. Full name as-is
      2. Full name Arabic → Roman   ("Might & Magic 6"  → "Might & Magic VI")
      3. Full name Roman  → Arabic  (reverse case)
      4. Full name & → "and"        ("Might & Magic VI" → "Might and Magic VI")
      5. Combinations of numerals + ampersand
      6. Short title (subtitle stripped) and its variants

    Returns a deduplicated list preserving this priority order.
    """
    terms: list[str] = []
    seen:  set[str]  = set()

    def _add(t: str) -> None:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            terms.append(t)

    # Full-name variants
    _add(clean_name)
    roman_name  = _arabic_to_roman(clean_name)
    arabic_name = _roman_to_arabic(clean_name)
    _add(roman_name)
    _add(arabic_name)
    # & ↔ "and" swaps on each numeral variant
    for base in (clean_name, roman_name, arabic_name):
        _add(base.replace(' & ', ' and '))
        _add(re.sub(r'\band\b', '&', base))

    # Short-title variants (everything before the first subtitle separator)
    short = subtitle_re.split(clean_name)[0].strip()
    if short and short != clean_name:
        _add(short)
        roman_short  = _arabic_to_roman(short)
        arabic_short = _roman_to_arabic(short)
        _add(roman_short)
        _add(arabic_short)
        for base in (short, roman_short, arabic_short):
            _add(base.replace(' & ', ' and '))
            _add(re.sub(r'\band\b', '&', base))

    return terms
