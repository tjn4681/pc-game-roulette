# Auto-Collections by Genre (Phase A: Steam) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Steam users with no custom Collections an opt-in, one-click way to auto-generate spinnable collection cards grouped by Steam genre, warmed in the background and cached.

**Architecture:** A new `GenreService` owns a genre cache (`cache/genres.json`) and a background warmer modeled on the existing name-warmer. Genres come per-game from the Steam `appdetails` API (`filters=genres`), with SteamSpy as a fallback. A pure helper buckets cached genres (filtered to a curated allowlist + a minimum size) into the same collection-card shape the grid already renders. The js_api facade exposes enable/disable + get-buckets + progress; the frontend shows an opt-in button and renders genre cards next to "Whole Library," polling for progressive fill. **App-internal only — never writes to Steam's data.**

**Tech Stack:** Python 3.13 (`pcgr` package), pywebview js_api, vanilla JS/HTML/CSS in `web/`. No pytest — pure logic is tested with stdlib `unittest` (`python -m unittest`); integrated/network/UI behavior is verified by running the app over the Chrome DevTools Protocol (CDP), as the rest of the codebase is.

**Spec:** `docs/superpowers/specs/2026-06-19-auto-collections-by-genre-design.md`

---

## Key facts the implementer needs

- **Genre source is verified.** `https://store.steampowered.com/api/appdetails?appids=<id>&filters=genres&cc=us&l=english` returns `{"<id>":{"success":true,"data":{"genres":[{"id":"1","description":"Action"},...]}}}`. SteamSpy fallback `https://steamspy.com/api.php?request=appdetails&appid=<id>` returns `{"genre":"Action, Adventure", ...}`.
- **Existing patterns to mirror:**
  - `pcgr/sources/store.py` already has `fetch_name_from_api` (the appdetails headers to copy), and the name cache (`load_name_cache`/`save_name_cache` with `_NAME_CACHE_LOCK`, atomic write). `CACHE_DIR` is imported there.
  - `pcgr/services/names.py` `NameService` has the background-warmer pattern (lock + thread guard + periodic flush + `time.sleep` politeness). Copy its shape.
  - Leaf pure-logic modules live at package top: `pcgr/dedup.py`, `pcgr/titles.py`. The new pure genre module follows them.
  - `pcgr/api.py` composes services in `__init__` and exposes thin delegators (see the Filters section).
- **Frontend (`web/app.js`):**
  - `renderCollections(collections, shortcutAppids, hiddenList)` (~line 185) renders the **Steam** grid. It builds `allAppIds` (the library: owned-via-key, else collections ∪ installed, plus shortcuts) ~line 217 and appends the "Whole Library" card. The no-collections branch is ~line 238 (`if (!filteredColls.length) { ...; return; }`); collections render ~line 249. Two `showScreen('screen-main')` calls (lines ~243, ~251).
  - `makeCollCard(collection, variant)` (~line 254) builds a spinnable card from `{name, count, appids}`; `variant` adds a CSS class (`'library'`, `'shortcuts'`). Cards call `openSpin(collection)`.
  - Re-render the Steam grid after a settings change with: `renderCollections(allCollections, allShortcutAppids, allHiddenCollections)` (globals; see line 2172).
  - Settings screen opens via `openSettings()` (~line 944) which calls a series of `refresh*Settings()`; change handlers are registered ~line 2968.
  - `#empty-state` (index.html line 101) is shown only when there are genuinely no games.
- **Run the app:** `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222 python main.py`. Kill stray `msedgewebview2.exe`/`python main.py` first (orphans hold the debug port → false readings).

---

## File structure

- **Create** `pcgr/genres.py` — pure: `CURATED_GENRES`, `MIN_BUCKET_SIZE`, `build_genre_buckets()`.
- **Modify** `pcgr/sources/store.py` — `fetch_genres()`, `GENRES_CACHE`, `load_genre_cache()`, `save_genre_cache()`, `merge_genre_cache()`.
- **Create** `pcgr/services/genres.py` — `GenreService` (cache + warmer + buckets + status + enable flag).
- **Modify** `pcgr/api.py` — instantiate `self.genres`; add 4 delegators.
- **Modify** `web/app.js` — enabled-state cache, render genre cards, opt-in button, progressive poll, Settings toggle.
- **Modify** `web/index.html` — Settings "Auto-Collections" toggle.
- **Modify** `web/style.css` — genre-card marker + opt-in button styling.
- **Modify** `tests/test_auto_collections.py` (Create) — stdlib unittest for the pure bucket builder + cache round-trip + status logic.

---

## Task 1: Pure genre bucket builder

**Files:**
- Create: `pcgr/genres.py`
- Test: `tests/test_auto_collections.py`

- [ ] **Step 1: Write the failing test.** Create `tests/test_auto_collections.py`:

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcgr.genres import build_genre_buckets, CURATED_GENRES


class TestBuildGenreBuckets(unittest.TestCase):
    def test_game_appears_in_every_curated_genre_it_matches(self):
        cache = {"10": ["Action", "Adventure"], "20": ["Action"]}
        out = build_genre_buckets(cache, [10, 20], min_size=1)
        by = {b["name"]: b for b in out}
        self.assertEqual(by["Action"]["appids"], [10, 20])
        self.assertEqual(by["Adventure"]["appids"], [10])

    def test_non_curated_genres_are_dropped(self):
        cache = {"10": ["Indie", "Early Access", "Free to Play"]}
        out = build_genre_buckets(cache, [10], min_size=1)
        self.assertEqual(out, [])

    def test_min_size_hides_small_buckets(self):
        cache = {"10": ["Racing"], "20": ["Action"], "30": ["Action"]}
        out = build_genre_buckets(cache, [10, 20, 30], min_size=2)
        names = [b["name"] for b in out]
        self.assertIn("Action", names)        # 2 games -> kept
        self.assertNotIn("Racing", names)     # 1 game  -> hidden

    def test_only_library_appids_counted_and_uncached_ignored(self):
        cache = {"10": ["Action"], "99": ["Action"]}  # 99 not in library
        out = build_genre_buckets(cache, [10, 50], min_size=1)  # 50 uncached
        self.assertEqual(out, [{"name": "Action", "count": 1, "appids": [10]}])

    def test_curated_list_excludes_noise(self):
        for junk in ("Indie", "Early Access", "Free to Play", "Gore", "Utilities"):
            self.assertNotIn(junk, CURATED_GENRES)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it, confirm FAIL:** `python -m unittest tests.test_auto_collections -v` → `ImportError: cannot import name 'build_genre_buckets'`.

- [ ] **Step 3: Create `pcgr/genres.py`:**

```python
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
```

- [ ] **Step 4: Run tests, confirm PASS:** `python -m unittest tests.test_auto_collections -v` → 5 tests OK.

- [ ] **Step 5: Commit:**
```bash
git add pcgr/genres.py tests/test_auto_collections.py
git commit -m "feat(genres): pure curated genre bucket builder"
```

---

## Task 2: Genre fetch + cache (sources/store.py)

**Files:**
- Modify: `pcgr/sources/store.py`
- Test: `tests/test_auto_collections.py`

- [ ] **Step 1: Write the failing test.** Append to `tests/test_auto_collections.py` (after the existing class):

```python
import json
import tempfile
from unittest import mock


class TestGenreCache(unittest.TestCase):
    def test_cache_round_trip_and_merge(self):
        import pcgr.sources.store as store
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "genres.json")
            with mock.patch.object(store, "GENRES_CACHE", path):
                self.assertEqual(store.load_genre_cache(), {})
                store.save_genre_cache({"10": ["Action"]})
                self.assertEqual(store.load_genre_cache(), {"10": ["Action"]})
                # merge adds new keys, preserves existing
                store.merge_genre_cache({"20": ["RPG"], "10": ["IGNORED"]})
                got = store.load_genre_cache()
                self.assertEqual(got["20"], ["RPG"])
                self.assertEqual(got["10"], ["Action"])  # not overwritten
```

- [ ] **Step 2: Run it, confirm FAIL:** `python -m unittest tests.test_auto_collections -v` → `AttributeError: module 'pcgr.sources.store' has no attribute 'GENRES_CACHE'`.

- [ ] **Step 3: Add the fetch + cache to `pcgr/sources/store.py`.**

First, find the line `from pcgr.config import CACHE_DIR, NAMES_CACHE` and confirm `CACHE_DIR` is imported (it is). Near the other cache-path constants / the name-cache section, add the genre-cache constant and lock:

```python
GENRES_CACHE = os.path.join(CACHE_DIR, "genres.json")
_GENRE_CACHE_LOCK = threading.Lock()
```

Add this fetch function next to `fetch_name_from_api` (after `fetch_name_from_steamspy`):

```python
def fetch_genres(appid):
    """Return a list of Steam genre names for `appid`, or None on failure.

    An empty list [] means 'fetched successfully, but the game has no genres' —
    callers should cache that so it isn't re-fetched.  None means the lookup
    failed (network/again-later) and should NOT be cached.

    Primary source: Steam appdetails (filters=genres, small payload).  Fallback:
    SteamSpy (comma-separated genre string), which covers some delisted games."""
    url = (f"https://store.steampowered.com/api/appdetails"
           f"?appids={appid}&filters=genres&cc=us&l=english")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Cookie":     "birthtime=283993201; mature_content=1; "
                          "wants_mature_content=1; lastagecheckage=1-0-1979",
            "Referer":    "https://store.steampowered.com/",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        entry = data.get(str(appid), {})
        if entry.get("success"):
            genres = (entry.get("data") or {}).get("genres") or []
            return [g.get("description", "").strip()
                    for g in genres if g.get("description")]
    except Exception:
        pass
    # SteamSpy fallback
    try:
        url2 = f"https://steamspy.com/api.php?request=appdetails&appid={appid}"
        req2 = urllib.request.Request(url2, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = (data.get("genre") or "").strip()
        if raw:
            return [x.strip() for x in raw.split(",") if x.strip()]
    except Exception:
        pass
    return None
```

Add the cache helpers in the name-cache section (after `save_name_cache`):

```python
def load_genre_cache():
    if os.path.isfile(GENRES_CACHE):
        try:
            with open(GENRES_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_genre_cache(cache):
    """Atomically persist the genre cache (write-temp-then-replace), locked so
    the background warmer and the js_api thread can't corrupt the JSON."""
    with _GENRE_CACHE_LOCK:
        tmp = GENRES_CACHE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp, GENRES_CACHE)
        except OSError:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass


def merge_genre_cache(new_genres):
    """Read-merge-write the genre cache; only adds appids not already cached."""
    if not new_genres:
        return
    cache = load_genre_cache()
    changed = False
    for k, v in new_genres.items():
        if k not in cache:
            cache[k] = v
            changed = True
    if changed:
        save_genre_cache(cache)
```

- [ ] **Step 4: Run tests, confirm PASS:** `python -m unittest tests.test_auto_collections -v` → 6 tests OK.

- [ ] **Step 5: Verify the live fetch works (network):**
```bash
python -c "from pcgr.sources.store import fetch_genres; print(fetch_genres(620))"
```
Expected: `['Action', 'Adventure']` (Portal 2).

- [ ] **Step 6: Commit:**
```bash
git add pcgr/sources/store.py tests/test_auto_collections.py
git commit -m "feat(genres): fetch_genres + genres.json cache (appdetails + steamspy)"
```

---

## Task 3: GenreService (cache + warmer + buckets + status + enable)

**Files:**
- Create: `pcgr/services/genres.py`
- Test: `tests/test_auto_collections.py`

- [ ] **Step 1: Write the failing test.** Append to `tests/test_auto_collections.py`:

```python
class TestGenreService(unittest.TestCase):
    def test_status_counts_cached_vs_pending(self):
        from pcgr.services.genres import GenreService
        svc = GenreService()
        with mock.patch("pcgr.services.genres.load_genre_cache",
                        return_value={"10": ["Action"], "20": []}):
            # patch the warmer so no real thread/network starts
            with mock.patch.object(svc, "_warm"):
                st = svc.status([10, 20, 30])
        self.assertEqual(st["total"], 3)
        self.assertEqual(st["categorized"], 2)   # 10 and 20 are cached
        self.assertEqual(st["pending"], 1)        # 30 not cached

    def test_get_buckets_uses_cache_and_curated_filter(self):
        from pcgr.services.genres import GenreService
        svc = GenreService()
        cache = {"10": ["Action", "Indie"], "20": ["Action"]}
        with mock.patch("pcgr.services.genres.load_genre_cache", return_value=cache):
            with mock.patch.object(svc, "_warm"):
                r = svc.get_buckets([10, 20])
        self.assertEqual(r["status"], "ok")
        names = [b["name"] for b in r["collections"]]
        self.assertEqual(names, ["Action"])       # Indie dropped, min_size=2 met
        self.assertEqual(r["collections"][0]["appids"], [10, 20])
```

- [ ] **Step 2: Run it, confirm FAIL:** `python -m unittest tests.test_auto_collections -v` → `ModuleNotFoundError: No module named 'pcgr.services.genres'`.

- [ ] **Step 3: Create `pcgr/services/genres.py`:**

```python
"""
Auto-collections by genre.

Owns the genre cache and a polite background warmer (modeled on NameService):
when asked for buckets it returns whatever genres are cached now and kicks off
resolution of any uncached library appids, so cards fill in progressively over
the first run and load instantly afterward.  Buckets are built by the pure
``pcgr.genres.build_genre_buckets`` (curated allowlist + minimum size).

Steam only for now (Phase A).  App-internal — never writes Steam data.
"""

import threading
import time

from pcgr.config import get_setting, set_setting
from pcgr.genres import build_genre_buckets
from pcgr.sources.store import fetch_genres, load_genre_cache, merge_genre_cache


class GenreService:
    def __init__(self):
        self._warm_lock = threading.Lock()
        self._warm_thread = None

    # ── Enable flag ───────────────────────────────────────────────────────

    def is_enabled(self):
        return bool(get_setting("auto_collections_enabled", False))

    def set_enabled(self, enabled):
        set_setting("auto_collections_enabled", bool(enabled))
        return {"status": "ok", "enabled": bool(enabled)}

    # ── Buckets + progress ────────────────────────────────────────────────

    def get_buckets(self, appids):
        """Return genre collection cards for the given library appids, building
        from cached genres and kicking off a background warm for any uncached
        ones so the buckets fill in over time."""
        ids = self._ints(appids)
        cache = load_genre_cache()
        self._warm(ids, cache)
        return {"status": "ok", "collections": build_genre_buckets(cache, ids)}

    def status(self, appids):
        """Progress for the 'Categorizing…' indicator."""
        ids = self._ints(appids)
        cache = load_genre_cache()
        categorized = sum(1 for a in ids if str(a) in cache)
        return {"status": "ok", "total": len(ids),
                "categorized": categorized, "pending": len(ids) - categorized}

    # ── Background warmer ─────────────────────────────────────────────────

    def _warm(self, appids, cache):
        """Resolve genres for uncached appids on a daemon thread.  Polite
        (fixed delay), flushes every 25 hits so progress survives a crash, and
        resumes across launches (cache persists).  One warmer at a time."""
        todo = [a for a in appids if str(a) not in cache]
        if not todo:
            return
        with self._warm_lock:
            if self._warm_thread and self._warm_thread.is_alive():
                return

            def _worker():
                resolved = {}
                for appid in todo:
                    try:
                        g = fetch_genres(appid)
                    except Exception:
                        g = None
                    if g is not None:            # [] (no genres) is a real result
                        resolved[str(appid)] = g
                    if len(resolved) >= 25:
                        merge_genre_cache(resolved)
                        resolved = {}
                    time.sleep(0.20)             # ~5 req/s, gentle on Steam
                if resolved:
                    merge_genre_cache(resolved)

            t = threading.Thread(target=_worker, name="genre-warmer", daemon=True)
            self._warm_thread = t
            t.start()

    @staticmethod
    def _ints(appids):
        out = []
        for a in (appids or []):
            try:
                out.append(int(a))
            except (TypeError, ValueError):
                pass
        return out
```

- [ ] **Step 4: Run tests, confirm PASS:** `python -m unittest tests.test_auto_collections -v` → 8 tests OK.

- [ ] **Step 5: Lint:** `python -m pyflakes pcgr/genres.py pcgr/services/genres.py pcgr/sources/store.py` → no output.

- [ ] **Step 6: Commit:**
```bash
git add pcgr/services/genres.py tests/test_auto_collections.py
git commit -m "feat(genres): GenreService (cache + warmer + buckets + status)"
```

---

## Task 4: Facade wiring (api.py)

**Files:**
- Modify: `pcgr/api.py`

- [ ] **Step 1: Import + instantiate.** In `pcgr/api.py`, add the import near the other service imports (after `from pcgr.services.filters import FilterService`):
```python
from pcgr.services.genres import GenreService
```
In `__init__`, after `self.filters = FilterService(...)`, add:
```python
        self.genres    = GenreService()
```

- [ ] **Step 2: Add delegators.** After the `set_playtime_settings` delegator in the Filters section, add a new block:
```python

    # ════════════════════════════════════════════════════════════════════
    #  Auto-collections (genre buckets for no-collection users)
    # ════════════════════════════════════════════════════════════════════

    def get_auto_collections_enabled(self):
        return {"status": "ok", "enabled": self.genres.is_enabled()}
    def set_auto_collections_enabled(self, enabled):
        return self.genres.set_enabled(enabled)
    def get_auto_collections(self, appids):
        return self.genres.get_buckets(appids)
    def get_auto_collection_status(self, appids):
        return self.genres.status(appids)
```

- [ ] **Step 3: Verify the facade constructs and delegators work:**
```bash
python -c "from pcgr import SteamRouletteAPI; a=SteamRouletteAPI(); print(a.get_auto_collections_enabled()); print(a.get_auto_collections([620]))"
```
Expected: prints `{'status': 'ok', 'enabled': False}` then `{'status': 'ok', 'collections': [...]}` (collections may be empty if 620's genres aren't cached yet — that's fine; no exception).

- [ ] **Step 4: Lint:** `python -m pyflakes pcgr/api.py` → no output.

- [ ] **Step 5: Commit:**
```bash
git add pcgr/api.py
git commit -m "feat(genres): compose GenreService + api delegators"
```

---

## Task 5: Frontend — opt-in button + render genre cards

**Files:**
- Modify: `web/app.js`
- Modify: `web/index.html`

- [ ] **Step 1: Add an enabled-state cache + loader near the top-of-file filter caches.** In `web/app.js`, find `let playtimeExcludes = null;` and add below it:
```javascript
let autoCollectionsEnabled = false;
```
Add this function right after `getPlaytimeExcludes()` (or near the other small async helpers):
```javascript
async function refreshAutoCollectionsEnabled() {
  if (!api) { autoCollectionsEnabled = false; return; }
  try {
    const r = await api.get_auto_collections_enabled();
    autoCollectionsEnabled = !!(r && r.enabled);
  } catch (_) { autoCollectionsEnabled = false; }
}
```

- [ ] **Step 2: Add the render + button helpers.** In `web/app.js`, add these two functions just before `function makeCollCard(` (~line 254):
```javascript
// Append genre auto-collection cards (and a progress note) to the Steam grid.
// `allAppIds` is the library set the buckets are restricted to.
async function renderAutoGenreCards(grid, allAppIds) {
  if (!api || !allAppIds.length) return;
  let r;
  try { r = await api.get_auto_collections(allAppIds); } catch (_) { return; }
  grid.querySelectorAll('.coll-card-genre').forEach(el => el.remove());
  const old = document.getElementById('genre-progress');
  if (old) old.remove();
  (r && r.collections || []).forEach(c => grid.appendChild(makeCollCard(c, 'genre')));
  // Progress note while the warmer is still resolving genres.
  let st;
  try { st = await api.get_auto_collection_status(allAppIds); } catch (_) { st = null; }
  if (st && st.pending > 0) {
    const note = document.createElement('div');
    note.id = 'genre-progress';
    note.className = 'genre-progress';
    note.textContent = `Categorizing your library… ${st.categorized} of ${st.total} games`;
    grid.appendChild(note);
  }
}

// Opt-in call-to-action shown to no-collections users who haven't enabled the
// feature yet.
function appendAutoCollectionsCTA(grid, allAppIds) {
  const card = document.createElement('div');
  card.className = 'coll-card coll-card-autocta';
  card.innerHTML = `
    <div class="coll-name">Auto-organize by genre</div>
    <div class="coll-count">Group your library into genre collections to spin</div>
    <button class="btn-primary btn-sm" id="auto-cta-btn">Auto-organize</button>`;
  card.querySelector('#auto-cta-btn').addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!api) return;
    await api.set_auto_collections_enabled(true);
    autoCollectionsEnabled = true;
    renderCollections(allCollections, allShortcutAppids, allHiddenCollections);
  });
  grid.appendChild(card);
}
```

- [ ] **Step 3: Wire them into `renderCollections`.** In `web/app.js`, the no-collections branch currently reads:
```javascript
  if (!filteredColls.length) {
    // No custom collections.  We may still have a Whole Library (from the API
    // key) or shortcuts; only drop to the installed-only scan when there's
    // genuinely nothing else.
    if (allAppIds.length === 0) loadInstalledLibrary(grid, empty);
    else { empty.classList.add('hidden'); showScreen('screen-main'); }
    return;
  }
```
Replace it with:
```javascript
  if (!filteredColls.length) {
    // No custom collections.  We may still have a Whole Library (from the API
    // key) or shortcuts; only drop to the installed-only scan when there's
    // genuinely nothing else.
    if (allAppIds.length === 0) { loadInstalledLibrary(grid, empty); return; }
    empty.classList.add('hidden');
    showScreen('screen-main');
    if (autoCollectionsEnabled) await renderAutoGenreCards(grid, allAppIds);
    else appendAutoCollectionsCTA(grid, allAppIds);
    return;
  }
```
And just before the FINAL `showScreen('screen-main');` (the one after the collections `.forEach`, ~line 251), add:
```javascript
  if (autoCollectionsEnabled) await renderAutoGenreCards(grid, allAppIds);
```

- [ ] **Step 4: Mark genre cards in `makeCollCard`.** In `web/app.js`, in `makeCollCard`, find:
```javascript
  if      (variant === 'library')   classes += ' coll-card-library';
  else if (variant === 'shortcuts') classes += ' coll-card-shortcuts';
```
and add a branch:
```javascript
  else if (variant === 'genre')     classes += ' coll-card-genre';
```

- [ ] **Step 5: Load the enabled flag on startup.** In `web/app.js`, find where the app first loads Steam data (the init path that calls `api.auto_load()` ~line 2863 / `handleLoadResult`). Immediately before that `auto_load()` call, add:
```javascript
  await refreshAutoCollectionsEnabled();
```
(If there are two such init call sites, add it before each — search for `api.auto_load()`.)

- [ ] **Step 6: Syntax check:** `node --check web/app.js` (or note node unavailable; Task 7 launch will catch syntax errors).

- [ ] **Step 7: Commit:**
```bash
git add web/app.js
git commit -m "feat(genres): opt-in button + render genre cards in the Steam grid"
```

---

## Task 6: Frontend — progressive fill, Settings toggle, styling

**Files:**
- Modify: `web/app.js`
- Modify: `web/index.html`
- Modify: `web/style.css`

- [ ] **Step 1: Progressive fill — poll while warming.** In `web/app.js`, replace the progress-note block inside `renderAutoGenreCards` (the `if (st && st.pending > 0) {...}`) with this version that re-renders until the warmer finishes:
```javascript
  if (st && st.pending > 0) {
    const note = document.createElement('div');
    note.id = 'genre-progress';
    note.className = 'genre-progress';
    note.textContent = `Categorizing your library… ${st.categorized} of ${st.total} games`;
    grid.appendChild(note);
    // Re-render in a few seconds to show newly-categorized games, but only if
    // the user is still on the Steam grid (avoid background churn elsewhere).
    clearTimeout(window.__genrePoll);
    window.__genrePoll = setTimeout(() => {
      if (currentPlatform === 'steam' && autoCollectionsEnabled) {
        renderAutoGenreCards(grid, allAppIds);
      }
    }, 4000);
  } else {
    clearTimeout(window.__genrePoll);
  }
```
(`currentPlatform` is the existing global tracking the active tab.)

- [ ] **Step 2: Add the Settings toggle markup.** In `web/index.html`, find the `<section class="settings-section">` whose heading is `<h3>Playtime</h3>` and insert this NEW section immediately BEFORE it:
```html
      <section class="settings-section">
        <h3>Auto-Collections</h3>
        <label class="dedup-toggle">
          <input type="checkbox" id="auto-collections-enabled">
          <span>Auto-organize my Steam library into genre collections</span>
        </label>
        <div class="edition-pref-label" style="margin-top:6px;">
          Generates spinnable collections by Steam genre — handy when you don't
          use custom Collections. Built inside this app only; your real Steam
          collections are never touched. The first run fills in over a few
          minutes as genres are looked up.
        </div>
      </section>
```

- [ ] **Step 3: Load the toggle state on Settings open.** In `web/app.js`, add this function just before `refreshPlaytimeSettings`:
```javascript
async function refreshAutoCollectionsSettings() {
  if (!api) return;
  const r = await api.get_auto_collections_enabled();
  if (r.status !== 'ok') return;
  document.getElementById('auto-collections-enabled').checked = !!r.enabled;
}
```
In `openSettings()`, after `await refreshPlaytimeSettings();`, add:
```javascript
  await refreshAutoCollectionsSettings();
```

- [ ] **Step 4: Register the toggle handler.** In `web/app.js`, after the playtime handlers registered near line ~3011, add:
```javascript
  document.getElementById('auto-collections-enabled').addEventListener('change', async (e) => {
    if (!api) return;
    await api.set_auto_collections_enabled(e.target.checked);
    autoCollectionsEnabled = e.target.checked;
  });
```

- [ ] **Step 5: Styling.** In `web/style.css`, append:
```css
/* Auto-collection genre cards: subtle "auto" marker distinguishing them from
   hand-made collections. */
.coll-card-genre { border-style: dashed; }
.coll-card-genre .coll-name::after {
  content: "auto";
  margin-left: 8px;
  font-size: 0.6rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  opacity: 0.55;
  vertical-align: middle;
}
.coll-card-autocta { display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.coll-card-autocta .coll-count { white-space: normal; }
.genre-progress {
  grid-column: 1 / -1;
  padding: 8px 4px;
  font-size: 0.85rem;
  opacity: 0.7;
}
```

- [ ] **Step 6: Syntax check:** `node --check web/app.js` (or note node unavailable).

- [ ] **Step 7: Commit:**
```bash
git add web/app.js web/index.html web/style.css
git commit -m "feat(genres): progressive fill, Settings toggle, genre-card styling"
```

---

## Task 7: Runtime CDP verification

**Files:** none (verification only)

- [ ] **Step 1: Kill stray processes and launch.**
```bash
powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | ? { \$_.CommandLine -like '*steam-roulette*' } | % { Stop-Process -Id \$_.ProcessId -Force -EA SilentlyContinue }; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | ? { \$_.CommandLine -like '*main.py*' } | % { Stop-Process -Id \$_.ProcessId -Force -EA SilentlyContinue }"
```
Then launch (background): `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222 python main.py`

- [ ] **Step 2: Verify the backend end-to-end via CDP.** Drive `window.pywebview.api` (port 9222), evaluating (with a small library sample so the warm is quick):
```javascript
(async () => {
  const A = window.pywebview.api;
  const sample = [620, 730, 570, 440, 271590, 1174180]; // mixed-genre Steam appids
  await A.set_auto_collections_enabled(true);
  const en = await A.get_auto_collections_enabled();
  // poll until warm (or ~30s)
  let st, buckets;
  for (let i = 0; i < 15; i++) {
    st = await A.get_auto_collection_status(sample);
    buckets = await A.get_auto_collections(sample);
    if (st.pending === 0) break;
    await new Promise(r => setTimeout(r, 2000));
  }
  await A.set_auto_collections_enabled(false); // restore dev state
  return JSON.stringify({ enabled: en, status: st,
    bucketNames: (buckets.collections||[]).map(b => b.name + ':' + b.count) });
})()
```
Expected: `enabled.enabled === true`; after polling `status.pending === 0`; `bucketNames` includes real genres (e.g. `Action:N`, `RPG:N`) built from the sample. Confirm no exception.

- [ ] **Step 3: Verify the Settings toggle reflects + persists.** Via CDP: `await openSettings()`, then read `document.getElementById('auto-collections-enabled')` — confirm present and its `.checked` matches the persisted enabled state.

- [ ] **Step 4: Verify the genre cache wrote to disk and is app-internal.**
```bash
test -f cache/genres.json && echo "genres.json exists" && python -c "import json;print('cached appids:', len(json.load(open('cache/genres.json'))))"
git status --porcelain cache/ | head   # expect empty — cache/ is gitignored
```
Confirm Steam's collections file is untouched (we never open it for writing — code inspection: no writes to `cloud-storage-namespace-1.json`).

- [ ] **Step 5: Spot-check the UI** (optional screenshot via CDP `Page.captureScreenshot`): with auto-collections enabled and a no-collections state, confirm genre cards render alongside Whole Library; with it disabled, confirm the "Auto-organize" button appears.

- [ ] **Step 6: Restore dev state + close.** Ensure `set_auto_collections_enabled(false)` was called (Step 2 does). Kill the app processes (same PowerShell as Step 1).

---

## Self-review notes (already applied)

- **Spec coverage:** opt-in button in empty state + Settings (Task 5/6); progressive background fill with status indicator (Task 3 warmer + Task 6 poll); persistence via flag + `genres.json`, rebuilt-from-cache each launch (Task 3); genre source = appdetails `filters=genres` + SteamSpy fallback (Task 2); curated allowlist + min-size, multi-genre membership, no "Uncategorized" bucket (Task 1); genre cards are normal spinnable collections so other filters apply (Task 5 uses `makeCollCard`); **app-internal / never writes Steam data** (no Steam writes anywhere; Task 7 Step 4 confirms); Steam-only Phase A behind a service seam (GenreService orchestrates; provider = store fetch). ✓
- **Type consistency:** `build_genre_buckets(genres_by_appid, library_appids, min_size)` used identically in Task 1 and Task 3; cache shape `{appid_str: [genre_str]}` consistent across store + service + builder; facade method names (`get_auto_collections`, `get_auto_collection_status`, `*_enabled`) match the frontend calls in Tasks 5/6; CSS classes (`coll-card-genre`, `coll-card-autocta`, `genre-progress`) and element ids (`auto-collections-enabled`, `auto-cta-btn`, `genre-progress`) consistent across JS/HTML/CSS. ✓
- **No placeholders:** every step has concrete code and exact anchors. ✓
- **Out of scope (deferred, per spec):** GOG/Epic (Galaxy) genre provider, curated community tags, writing real Steam collections.
