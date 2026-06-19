# Playtime Exclusion Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Settings toggle that hides games played more than N hours (decimals allowed) from every spin, across all platforms where playtime is known.

**Architecture:** A new filter that plugs into the existing `get_all_filters()` pipeline (the same shape as the dedup and edition filters). The backend computes per-platform "hide these IDs" lists from known playtimes; the frontend merges them into the combined hide-set it already applies. No data is written back to Steam.

**Tech Stack:** Python 3.13 (`pcgr` package), pywebview js_api, vanilla JS/HTML frontend in `web/`. This repo has **no test framework**: pure backend logic is tested with the stdlib `unittest` module (runnable via `python -m unittest`), and end-to-end behavior is verified by running the app and driving it over the Chrome DevTools Protocol (CDP), exactly as the rest of the codebase is verified.

**Spec:** `docs/superpowers/specs/2026-06-19-playtime-exclusion-filter-design.md`

---

## Key facts the implementer needs

- **Playtime is already available.** `SteamLauncher` parses `localconfig.vdf` into `self._playtimes` (`{appid_int: minutes_int}`). GOG/Epic game dicts already carry `playtime_minutes` (set by Galaxy enrichment in `pcgr/sources/galaxy.py`).
- **The filter pipeline.** `FilterService` (`pcgr/services/filters.py`) already exposes `get_duplicate_filter`, `get_edition_filter`, and `get_all_filters` — each returns per-platform exclude-id lists keyed by the platform ids in `PLATFORMS` (`steam`, `gog`, `epic`) plus `_GOG_INTEGRATED_PREFIXES` (`battlenet`, `origin`, `uplay`). Game ids look like `steam_<appid>`, `gog_<id>`, `epic_<id>`, `battlenet_<id>`, etc.
- **Frontend merge point.** `web/app.js` holds `dedupExcludes` / `editionExcludes` globals, parses backend results with `_parseDedup` / `_parseEdition`, primes both from one `api.get_all_filters()` call in `getAllExcludes()` (app.js ~line 93), and merges them into per-platform `Set`s. `invalidateAllExcludes()` resets the caches.
- **Settings load + handlers.** `openSettings()` (app.js:944) calls `refreshDedupSettings()` (app.js:1345) etc. to populate controls. Change handlers are registered once near app.js:2968 (the `dedup-enabled` handler).
- **Run the app:** `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222 python main.py`. Kill stray `msedgewebview2.exe`/`python main.py` processes before each launch (orphans hold the debug port and cause false readings).

---

## File structure

- **Create** `tests/test_playtime_filter.py` — stdlib `unittest` tests for the pure exclude helper and the `FilterService` glue (with stub launchers + patched settings).
- **Modify** `pcgr/services/filters.py` — add module-level `playtime_excludes()` pure helper; add `get_playtime_settings`, `set_playtime_settings`, `get_playtime_filter`; fold playtime into `get_all_filters`.
- **Modify** `pcgr/launchers/steam.py` — add a `playtimes` read-only property exposing `_playtimes`.
- **Modify** `pcgr/api.py` — add `get_playtime_settings` / `set_playtime_settings` delegators.
- **Modify** `web/index.html` — add a "Playtime" Settings section (toggle + number input).
- **Modify** `web/app.js` — add `playtimeExcludes` global + `_parsePlaytime`; fold into `getAllExcludes` priming/merge and `invalidateAllExcludes`; add `refreshPlaytimeSettings()` + change handlers.

---

## Task 1: Pure playtime-exclude helper (backend logic + unit test)

**Files:**
- Modify: `pcgr/services/filters.py`
- Test: `tests/test_playtime_filter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_playtime_filter.py`:

```python
import os
import sys
import unittest

# Make the repo root importable so `import pcgr...` works when run from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcgr.services.filters import playtime_excludes


class TestPlaytimeExcludes(unittest.TestCase):
    def test_strictly_over_threshold_is_excluded(self):
        games = {"steam": [
            {"id": "steam_1", "playtime_minutes": 200},   # over 180 -> excluded
            {"id": "steam_2", "playtime_minutes": 180},   # equal     -> kept
            {"id": "steam_3", "playtime_minutes": 60},     # under     -> kept
        ]}
        out = playtime_excludes(games, 180)
        self.assertEqual(out["steam"], ["steam_1"])

    def test_zero_threshold_is_backlog_mode(self):
        games = {"gog": [
            {"id": "gog_a", "playtime_minutes": 1},        # any play   -> excluded
            {"id": "gog_b", "playtime_minutes": 0},        # never      -> kept
        ]}
        out = playtime_excludes(games, 0)
        self.assertEqual(out["gog"], ["gog_a"])

    def test_unknown_or_missing_playtime_is_kept(self):
        games = {"epic": [
            {"id": "epic_a"},                               # missing    -> kept
            {"id": "epic_b", "playtime_minutes": None},     # None       -> kept
            {"id": "epic_c", "playtime_minutes": 0},        # zero       -> kept
        ]}
        out = playtime_excludes(games, 60)
        self.assertEqual(out["epic"], [])

    def test_each_platform_handled_independently(self):
        games = {
            "steam": [{"id": "steam_1", "playtime_minutes": 999}],
            "gog":   [{"id": "gog_1",   "playtime_minutes": 10}],
        }
        out = playtime_excludes(games, 60)
        self.assertEqual(out, {"steam": ["steam_1"], "gog": []})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_playtime_filter -v`
Expected: FAIL with `ImportError: cannot import name 'playtime_excludes'`.

- [ ] **Step 3: Write the helper**

In `pcgr/services/filters.py`, add this module-level function **after the imports and before the `FilterService` class**:

```python
def playtime_excludes(per_platform_games, threshold_minutes):
    """For a mapping of platform -> [game dicts], return a dict mapping each
    platform to the list of game ids whose KNOWN playtime is strictly greater
    than threshold_minutes.

    Games with missing / None / zero playtime are never excluded — we can't
    threshold what we can't measure, and a threshold of 0 ("backlog mode")
    hides anything with any recorded playtime while leaving never-played and
    unknown games alone.
    """
    out = {}
    for platform, games in per_platform_games.items():
        excluded = []
        for g in games:
            pm = g.get("playtime_minutes") or 0
            if isinstance(pm, (int, float)) and pm > threshold_minutes:
                gid = g.get("id")
                if gid:
                    excluded.append(gid)
        out[platform] = excluded
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_playtime_filter -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_playtime_filter.py pcgr/services/filters.py
git commit -m "feat(playtime): pure per-platform playtime-exclude helper"
```

---

## Task 2: Steam playtimes accessor

**Files:**
- Modify: `pcgr/launchers/steam.py`

- [ ] **Step 1: Add the property**

In `pcgr/launchers/steam.py`, find the existing accessor properties (`steam_path`, `collections`, `shortcuts`, `collections_path`) and add alongside them:

```python
    @property
    def playtimes(self):
        """{appid_int: minutes_int} parsed from localconfig.vdf — used by the
        playtime filter."""
        return self._playtimes
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `python -c "from pcgr.launchers.steam import SteamLauncher; print(hasattr(SteamLauncher, 'playtimes'))"`
Expected: prints `True`.

- [ ] **Step 3: Commit**

```bash
git add pcgr/launchers/steam.py
git commit -m "feat(playtime): expose SteamLauncher.playtimes accessor"
```

---

## Task 3: FilterService settings + filter method + get_all_filters fold-in

**Files:**
- Modify: `pcgr/services/filters.py`
- Test: `tests/test_playtime_filter.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_playtime_filter.py` (inside the file, after the existing class):

```python
from unittest import mock
from pcgr.services.filters import FilterService


class _StubSteam:
    def __init__(self, playtimes):
        self.playtimes = playtimes


class _StubLibrary:
    def __init__(self, games):
        self._games = games
    def get_games(self):
        return {"status": "ok", "games": self._games}


class TestPlaytimeFilterService(unittest.TestCase):
    def _service(self, playtimes, gog_games, epic_games):
        return FilterService(
            steam=_StubSteam(playtimes),
            gog=_StubLibrary(gog_games),
            epic=_StubLibrary(epic_games),
            names=None,
        )

    def test_disabled_returns_empty(self):
        svc = self._service({123: 9999}, [], [])
        with mock.patch("pcgr.services.filters.get_setting") as gs:
            gs.side_effect = lambda k, d=None: {"playtime_filter_enabled": False}.get(k, d)
            out = svc.get_playtime_filter()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["steam"], [])

    def test_enabled_filters_each_platform(self):
        svc = self._service(
            playtimes={123: 200 * 60, 456: 10},  # 123 = 200h, 456 = 10min
            gog_games=[{"id": "gog_1", "platform": "gog", "playtime_minutes": 5000}],
            epic_games=[{"id": "epic_1", "platform": "epic", "playtime_minutes": 30}],
        )
        settings = {"playtime_filter_enabled": True, "playtime_max_hours": 50}
        with mock.patch("pcgr.services.filters.get_setting") as gs:
            gs.side_effect = lambda k, d=None: settings.get(k, d)
            out = svc.get_playtime_filter()
        self.assertEqual(out["steam"], ["steam_123"])  # 200h > 50h
        self.assertEqual(out["gog"], ["gog_1"])         # ~83h > 50h
        self.assertEqual(out["epic"], [])               # 30min kept
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_playtime_filter -v`
Expected: FAIL with `AttributeError: 'FilterService' object has no attribute 'get_playtime_filter'`.

- [ ] **Step 3: Add settings + filter methods to FilterService**

In `pcgr/services/filters.py`, add these methods to the `FilterService` class (place them after `set_edition_preference`, before `get_edition_filter`):

```python
    # ── Playtime filter ───────────────────────────────────────────────────

    def get_playtime_settings(self):
        """Return the user's playtime-exclusion settings."""
        return {
            "status":    "ok",
            "enabled":   bool(get_setting("playtime_filter_enabled", False)),
            "max_hours": get_setting("playtime_max_hours", 0),
        }

    def set_playtime_settings(self, enabled, max_hours):
        """Persist the playtime-exclusion toggle and hour threshold.  A blank or
        non-numeric threshold is coerced to 0 (which, while enabled, becomes
        backlog mode — hide anything with any recorded playtime)."""
        try:
            hours = float(max_hours)
            if hours < 0:
                hours = 0
        except (TypeError, ValueError):
            hours = 0
        set_setting("playtime_filter_enabled", bool(enabled))
        set_setting("playtime_max_hours", hours)
        return self.get_playtime_settings()

    def get_playtime_filter(self, _gog_games=None, _epic_games=None):
        """Per-platform game ids to hide because their known playtime exceeds the
        user's threshold.  Same shape as get_duplicate_filter.  `_gog_games` /
        `_epic_games` let get_all_filters() share one library fetch.  Returns all
        empty lists when the filter is disabled."""
        all_platform_ids = list(PLATFORMS) + list(_GOG_INTEGRATED_PREFIXES)
        empty = {pid: [] for pid in all_platform_ids}
        if not bool(get_setting("playtime_filter_enabled", False)):
            return {"status": "ok", **empty, "counts": {}}

        try:
            threshold = round(float(get_setting("playtime_max_hours", 0)) * 60)
        except (TypeError, ValueError):
            threshold = 0

        # Steam: synthesise {id, playtime_minutes} from the parsed playtimes map.
        steam_games = [{"id": f"steam_{a}", "playtime_minutes": m}
                       for a, m in (self.steam.playtimes or {}).items()]

        # GOG result already carries playtime_minutes; split by platform so the
        # integrated launchers (battlenet/origin/uplay) are keyed correctly.
        gog_all   = (_gog_games if _gog_games is not None
                     else self.gog.get_games().get("games", []))
        epic_list = (_epic_games if _epic_games is not None
                     else self.epic.get_games().get("games", []))
        per_platform = {
            "steam":     steam_games,
            "gog":       [g for g in gog_all if g["platform"] == "gog"],
            "epic":      epic_list,
            "battlenet": [g for g in gog_all if g["platform"] == "battlenet"],
            "origin":    [g for g in gog_all if g["platform"] == "origin"],
            "uplay":     [g for g in gog_all if g["platform"] == "uplay"],
        }

        excludes = playtime_excludes(per_platform, threshold)
        out = {"status": "ok"}
        counts = {}
        for pid in all_platform_ids:
            out[pid] = excludes.get(pid, [])
            counts[f"{pid}_hidden"] = len(out[pid])
        out["counts"] = counts
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_playtime_filter -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Fold playtime into get_all_filters**

In `pcgr/services/filters.py`, find `get_all_filters` (its body computes `dedup_on` / `edition_on` and conditionally fetches `gog` / `epic`). Replace the method body with:

```python
    def get_all_filters(self):
        """Compute cross-platform dedup, same-platform edition, AND playtime
        excludes, fetching each platform's library only once and sharing it
        across all three filters."""
        dedup_on    = self.get_dedup_settings()["enabled"]
        edition_on  = get_setting("edition_preference", "both") in ("enhanced", "original")
        playtime_on = bool(get_setting("playtime_filter_enabled", False))
        # Only fetch the GOG / Epic libraries if a filter actually needs them.
        if dedup_on or edition_on or playtime_on:
            gog  = self.gog.get_games().get("games", [])
            epic = self.epic.get_games().get("games", [])
        else:
            gog, epic = [], []
        return {
            "status":   "ok",
            "dedup":    self.get_duplicate_filter(_gog_games=gog, _epic_games=epic),
            "edition":  self.get_edition_filter(_gog_games=gog, _epic_games=epic),
            "playtime": self.get_playtime_filter(_gog_games=gog, _epic_games=epic),
        }
```

- [ ] **Step 6: Verify nothing regressed**

Run: `python -m unittest tests.test_playtime_filter -v && python -m pyflakes pcgr/services/filters.py`
Expected: tests PASS, pyflakes prints nothing.

- [ ] **Step 7: Commit**

```bash
git add pcgr/services/filters.py tests/test_playtime_filter.py
git commit -m "feat(playtime): FilterService settings + filter + get_all_filters fold-in"
```

---

## Task 4: Facade delegators

**Files:**
- Modify: `pcgr/api.py`

- [ ] **Step 1: Add the delegators**

In `pcgr/api.py`, in the Filters section (after the `get_all_filters` delegator), add:

```python
    def get_playtime_settings(self):           return self.filters.get_playtime_settings()
    def set_playtime_settings(self, enabled, max_hours):
        return self.filters.set_playtime_settings(enabled, max_hours)
```

- [ ] **Step 2: Verify the facade still constructs**

Run: `python -c "from pcgr import SteamRouletteAPI; a=SteamRouletteAPI(); print(a.get_playtime_settings())"`
Expected: prints `{'status': 'ok', 'enabled': False, 'max_hours': 0}` (values reflect your config; enabled should be False on a fresh setting).

- [ ] **Step 3: Commit**

```bash
git add pcgr/api.py
git commit -m "feat(playtime): api facade delegators for playtime settings"
```

---

## Task 5: Frontend — merge playtime into the combined exclude set

**Files:**
- Modify: `web/app.js`

- [ ] **Step 1: Add the global + parser**

In `web/app.js`, find `let editionExcludes = null;` (~line 59) and add below it:

```javascript
let playtimeExcludes = null;
```

Then find `function _parseEdition(r) {` and add this function right after it:

```javascript
function _parsePlaytime(r) {
  return (r && r.status === 'ok')
    ? { steam:     new Set(r.steam),
        gog:       new Set(r.gog),
        epic:      new Set(r.epic),
        battlenet: new Set(r.battlenet || []),
        origin:    new Set(r.origin    || []),
        uplay:     new Set(r.uplay     || []) }
    : { steam: new Set(), gog: new Set(), epic: new Set(),
        battlenet: new Set(), origin: new Set(), uplay: new Set() };
}

async function getPlaytimeExcludes() {
  if (playtimeExcludes !== null) return playtimeExcludes;
  if (!api) { playtimeExcludes = _parsePlaytime(null); return playtimeExcludes; }
  playtimeExcludes = _parsePlaytime(await api.get_playtime_filter());
  return playtimeExcludes;
}
```

- [ ] **Step 2: Fold playtime into getAllExcludes**

In `web/app.js`, in `getAllExcludes()`, update the priming guard and parsing. Replace:

```javascript
  if (api && dedupExcludes === null && editionExcludes === null) {
```

with:

```javascript
  if (api && dedupExcludes === null && editionExcludes === null && playtimeExcludes === null) {
```

Inside the same `try` block, after `editionExcludes = _parseEdition(r.edition);`, add:

```javascript
        playtimeExcludes = _parsePlaytime(r.playtime);
```

Then replace the final return of `getAllExcludes()`:

```javascript
  const [d, e] = await Promise.all([getDedupExcludes(), getEditionExcludes()]);
  return {
    steam:     new Set([...d.steam,     ...(e.steam     || [])]),
    gog:       new Set([...d.gog,       ...(e.gog       || [])]),
    epic:      new Set([...d.epic,      ...(e.epic      || [])]),
    battlenet: new Set([...(d.battlenet || [])]),
    origin:    new Set([...(d.origin    || [])]),
    uplay:     new Set([...(d.uplay     || [])]),
  };
```

with:

```javascript
  const [d, e, p] = await Promise.all([
    getDedupExcludes(), getEditionExcludes(), getPlaytimeExcludes(),
  ]);
  return {
    steam:     new Set([...d.steam,     ...(e.steam || []), ...p.steam]),
    gog:       new Set([...d.gog,       ...(e.gog   || []), ...p.gog]),
    epic:      new Set([...d.epic,      ...(e.epic  || []), ...p.epic]),
    battlenet: new Set([...(d.battlenet || []), ...p.battlenet]),
    origin:    new Set([...(d.origin    || []), ...p.origin]),
    uplay:     new Set([...(d.uplay     || []), ...p.uplay]),
  };
```

- [ ] **Step 3: Reset the cache in invalidateAllExcludes**

In `web/app.js`, replace:

```javascript
function invalidateAllExcludes()  { dedupExcludes = null; editionExcludes = null; }
```

with:

```javascript
function invalidateAllExcludes()  { dedupExcludes = null; editionExcludes = null; playtimeExcludes = null; }
```

- [ ] **Step 4: Sanity-check the JS parses**

Run: `node --check web/app.js`
Expected: no output (exit 0). If `node` is unavailable, skip — the CDP launch in Task 7 will surface syntax errors.

- [ ] **Step 5: Commit**

```bash
git add web/app.js
git commit -m "feat(playtime): merge playtime excludes into the combined hide-set"
```

---

## Task 6: Frontend — Settings control + wiring

**Files:**
- Modify: `web/index.html`
- Modify: `web/app.js`

- [ ] **Step 1: Add the Settings section markup**

In `web/index.html`, find the Edition Preference section (`<section class="settings-section">` containing `<h3>Edition Preference</h3>`). Immediately **before** that `<section>`, insert:

```html
      <section class="settings-section">
        <h3>Playtime</h3>
        <label class="dedup-toggle">
          <input type="checkbox" id="playtime-enabled">
          <span>Hide games I've played more than
            <input type="number" id="playtime-hours" min="0" step="0.5"
                   inputmode="decimal" style="width:64px;"> hours</span>
        </label>
        <div class="edition-pref-label" style="margin-top:6px;">
          Applies to every spin, on any platform where playtime is known.
          Set to <strong>0</strong> to spin only games you've never played.
        </div>
      </section>
```

- [ ] **Step 2: Add the load-on-open function**

In `web/app.js`, find `async function refreshDedupSettings() {` (~line 1345) and add this function right before it:

```javascript
async function refreshPlaytimeSettings() {
  if (!api) return;
  const s = await api.get_playtime_settings();
  if (s.status !== 'ok') return;
  document.getElementById('playtime-enabled').checked = !!s.enabled;
  document.getElementById('playtime-hours').value =
    (s.max_hours === null || s.max_hours === undefined) ? '' : s.max_hours;
}
```

- [ ] **Step 3: Call it when Settings opens**

In `web/app.js`, in `openSettings()` (~line 944), find the line `await refreshDedupSettings();` and add immediately after it:

```javascript
  await refreshPlaytimeSettings();
```

- [ ] **Step 4: Register the change handlers**

In `web/app.js`, find the dedup toggle handler (`document.getElementById('dedup-enabled').addEventListener(...)`, ~line 2968). Add this block immediately after that handler's closing `});`:

```javascript
  // Playtime filter: toggle + threshold input
  async function _savePlaytime() {
    if (!api) return;
    const enabled = document.getElementById('playtime-enabled').checked;
    const hours   = document.getElementById('playtime-hours').value;
    await api.set_playtime_settings(enabled, hours);
    invalidateAllExcludes();
  }
  document.getElementById('playtime-enabled').addEventListener('change', _savePlaytime);
  document.getElementById('playtime-hours').addEventListener('change', _savePlaytime);
```

- [ ] **Step 5: Sanity-check the JS parses**

Run: `node --check web/app.js`
Expected: no output (exit 0). If `node` is unavailable, skip.

- [ ] **Step 6: Commit**

```bash
git add web/index.html web/app.js
git commit -m "feat(playtime): Settings toggle + threshold input wired to backend"
```

---

## Task 7: Runtime verification (CDP)

**Files:** none (verification only)

- [ ] **Step 1: Kill stray processes and launch**

```bash
# PowerShell: kill orphaned webview/python so CDP connects to THIS instance
powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | ? { \$_.CommandLine -like '*steam-roulette*' } | % { Stop-Process -Id \$_.ProcessId -Force -EA SilentlyContinue }; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | ? { \$_.CommandLine -like '*main.py*' } | % { Stop-Process -Id \$_.ProcessId -Force -EA SilentlyContinue }"
```

Then launch (background): `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222 python main.py`

- [ ] **Step 2: Verify settings round-trip + filter via CDP**

Drive `window.pywebview.api` over CDP (port 9222), evaluating:

```javascript
(async () => {
  const before = await window.pywebview.api.get_playtime_settings();
  await window.pywebview.api.set_playtime_settings(true, 2.5);
  const after  = await window.pywebview.api.get_playtime_settings();
  const filt   = await window.pywebview.api.get_all_filters();
  return JSON.stringify({ before, after, playtimeCounts: filt.playtime.counts });
})()
```

Expected: `after` shows `{enabled: true, max_hours: 2.5}`; `filt.playtime.counts` has per-platform `*_hidden` numbers (non-zero on a library with games over 2.5h). Then set it back: `set_playtime_settings(false, 0)`.

- [ ] **Step 3: Verify the Settings UI control**

Via CDP, open Settings (click the gear / call `openSettings()` if exposed, or evaluate the same), then confirm:

```javascript
(() => {
  const t = document.getElementById('playtime-enabled');
  const h = document.getElementById('playtime-hours');
  return JSON.stringify({ togglePresent: !!t, inputPresent: !!h,
                          checked: t && t.checked, value: h && h.value });
})()
```

Expected: both controls present and reflecting the persisted state.

- [ ] **Step 4: Verify end-to-end hiding (spot check)**

With the filter enabled at a low threshold (e.g. 1 hour) and a Steam collection/Whole Library that contains a >1h game, confirm via CDP that `get_all_filters().playtime.steam` contains that game's `steam_<appid>` id, and that after disabling it the list is empty.

- [ ] **Step 5: Restore dev state + close**

Set `set_playtime_settings(false, 0)` (so the dev config isn't left with the filter on), kill the app processes (same PowerShell as Step 1).

- [ ] **Step 6: Final commit (if any cleanup was needed)**

```bash
git add -A
git commit -m "test(playtime): runtime CDP verification notes" --allow-empty
```

---

## Self-review notes (already applied)

- **Spec coverage:** scope across platforms (Task 3 splits steam/gog/epic/integrated), decimals + minutes math (`round(hours*60)`, Task 3), strictly-over + unknown-kept + N=0 backlog (Task 1 tests + helper), Settings toggle + number (Task 6), stacks with other filters (Task 5 merges into the union), one combined call (`get_all_filters` fold-in, Task 3), never writes Steam data (no Steam writes anywhere). ✓
- **Type consistency:** `playtime_excludes` (module fn) vs `get_playtime_filter` (method) used consistently; settings keys `playtime_filter_enabled` / `playtime_max_hours` identical across backend + frontend; frontend `_parsePlaytime` returns all six platform keys to match `_parseDedup`. ✓
- **No placeholders:** every step has concrete code and exact insertion anchors. ✓
