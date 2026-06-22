# Curated Tag Collections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add curated community-tag buckets (Roguelike, Metroidvania, …) as extra spinnable auto-collection cards alongside the existing genre buckets, under the same opt-in toggle.

**Architecture:** A parallel "tag track" mirroring the genre track: a `tags.json` cache + a SteamSpy `fetch_tags`, a pure `build_tag_buckets` (curated `{canonical: [synonyms]}` map + min-size), and `GenreService` warming/returning both tracks combined. Tag cards flow through the existing `get_auto_collections` → frontend render path unchanged, so **there is no facade or frontend change** — backend only.

**Tech Stack:** Python 3.13 (`pcgr` package), stdlib `unittest` (run via `python -m unittest`; no pytest). End-to-end verified by running the app and driving it over CDP.

**Spec:** `docs/superpowers/specs/2026-06-21-curated-tag-collections-design.md`

---

## Key facts the implementer needs

- **Genre track (the thing we mirror):**
  - `pcgr/genres.py`: `CURATED_GENRES`, `MIN_BUCKET_SIZE = 2`, `_CANON`, and `build_genre_buckets(genres_by_appid, library_appids, min_size=MIN_BUCKET_SIZE)` → `[{name,count,appids}]`.
  - `pcgr/sources/store.py`: `fetch_genres(appid)`; the genre cache block (`GENRES_CACHE`, `_GENRE_CACHE_LOCK`, `load_genre_cache`/`save_genre_cache`/`merge_genre_cache`) right after the name cache. `import json`/`os`/`threading`/`urllib.request` and `from pcgr.config import CACHE_DIR, NAMES_CACHE` are already at the top.
  - `pcgr/services/genres.py`: `GenreService` with `_warm_lock`/`_warm_thread`, `get_buckets`, `status`, `_warm`, `_ints`.
- **SteamSpy** `https://steamspy.com/api.php?request=appdetails&appid=<id>` returns `tags` as a vote-ordered `{tag: votes}` dict (or `[]` if none). It's the only community-tag source.
- **No pytest.** Tests live in `tests/test_auto_collections.py` (classes `TestBuildGenreBuckets`, `TestGenreCache`, `TestGenreService`; `from unittest import mock` is module-level).
- **Run the app:** `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222 python main.py`. Kill stray `msedgewebview2.exe`/`python main.py` first (orphans cause false readings).

## File structure
- **Modify** `pcgr/genres.py` — add `CURATED_TAGS`, `_TAG_CANON`, `build_tag_buckets`.
- **Modify** `pcgr/sources/store.py` — add `fetch_tags`, `TAGS_CACHE`, `_TAG_CACHE_LOCK`, `load_tag_cache`/`save_tag_cache`/`merge_tag_cache`.
- **Modify** `pcgr/services/genres.py` — `_warm` resolves both tracks; `get_buckets` returns genres+tags; `status` counts both caches.
- **Modify** `tests/test_auto_collections.py` — new tag tests; update the two `GenreService` tests for the dual-cache behavior.
- No change to `pcgr/api.py` or `web/` — tag buckets reuse the genre render path.

---

## Task 1: Curated tags + pure bucket builder

**Files:** Modify `pcgr/genres.py`; Test: `tests/test_auto_collections.py`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_auto_collections.py` (after the existing classes, before `if __name__`):

```python
from pcgr.genres import build_tag_buckets, CURATED_TAGS, CURATED_GENRES


class TestBuildTagBuckets(unittest.TestCase):
    def test_synonyms_canonicalize(self):
        cache = {"10": ["Souls-like"], "20": ["Action Roguelike"], "30": ["Rogue-lite"]}
        out = build_tag_buckets(cache, [10, 20, 30], min_size=1)
        by = {b["name"]: b for b in out}
        self.assertEqual(by["Soulslike"]["appids"], [10])
        self.assertEqual(by["Roguelike"]["appids"], [20, 30])  # both variants -> Roguelike

    def test_case_insensitive(self):
        out = build_tag_buckets({"10": ["mEtRoIdVaNiA"]}, [10], min_size=1)
        self.assertEqual([b["name"] for b in out], ["Metroidvania"])

    def test_min_size_and_library_scope(self):
        cache = {"10": ["Metroidvania"], "20": ["Roguelike"], "30": ["Roguelike"], "99": ["Roguelike"]}
        out = build_tag_buckets(cache, [10, 20, 30], min_size=2)  # 99 not in library
        self.assertEqual([b["name"] for b in out], ["Roguelike"])
        self.assertEqual(out[0]["appids"], [20, 30])

    def test_non_curated_tags_ignored(self):
        out = build_tag_buckets({"10": ["Great Soundtrack", "2D", "Singleplayer"]}, [10], min_size=1)
        self.assertEqual(out, [])

    def test_multi_membership(self):
        # one game, two distinct curated tags -> appears in both buckets
        cache = {"10": ["Roguelike", "Metroidvania"], "20": ["Roguelike"], "30": ["Metroidvania"]}
        out = build_tag_buckets(cache, [10, 20, 30], min_size=2)
        by = {b["name"]: b["appids"] for b in out}
        self.assertEqual(by["Roguelike"], [10, 20])
        self.assertEqual(by["Metroidvania"], [10, 30])


class TestCuratedTagsIntegrity(unittest.TestCase):
    def test_each_term_maps_to_one_canonical(self):
        seen = {}
        for canon, syns in CURATED_TAGS.items():
            for term in [canon] + syns:
                key = term.lower()
                self.assertNotIn(key, seen,
                                 f"{term!r} under {canon!r} also under {seen.get(key)!r}")
                seen[key] = canon

    def test_no_overlap_with_genres(self):
        self.assertEqual(set(CURATED_TAGS) & set(CURATED_GENRES), set())
```

- [ ] **Step 2: Run, verify FAIL.** Run: `python -m unittest tests.test_auto_collections -v`
Expected: FAIL — `ImportError: cannot import name 'build_tag_buckets'`.

- [ ] **Step 3: Implement.** In `pcgr/genres.py`, add at the end of the file:

```python
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
```

- [ ] **Step 4: Run, verify PASS.** Run: `python -m unittest tests.test_auto_collections -v`
Expected: all tests OK (the new tag tests included).

- [ ] **Step 5: Lint.** Run: `python -m pyflakes pcgr/genres.py tests/test_auto_collections.py`
Expected: no output.

- [ ] **Step 6: Commit.**

```bash
git add pcgr/genres.py tests/test_auto_collections.py
git commit -m "feat(tags): curated tag map + pure build_tag_buckets"
```

---

## Task 2: SteamSpy tag fetch + tags.json cache

**Files:** Modify `pcgr/sources/store.py`; Test: `tests/test_auto_collections.py`.

- [ ] **Step 1: Write the failing test.** Append to `tests/test_auto_collections.py`:

```python
class TestTagCache(unittest.TestCase):
    def test_cache_round_trip_and_merge(self):
        import tempfile
        import pcgr.sources.store as store
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tags.json")
            with mock.patch.object(store, "TAGS_CACHE", path):
                self.assertEqual(store.load_tag_cache(), {})
                store.save_tag_cache({"10": ["Roguelike"]})
                self.assertEqual(store.load_tag_cache(), {"10": ["Roguelike"]})
                store.merge_tag_cache({"20": ["Metroidvania"], "10": ["IGNORED"]})
                got = store.load_tag_cache()
                self.assertEqual(got["20"], ["Metroidvania"])
                self.assertEqual(got["10"], ["Roguelike"])  # existing not overwritten
```

- [ ] **Step 2: Run, verify FAIL.** Run: `python -m unittest tests.test_auto_collections -v`
Expected: FAIL — `AttributeError: module 'pcgr.sources.store' has no attribute 'TAGS_CACHE'`.

- [ ] **Step 3: Add `fetch_tags`.** In `pcgr/sources/store.py`, immediately AFTER the `fetch_genres` function (it ends with `return None` before the `# ── Name cache ──` comment), add:

```python
def fetch_tags(appid, top_n=15):
    """Return the game's top `top_n` SteamSpy community tags (vote-ordered) as a
    list of names, or None on failure (don't cache).  An empty list [] means
    'fetched, but no tags' and should be cached.  SteamSpy is the only source
    that exposes community tags; it returns `tags` as a vote-ordered
    {tag: votes} dict (or [] when none)."""
    try:
        url = f"https://steamspy.com/api.php?request=appdetails&appid={appid}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    tags = data.get("tags")
    if isinstance(tags, dict):
        return list(tags.keys())[:top_n]   # dict preserves SteamSpy's vote order
    if isinstance(tags, list):
        return tags[:top_n]
    return []
```

- [ ] **Step 4: Add the tag cache.** In `pcgr/sources/store.py`, immediately AFTER the `merge_genre_cache` function (end of the `# ── Genre cache ──` block, before `# ── Cross-platform "already searched" cache ──`), add:

```python
# ── Tag cache ───────────────────────────────────────────────────────────────

TAGS_CACHE = os.path.join(CACHE_DIR, "tags.json")
_TAG_CACHE_LOCK = threading.Lock()


def load_tag_cache():
    if os.path.isfile(TAGS_CACHE):
        try:
            with open(TAGS_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_tag_cache(cache):
    """Atomically persist the tag cache (write-temp-then-replace), locked so the
    background warmer and the js_api thread can't corrupt the JSON."""
    with _TAG_CACHE_LOCK:
        tmp = TAGS_CACHE + ".tmp"
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp, TAGS_CACHE)
        except OSError:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass


def merge_tag_cache(new_tags):
    """Read-merge-write the tag cache; only adds appids not already cached."""
    if not new_tags:
        return
    cache = load_tag_cache()
    changed = False
    for k, v in new_tags.items():
        if k not in cache:
            cache[k] = v
            changed = True
    if changed:
        save_tag_cache(cache)
```

- [ ] **Step 5: Run, verify PASS.** Run: `python -m unittest tests.test_auto_collections -v`
Expected: all OK.

- [ ] **Step 6: Live fetch check (network).** Run: `python -c "from pcgr.sources.store import fetch_tags; t=fetch_tags(367520); print('Metroidvania' in t, t[:4])"`
Expected: `True ['Metroidvania', ...]` (Hollow Knight; tags vote-ordered).

- [ ] **Step 7: Lint + commit.**

```bash
python -m pyflakes pcgr/sources/store.py tests/test_auto_collections.py
git add pcgr/sources/store.py tests/test_auto_collections.py
git commit -m "feat(tags): SteamSpy fetch_tags + tags.json cache"
```

---

## Task 3: GenreService warms + returns both tracks

**Files:** Modify `pcgr/services/genres.py`; Test: `tests/test_auto_collections.py`.

The two existing `TestGenreService` tests must be updated because `get_buckets`/`status` now read the tag cache too. This task replaces them and adds a combined-result test.

- [ ] **Step 1: Update the failing tests.** In `tests/test_auto_collections.py`, find `class TestGenreService(unittest.TestCase):` and REPLACE the entire class body (both existing methods) with:

```python
class TestGenreService(unittest.TestCase):
    def _svc(self):
        from pcgr.services.genres import GenreService
        return GenreService()

    def test_status_pending_until_in_both_caches(self):
        svc = self._svc()
        # 10 in both, 20 only genres, 30 in neither
        with mock.patch("pcgr.services.genres.load_genre_cache",
                        return_value={"10": ["Action"], "20": ["Action"]}), \
             mock.patch("pcgr.services.genres.load_tag_cache",
                        return_value={"10": ["Roguelike"]}), \
             mock.patch.object(svc, "_warm"):
            st = svc.status([10, 20, 30])
        self.assertEqual(st["total"], 3)
        self.assertEqual(st["categorized"], 1)   # only 10 is in BOTH
        self.assertEqual(st["pending"], 2)

    def test_get_buckets_returns_genres_then_tags(self):
        svc = self._svc()
        with mock.patch("pcgr.services.genres.load_genre_cache",
                        return_value={"10": ["Action", "Indie"], "20": ["Action"]}), \
             mock.patch("pcgr.services.genres.load_tag_cache",
                        return_value={"10": ["Roguelike"], "20": ["Roguelike"]}), \
             mock.patch.object(svc, "_warm"):
            r = svc.get_buckets([10, 20])
        self.assertEqual(r["status"], "ok")
        names = [b["name"] for b in r["collections"]]
        self.assertEqual(names, ["Action", "Roguelike"])  # genres first, then tags
```

- [ ] **Step 2: Run, verify FAIL.** Run: `python -m unittest tests.test_auto_collections -v`
Expected: FAIL (e.g. `AttributeError`/`TypeError` — `load_tag_cache` not imported in `pcgr.services.genres`, and `_warm` signature mismatch).

- [ ] **Step 3: Implement.** Replace the entire contents of `pcgr/services/genres.py` with:

```python
"""
Auto-collections by genre and curated tag.

Owns the genre and tag caches plus a polite background warmer (modeled on
NameService): when asked for buckets it returns whatever's cached now and kicks
off resolution of any uncached library appids, so cards fill in progressively
over the first run and load instantly afterward.  Buckets are built by the pure
``pcgr.genres.build_genre_buckets`` / ``build_tag_buckets`` (curated lists +
minimum size).  One warm pass resolves both tracks (genres from Steam, tags from
SteamSpy).

Steam only for now (Phase A).  App-internal — never writes Steam data.
"""

import threading
import time

from pcgr.config import get_setting, set_setting
from pcgr.genres import build_genre_buckets, build_tag_buckets
from pcgr.sources.store import (
    fetch_genres, fetch_tags,
    load_genre_cache, merge_genre_cache,
    load_tag_cache, merge_tag_cache,
)


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
        """Return genre cards followed by curated-tag cards for the given library
        appids, building from the caches and kicking off a background warm for
        any uncached genres/tags so the buckets fill in over time."""
        ids = self._ints(appids)
        genre_cache = load_genre_cache()
        tag_cache = load_tag_cache()
        self._warm(ids, genre_cache, tag_cache)
        collections = (build_genre_buckets(genre_cache, ids)
                       + build_tag_buckets(tag_cache, ids))
        return {"status": "ok", "collections": collections}

    def status(self, appids):
        """Progress for the 'Categorizing…' indicator.  A game counts as done
        only once it's in BOTH caches, so the bar doesn't read 100% while tags
        are still resolving."""
        ids = self._ints(appids)
        genre_cache = load_genre_cache()
        tag_cache = load_tag_cache()
        categorized = sum(1 for a in ids
                          if str(a) in genre_cache and str(a) in tag_cache)
        return {"status": "ok", "total": len(ids),
                "categorized": categorized, "pending": len(ids) - categorized}

    # ── Background warmer (both tracks) ───────────────────────────────────

    def _warm(self, appids, genre_cache, tag_cache):
        """Resolve missing genres (Steam) and tags (SteamSpy) for uncached appids
        on one daemon thread.  Polite (fixed delay per appid), flushes every 25
        hits per track so progress survives a crash, resumes across launches
        (caches persist).  One warmer at a time."""
        todo = [a for a in appids
                if str(a) not in genre_cache or str(a) not in tag_cache]
        if not todo:
            return
        with self._warm_lock:
            if self._warm_thread and self._warm_thread.is_alive():
                return

            def _worker():
                res_g, res_t = {}, {}
                for appid in todo:
                    key = str(appid)
                    if key not in genre_cache:
                        try:
                            g = fetch_genres(appid)
                        except Exception:
                            g = None
                        if g is not None:        # [] (no genres) is a real result
                            res_g[key] = g
                    if key not in tag_cache:
                        try:
                            t = fetch_tags(appid)
                        except Exception:
                            t = None
                        if t is not None:        # [] (no tags) is a real result
                            res_t[key] = t
                    if len(res_g) >= 25:
                        merge_genre_cache(res_g); res_g = {}
                    if len(res_t) >= 25:
                        merge_tag_cache(res_t); res_t = {}
                    time.sleep(0.20)             # ~5 req/s, gentle on the APIs
                if res_g:
                    merge_genre_cache(res_g)
                if res_t:
                    merge_tag_cache(res_t)

            t = threading.Thread(target=_worker, name="genre-tag-warmer", daemon=True)
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

- [ ] **Step 4: Run, verify PASS.** Run: `python -m unittest tests.test_auto_collections -v`
Expected: all OK.

- [ ] **Step 5: Lint.** Run: `python -m pyflakes pcgr/services/genres.py tests/test_auto_collections.py`
Expected: no output.

- [ ] **Step 6: Facade smoke check (no facade change, but confirm it still wires).** Run: `python -c "from pcgr import SteamRouletteAPI; a=SteamRouletteAPI(); print(a.get_auto_collections([367520]).get('status'))"`
Expected: prints `ok` (collections may be empty if 367520 isn't cached yet — no exception).

- [ ] **Step 7: Commit.**

```bash
git add pcgr/services/genres.py tests/test_auto_collections.py
git commit -m "feat(tags): GenreService warms + returns genres and tags combined"
```

---

## Task 4: Runtime verification (CDP)

**Files:** none (verification only).

- [ ] **Step 1: Kill strays and launch.**

```bash
powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | ? { \$_.CommandLine -like '*steam-roulette*' } | % { Stop-Process -Id \$_.ProcessId -Force -EA SilentlyContinue }; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | ? { \$_.CommandLine -like '*main.py*' } | % { Stop-Process -Id \$_.ProcessId -Force -EA SilentlyContinue }"
```

Then launch (background): `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222 python main.py`

- [ ] **Step 2: Enable + warm a tag-heavy sample via CDP.** Drive `window.pywebview.api` (port 9222), evaluating (await-promise):

```javascript
(async () => {
  const A = window.pywebview.api;
  await A.set_auto_collections_enabled(true);
  const sample = [367520, 1145360, 105600, 620, 1086940, 268910, 292030, 413150]; // metroidvania/roguelike/survival/etc.
  let st, buckets;
  for (let i = 0; i < 25; i++) {
    st = await A.get_auto_collection_status(sample);
    buckets = await A.get_auto_collections(sample);
    if (st.pending === 0) break;
    await new Promise(r => setTimeout(r, 1500));
  }
  return JSON.stringify({ status: st, names: (buckets.collections||[]).map(b => b.name+':'+b.count) });
})()
```

Expected: `status.pending === 0`; `names` includes genre buckets **and** tag buckets such as `Roguelike`, `Metroidvania`, `Survival` (exact set depends on the sample, but at least one curated tag must appear).

- [ ] **Step 3: Confirm progressive/persisted cache + safety.** In a shell:

```bash
test -f cache/tags.json && python -c "import json;d=json.load(open('cache/tags.json'));print('tag-cached appids:',len(d))"
git check-ignore cache/tags.json && echo "tags.json gitignored"
grep -rn "cloud-storage-namespace" pcgr/ | grep -iE "open\(.*[\"']w|save|write" || echo "no writes to Steam collections file"
```

Expected: `tags.json` exists with cached appids, is gitignored, and there are no writes to Steam's collections file.

- [ ] **Step 4: Restore dev state + close.** Via CDP: `await window.pywebview.api.set_auto_collections_enabled(false)`. Then kill the app processes (same PowerShell as Step 1).

---

## Self-review notes (already applied)
- **Spec coverage:** same toggle/no new toggle (no facade/frontend change — Task 3 routes through existing path); SteamSpy top-N source (`fetch_tags`, Task 2); curated `{canonical:[synonyms]}` + synonym grouping + min-size (`build_tag_buckets`, Task 1); genres-then-tags order + multi-membership (Task 3 + Task 1 tests); progressive fill + dual-cache `status` (Task 3); tag cache mirrors genre cache (Task 2); app-internal/gitignored/no Steam writes (Task 4). ✓
- **Type consistency:** `build_tag_buckets`/`fetch_tags`/`load_tag_cache`/`save_tag_cache`/`merge_tag_cache`/`TAGS_CACHE`/`CURATED_TAGS`/`_TAG_CANON` used identically across tasks; `_warm(ids, genre_cache, tag_cache)` signature matches its call in `get_buckets`. ✓
- **No placeholders:** every step has concrete code, exact anchors, and expected output. ✓
- **No-overlap guard:** `TestCuratedTagsIntegrity` asserts each term maps to one canonical and tags don't collide with genre names. ✓
