# Playtime exclusion filter — design

## Context
Players often want the roulette to stop landing on games they've already
sunk dozens of hours into — they're looking for something *new* to play. Today
the only way to remove a game from spins is the manual per-game exclude. This
adds a single setting that hides any game whose recorded playtime exceeds a
user-chosen threshold, across every platform where playtime is known.

A useful side effect of the chosen semantics ("strictly over X hours"): setting
the threshold to **0** turns it into a **backlog mode** — spin only games you've
never played.

## Requirements

### Behavior
- A new control in **Settings**, alongside the existing dedup / edition filters:
  a toggle **"Hide games I've played more than `[N]` hours"** plus a numeric
  input that **accepts decimals** (e.g. `2.5`).
- When enabled, any game with **known** playtime **> `N × 60` minutes** is hidden
  from **all** spins — collections, Whole Library, and Leave It To Fate — on
  every platform where playtime is known (Steam from `localconfig.vdf`; GOG/Epic
  from GOG Galaxy enrichment).
- Comparison is done **in minutes** (`N × 60`) so the math is exact for decimals
  and `N = 0` cleanly means "any recorded playtime at all."
- Games with **unknown** playtime are **always kept** (we can't threshold what we
  can't measure).
- Stacks additively with the other filters (manual excludes, cross-platform
  dedup, edition preference) rather than replacing any of them.

### Non-goals
- Not platform-restricted to Steam (despite the original framing) — it applies
  wherever playtime is available.
- Does not mutate the persistent manual-exclude list; it is a live filter only.
- No per-collection override; it's a single global threshold.

## Architecture
Fits the existing filter pipeline. The dedup/edition filters already compute
per-platform "hide these IDs" lists that `get_all_filters()` returns and the
frontend applies in one combined pass; the playtime filter is the same shape.

- **Settings:** persist `playtime_filter_enabled` (bool) and
  `playtime_max_hours` (number) via the existing `get_setting` / `set_setting`
  in `pcgr/config.py`.
- **`FilterService` (`pcgr/services/filters.py`)** gains
  `get_playtime_filter()` → `{status, <per-platform exclude-id lists>, counts}`,
  identical in shape to `get_duplicate_filter` / `get_edition_filter`:
  - Steam exclude-ids come from `SteamLauncher`'s parsed `_playtimes`
    ({appid: minutes}) — expose what's needed via the launcher (e.g. a
    `playtimes` accessor) rather than reaching into a private attribute.
  - GOG / Epic exclude-ids come from each launcher's `get_games()` entries that
    carry `playtime_minutes` (set by Galaxy enrichment).
  - It is **folded into `get_all_filters()`** so the frontend still makes a
    single call and merges dedup + edition + playtime into one hide-set.
- **Facade (`pcgr/api.py`)** delegators: `get_playtime_settings` /
  `set_playtime_settings` (thin pass-throughs to `FilterService`).
- **Frontend (`web/`):** add the toggle + numeric input to the Settings filters
  block, wire to the new settings calls, and include the playtime excludes when
  building the combined hide-set the spin logic already applies.

## Edge cases
- **Decimal input / minutes math:** store the raw hours value; compare
  `playtime_minutes > round(hours * 60)`.
- **`N = 0`:** hides every game with > 0 recorded minutes → backlog mode.
- **Invalid / empty input:** treat a blank or non-numeric value as "filter
  effectively off"; don't crash. The toggle is the real on/off; the number just
  parameterizes it.
- **Unknown playtime:** never excluded.
- **Non-Steam shortcuts:** no playtime data → never excluded.

## Verification (runtime)
- Enable the filter with a mid-range threshold; confirm high-playtime games
  disappear from spins across the Steam, GOG and Epic tabs and from Leave It To
  Fate, while low/zero-playtime games remain.
- Set `N = 0`; confirm only never-played games are spinnable.
- Toggle the filter off; confirm everything returns.
- Confirm a game with unknown playtime is never hidden by this filter.
- Drive via CDP against the running app (kill zombie WebView2/python first),
  exercising `get_all_filters()` and the new settings methods.

## Out of scope
- Writing genres or any data back to Steam.
- A separate "minimum playtime" / "only games I've played" inverse filter
  (backlog mode already covers the common inverse via `N = 0`).
