# Curated tag collections — design

## Context
The auto-collections feature (shipped) buckets a user's Steam library by Steam
**genre** — but genres are coarse (~9 broad buckets: Action, RPG, Strategy…).
The "vibe" categories players actually think in — *Roguelike, Metroidvania,
Soulslike, Open World* — live in Steam's **community tags**, not the genres
field. This adds a curated set of those tags as additional spinnable
auto-collection cards, layered onto the existing feature.

This is the "Phase D" enrichment noted in the original auto-collections spec
(`2026-06-19-auto-collections-by-genre-design.md`). It reuses that feature's
opt-in, cache+warmer, progressive-fill, and card-rendering machinery — it is a
parallel *track*, not a new subsystem.

## Hard constraints (inherited)
- **App-internal only.** Never writes to or modifies Steam's data files.
- **Opt-in, Steam-only (Phase A).** Same toggle, same scope as genres.

## Requirements

### Behavior
- The existing **"Auto-organize my library by genre"** toggle now also produces
  **curated tag cards** alongside the genre cards. **No new toggle** — enabling
  auto-collections yields genre buckets **and** tag buckets, genres first then
  tags, all with the same "auto" badge, all normal spinnable collection cards.
- A game appears in **every** genre and **every** curated tag it strongly
  matches (multi-membership; a game can be in *Action* and *Roguelike*).
- Tags fill in **progressively** on the same background warm and persist in a
  cache, exactly like genres.

### Data source
- **SteamSpy** is the only source with community tags. Its `appdetails`
  response returns `tags` as a vote-ordered `{tag: votes}` map (and `genre`,
  which we don't use here — genres keep their existing Steam-appdetails source).
- We store each game's **top ~15 tags** (by vote). Storing top-N *is* the
  "strongly applied" threshold — a game only counts toward a tag bucket when
  that tag is prominent on it (not a handful of stray votes). We do **not**
  store vote counts.

### Curation
- `CURATED_TAGS`: a `{canonical_name: [synonym, …]}` map. Raw SteamSpy tags are
  matched case-insensitively against the synonyms (and the canonical name) and
  counted under the canonical name. One constant, tweakable.
- **Default set** (~17 canonical tags; synonyms in parentheses):
  - **Roguelike** ← Roguelike, Rogue-like, Rogue-lite, Roguelite, Action Roguelike, Traditional Roguelike
  - **Metroidvania** ← Metroidvania
  - **Soulslike** ← Soulslike, Souls-like
  - **Open World** ← Open World, Open-World
  - **Survival** ← Survival, Survival Craft, Open World Survival Craft
  - **Horror** ← Horror, Survival Horror, Psychological Horror
  - **Shooter** ← Shooter, FPS, First-Person Shooter, Third-Person Shooter, Looter Shooter
  - **Platformer** ← Platformer, 2D Platformer, 3D Platformer, Precision Platformer
  - **Visual Novel** ← Visual Novel
  - **Deckbuilder** ← Deckbuilder, Deckbuilding, Card Battler
  - **City Builder** ← City Builder, Base Building, Building, Colony Sim
  - **Stealth** ← Stealth
  - **Tower Defense** ← Tower Defense
  - **Hack and Slash** ← Hack and Slash, Hack 'n' Slash
  - **JRPG** ← JRPG
  - **Sandbox** ← Sandbox
  - **Co-op** ← Co-op, Online Co-Op, Local Co-Op, Co-operative

### Presentation
- Tag cards render **identically** to genre cards (same "auto" badge), mixed in
  the same grid, genres first then tags. The bucket builder may flag a card's
  `kind` ("genre"/"tag") for future use, but the frontend renders both the same
  way (Section: no frontend change beyond what genres already do).

### Non-goals
- No second toggle, no per-tag selection UI, no tag-vs-genre visual distinction.
- No GOG/Epic tags (Phase B, separate spec).
- Not changing the genre source.

## Architecture (in the `pcgr` package)
Mirrors the genre track piece-for-piece.

- **`pcgr/sources/store.py`** (parallel to the genre cache):
  - `fetch_tags(appid)` → `list[str]` of the game's top ~15 SteamSpy tag names
    (vote-ordered), or `None` on failure (not cached), `[]` for "no tags"
    (cached). Uses the SteamSpy `appdetails` endpoint already used as the genre
    fallback.
  - `TAGS_CACHE = cache/tags.json`, `_TAG_CACHE_LOCK`, and
    `load_tag_cache` / `save_tag_cache` / `merge_tag_cache` — exact mirrors of
    the genre-cache helpers (atomic write, `os.makedirs(CACHE_DIR)` on save).
- **`pcgr/genres.py`** (pure logic):
  - `CURATED_TAGS` (the `{canonical: [synonyms]}` map) and a derived
    case-insensitive lookup `{synonym_lower: canonical}` (including each
    canonical mapped to itself).
  - `build_tag_buckets(tags_by_appid, library_appids, min_size=MIN_BUCKET_SIZE)`
    → `[{name, count, appids, kind: "tag"}]`, canonicalizing each game's tags,
    bucketing by canonical name, honoring `min_size`, ordered by the curated
    list. Pure — no I/O. (The existing `build_genre_buckets` may grow a
    `kind: "genre"` field for parity; optional.)
- **`pcgr/services/genres.py`** (`GenreService`):
  - Gains a tag warmer mirroring the genre warmer (same lock/thread-guard/flush
    pattern) that resolves uncached tags via `fetch_tags` + `merge_tag_cache`.
  - `get_buckets(appids)` returns **genre buckets followed by tag buckets**
    (one combined `collections` list), warming both tracks.
  - `status(appids)` reflects overall progress (a game counts as categorized
    once it's present in **both** caches, so the progress bar doesn't read 100%
    while tags are still resolving). Implementation: `pending` = appids missing
    from genre cache **or** tag cache.
- **`pcgr/api.py`:** no new delegators — `get_auto_collections` /
  `get_auto_collection_status` already return whatever `GenreService` produces.
- **Frontend (`web/`):** no change. `renderAutoGenreCards` already renders the
  returned buckets and the progress note.

### Data flow
Enable → `GenreService.get_buckets(library)` builds genre + tag buckets from
both caches (whatever's warm now) and kicks off both warmers → frontend renders
genre+tag cards → polls `get_auto_collection_status` and re-pulls as caches
fill → instant on later launches.

## Edge cases
- **Tag-less games** (no SteamSpy entry): contribute no tag buckets; still
  spinnable via Whole Library; no "Uncategorized" bucket.
- **Synonym collisions:** the lookup is built canonical-first so a canonical
  name always maps to itself; duplicate synonyms across canonicals are a
  curation error caught by a unit test (each synonym maps to exactly one
  canonical).
- **A tag that is also a genre name** (none in the current sets) — not a
  concern; genre and tag buckets are independent lists.
- **Threshold interaction:** top-15 cache + `min_size` together keep buckets
  meaningful.
- **Large library:** tags add a second one-time background crawl (SteamSpy);
  same progressive, opt-in, cached-forever model as genres.

## Verification
- **Unit tests** (`tests/test_auto_collections.py`, extended):
  - `build_tag_buckets`: synonym grouping (Souls-like → Soulslike;
    Rogue-lite/Action Roguelike → Roguelike), case-insensitive, top-N already
    applied upstream, `min_size` hides small buckets, multi-membership, only
    library appids counted.
  - `CURATED_TAGS` integrity: every synonym maps to exactly one canonical.
  - `fetch_tags` / tag-cache round-trip + merge (temp dir, mocked).
- **Runtime (CDP):** enable auto-collections; confirm tag buckets (e.g.
  *Roguelike*, *Metroidvania*) appear alongside genre buckets and fill
  progressively; relaunch → instant from cache; `cache/tags.json` written and
  gitignored; Steam Collections file byte-for-byte unchanged.

## Out of scope
- GOG/Epic genres & tags (Phase B).
- Any change to the genre source or the playtime/dedup/edition filters.
