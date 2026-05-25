# Good morning — Epic OAuth scaffolding overnight build

Everything you need to wake up to. Below: what got built, the one-time OAuth
dance to actually wire it up, what to test, and known limitations.

## What was built

### New backend modules
- **`epic_auth.py`** — OAuth flow handler. Uses the public Epic Launcher
  credentials (same ones Legendary/Heroic use; stable for 4+ years). Refresh
  tokens stored encrypted on disk via **Windows DPAPI** — they're tied to your
  Windows user and only readable by us, on this machine. Stdlib only.
- **`epic_api.py`** — Epic library API client. Fetches your owned assets and
  resolves their titles via the catalog API. Filters out engines, tools, and
  redistributables. Returns games in our standard `{id, raw_id, name,
  platform, source, app_name}` shape so the frontend treats them just like
  GOG/Galaxy games.

### Backend wiring (`backend.py`)
- **`get_setting()` / `set_setting()`** — generic key/value persistence in
  `config.json` (used here for `epic_source`, available for any future toggle).
- **`get_epic_games()` refactored** to dispatch by `epic_source` setting:
  - `'oauth'` → Epic's API (full library, fast, no Galaxy needed)
  - `'galaxy'` → Galaxy DB (current default — full library if Galaxy+Epic
    integration is set up)
  - automatic fallback to local manifests (installed only) if both yield nothing
- **1-hour disk cache** (`cache/epic_library.json`) for OAuth library results
  so reloads are instant. Bypassed when you click Reload manually.
- **New API methods** on `SteamRouletteAPI`:
  - `get_epic_source()` / `set_epic_source(source)`
  - `epic_oauth_url()` — returns the Epic login URL
  - `epic_oauth_complete(code)` — exchanges code for tokens
  - `epic_oauth_status()` — connected? who?
  - `epic_oauth_disconnect()` — wipe tokens
  - `open_external_url(url)` — opens browser to the login page
- **`launch_epic_game()` expanded** to take an optional `app_name` argument.
  For OAuth-sourced games the AppName comes from the catalog response, so we
  launch via Epic's URI scheme directly (skipping Galaxy). Falls back to
  Galaxy URI when AppName isn't available.

### Frontend
- **New Settings section: "Epic Library Source"** — radio buttons for
  Galaxy vs Direct Epic, plus Connect / Disconnect buttons. Shows current
  connection status with the connected user's display name.
- **OAuth modal** — 3-step walkthrough:
  1. Click "Open Login Page" → opens Epic login in your default browser
  2. After login, copy the authorization code (or the whole JSON blob —
     both work, we parse either)
  3. Paste in the textarea, click Connect → tokens stored, modal closes
- **Auto-flow**: switching the radio to "Direct Epic" while not yet
  connected automatically pops the OAuth modal.

## How to actually connect (one-time, ~30 seconds)

1. Launch the app: `pythonw main.py`
2. Click the ☰ menu → **Settings**
3. Under **Epic Library Source**, click the **Direct from Epic Games** radio.
   The Connect modal should pop automatically.
4. Click **Open Login Page** → your browser opens
5. Log in to Epic Games normally
6. You'll land on a page showing JSON like:
   ```json
   {"redirectUrl":"...","authorizationCode":"abc123def456...","sid":null}
   ```
7. Copy the whole JSON blob (Ctrl+A, Ctrl+C) or just the `authorizationCode`
   value — either works
8. Paste into the textarea, click **Connect**
9. Should say "Connected as <your-displayName>!" and close
10. Click the **Epic** tab → your full 196-game library should appear

If anything misbehaves: leave the radio on "GOG Galaxy" and tell me what
broke. The Galaxy path is untouched and still works.

## Known limitations / where to look if things break

- **`epic_auth.py` — DPAPI**: If you ever see "CryptUnprotectData failed"
  errors, the encrypted token file got tied to a different Windows user
  profile or got corrupted. Delete `cache/epic_tokens.bin` and reconnect.
- **`epic_api.py` — `_is_game()`**: Heuristic filter for what counts as a
  game vs an engine/SDK. Epic's catalog tagging is inconsistent on old
  entries, so a few non-games may sneak through (Unreal Engine variants,
  some asset packs). If you see junk in the library list, that's the
  function to tighten.
- **Refresh tokens last ~30 days**: if you don't use the app for a long
  time, you'll need to reconnect. The UI will prompt you when that happens
  (it surfaces `auth_required` from the backend).
- **Token rotation**: Epic rotates the refresh token on every refresh, and
  the code already overwrites the stored blob on each refresh. If you ever
  run two instances of the app at the same time, you could race-clobber a
  refresh — unlikely to matter.

## Files touched

```
backend.py          (imports, EPIC_LIB_CACHE constant, get/set_setting helpers,
                     get_epic_games refactor + dispatch, _get_epic_games_oauth,
                     _get_epic_games_manifests, all the new API methods,
                     launch_epic_game expanded for OAuth source)
epic_auth.py        (NEW)
epic_api.py         (NEW)
web/index.html      (OAuth modal, Settings "Epic Library Source" section)
web/style.css       (modal + source-picker styles)
web/app.js          (refreshEpicSettings, handleEpicSourceChange,
                     disconnectEpic, OAuth modal helpers, init() wiring,
                     launch_epic_game call updated to pass app_name)
WAKEUP.md           (this file)
```

## What I could not test without your account

- The OAuth handshake itself (needs you to log in with Epic credentials)
- Real Epic API calls (needs a valid access token)
- The Settings UI with a real connected account
- The full end-to-end flow: switch radio → connect → see 196 games

What I DID verify works:
- All Python modules import cleanly
- DPAPI encrypt/decrypt round-trips correctly
- Token storage round-trips through DPAPI + disk
- Settings persist correctly in `config.json`
- `epic_oauth_url()` returns a well-formed login URL
- `launch_epic_game()` accepts the new `(raw_id, source, app_name)` signature
- The app boots without Python errors

## If you want to revert any of this

The Epic OAuth path is gated entirely behind the `epic_source` setting. As
long as you leave it on `galaxy` (the default), nothing changes from your
current working setup. The Galaxy DB code path is completely untouched.

Sleep well.
