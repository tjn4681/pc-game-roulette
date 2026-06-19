# Auto-collections by genre — design

## Context
Custom Steam Collections are the heart of the roulette experience, but plenty of
players have never made any — and the app currently gives them only a single
"Whole Library" card to spin. This feature offers those users a one-click,
**completely optional** way to get *some* structure: auto-generated collections
grouped by **Steam genre**. It's explicitly a compromise — genres are coarser
than hand-made collections — but it turns an empty grid into something worth
spinning for people who'd otherwise never bother.

Genres are not stored locally; they come per-game from the same Steam
`appdetails` / SteamSpy calls we already use to resolve names. So the first run
warms genres in the background (progressively), and every later launch is
instant from cache.

## Hard constraints
- **App-internal only.** Auto-genre buckets are virtual collections that live
  entirely inside PC Game Roulette. They **never** write to or modify the user's
  real Steam Collections (`cloud-storage-namespace-1.json`) and never appear in
  the Steam client. The app's read-only-toward-Steam guarantee is preserved, and
  the feature is fully reversible.

## Requirements

### Behavior
- **Opt-in trigger:** a button **"Auto-organize my library by genre,"** shown
  (a) prominently in the **no-collections empty state** and (b) in **Settings**.
- **On enable:** genres resolve in the background; the grid immediately shows
  whatever is already cached and **fills in progressively** (with a small
  "Categorizing… N games done" indicator). Later launches are instant.
- **Presentation:** genre buckets render as **normal spinnable collection cards**
  in the Steam grid, **alongside** the existing "Whole Library" hero (not
  replacing it), with a subtle marker distinguishing them from hand-made
  collections.
- **Persistence:** enabling sets a flag; genre data caches to disk like names.
  Buckets are **rebuilt from the cache on each launch** (not frozen), so they
  stay correct as the library changes. Re-clicking the button re-scans and warms
  any new/unknown games.
- **Opt-out:** a toggle turns it back off (hides the genre cards; keeps the cache
  so re-enabling is instant).

### Scope (phased)
- **Phase A (this spec): Steam only.** Nail the mechanism end-to-end behind a
  small "genre provider" abstraction so other platforms slot in later.
- **Phase B (fast-follow, noted here for the abstraction's sake): GOG via the
  Galaxy DB**, which stores genre data locally. Because Galaxy keys genres by
  release key, this provider also covers **Epic games integrated into GOG
  Galaxy** for free. Phase B is out of scope for the first implementation but the
  interfaces are designed for it.
- Native Epic genres (no Galaxy) are **not** pursued — there's no reliable local
  or low-effort source.

### Taxonomy
- **Phase A:** Steam **genres** only.
- **Future (not this spec):** a curated set of popular community **tags**
  (Roguelike, Soulslike, Shooter, Open World, Horror…) layered on top to add
  vibes that genres miss. The design keeps a seam for this.

## Architecture (in the `pcgr` package)
- **Genre source (`pcgr/sources/store.py`):** extend the existing `appdetails`
  fetch so resolving a game also captures its **`genres`** in the *same* HTTP
  request used for the name (no extra requests). SteamSpy remains the fallback
  source.
- **`pcgr/services/genres.py` → `GenreService`:** owns the genre cache
  (`cache/genres.json`, mirroring `names.json`) and a **background warmer**
  modeled on `NameService` (polite rate-limit, periodic flush to disk, resumes
  across launches; reuses the same threading/locking pattern). Responsibilities:
  - `warm(appids)` — background-resolve genres for the given appids.
  - cached-genre lookup for a set of appids.
  - `build_buckets(appids)` → `{genre: [appid, ...]}` filtered by the curated
    allowlist and the minimum-size rule, returned in the **collection-card
    shape** (`{name, count, appids}`) the grid already renders.
  - `status(appids)` — counts of categorized vs pending for the progress
    indicator.
  - A **genre-provider seam**: Phase A uses the Steam appdetails/SteamSpy
    provider; Phase B adds a Galaxy-DB provider (read-only, keyed by release
    key). `GenreService` orchestrates; only the provider differs.
- **Facade (`pcgr/api.py`)** delegators:
  - `get_auto_collections_enabled` / `set_auto_collections_enabled`
  - `get_auto_collections()` → genre buckets (collection-card shape)
  - `get_auto_collection_status()` → progress (categorized vs pending)
- **"Library appids"** = the same set the Whole Library card already builds
  (owned-via-API-key, else installed + collections), so the feature reuses
  existing logic rather than re-deriving the library.
- **Frontend (`web/`):** the empty-state + Settings button; render genre cards
  next to Whole Library when enabled; poll `get_auto_collection_status()` and
  re-pull `get_auto_collections()` while the warmer fills in.

### Data flow (on click)
1. Frontend calls `set_auto_collections_enabled(true)`.
2. `GenreService.warm(library_appids)` starts a background daemon.
3. Frontend calls `get_auto_collections()` → renders whatever is cached now.
4. Frontend polls `get_auto_collection_status()`; as it advances, re-pulls
   `get_auto_collections()` so cards appear/grow progressively.
5. On later launches, the cache is warm → buckets are built and shown instantly.

## Curated genres & edge cases
- **Default genre allowlist** (real game genres Steam returns, noise removed):
  **Action, Adventure, RPG, Strategy, Simulation, Sports, Racing, Casual,
  Massively Multiplayer.** Lives in one constant for easy tweaking.
- **Explicitly excluded** genres: business/quality labels (*Indie, Free to Play,
  Early Access*), content descriptors (*Gore, Violent, Nudity, Sexual
  Content*…), and non-game software "genres" (*Utilities, Audio Production,
  Design & Illustration, Education*…).
- **Minimum bucket size:** hide any genre with fewer than **2** games (a
  constant), to avoid a wall of single-game cards.
- **Multi-genre:** a game appears in **every** matching allowlisted bucket.
- **No-genre games** (delisted / no data / non-Steam shortcuts): appear in **no**
  genre card — there is **no "Uncategorized" bucket** — but remain spinnable via
  Whole Library, so nothing is lost.
- **Filters interplay:** genre cards are ordinary collections, so dedup, edition,
  the new playtime filter, and manual excludes all apply when spinning.
- **First-run latency:** large libraries warm over several minutes; the status
  indicator sets expectations; partial buckets are usable immediately.

## Verification (runtime)
- On a no-collections test state, click the button → genre cards appear and fill
  in progressively; the status indicator advances.
- Relaunch → cached genres persist; buckets build instantly with no re-fetch.
- Confirm a multi-genre game lands in multiple buckets.
- Confirm the curated allowlist excludes the junk genres and that sub-min-size
  buckets are hidden.
- **Confirm the real Steam Collections file is byte-for-byte unchanged** before
  and after enabling (the read-only guarantee).
- Toggle off → genre cards disappear, cache retained; re-enable is instant.
- Drive via CDP against the running app (kill zombie WebView2/python first).

## Out of scope (this spec)
- GOG (Galaxy) and Epic genre providers (Phase B; interfaces prepared).
- Curated community **tags** (future enrichment).
- Writing genres back as real Steam Collections.
