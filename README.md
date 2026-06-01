# 🎲 PC Game Roulette

Can't decide what to play? Let fate pick. **PC Game Roulette** spins a
slot-machine wheel across your **Steam, GOG, Epic, and RetroArch** libraries and
lands on a game for you to play. Pick a platform, optionally narrow to a
collection or console, hit **SPIN** — or use **Leave It To Fate** to spin across
*everything* at once.

A single, portable Windows app. No account sign-up required to get started.

---

## Download & install

1. Go to the [**Releases**](../../releases/latest) page and download
   **`PC-Game-Roulette-Setup.exe`**.
2. Run it. Windows may show a blue **"Windows protected your PC"** screen,
   because the app isn't code-signed (a certificate costs money and this is a
   hobby project). Click **More info → Run anyway**.
   - **Antivirus may flag it as a false positive** (e.g. Malwarebytes reporting
     a generic `Malware.AI.*` verdict). This is a known quirk of apps built with
     **PyInstaller** — the single-exe bootloader that unpacks the app at launch
     looks, to a heuristic scanner, like the self-extracting behavior some
     malware uses. There is no actual malware here; the full source is in this
     repo and you can build the exe yourself (see *Building from source*). If
     your AV quarantines it, choose **Restore / Allow** for the file, and
     consider reporting the false positive to your AV vendor so they whitelist it.
3. Follow the wizard. It installs **per-user** (no admin/UAC prompt) and sets up
   the **Microsoft Edge WebView2 runtime** automatically if you don't already
   have it (most up-to-date Windows 10/11 PCs do).
4. Launch it from the Start menu.

> Upgrading from an older version? Just run the new installer — it upgrades in
> place and your settings carry over.

---

## First launch

The first time you open the app, a short **welcome** walks you through the one
optional setup step (the Steam library — see below). Your launchers are detected
automatically:

| Launcher | How it's detected | Notes |
|---|---|---|
| **Steam** | Automatically | Shows installed games + your Collections. See below to add your *full* owned library. |
| **GOG** | Requires **GOG Galaxy** installed | Reads your owned library and tags from Galaxy. |
| **Epic** | **GOG Galaxy** Epic integration, *or* connect directly | Settings → *Epic Library Source*. |
| **RetroArch** | Automatically, if installed | Each system playlist becomes its own category, with local box art. |

Only the launchers you actually have show up as tabs.

### Getting your full Steam library (optional)

By default the app sees your **installed** Steam games plus anything you've put
into Steam **Collections** (the categories in Steam's library sidebar).

- **If you use Collections**, you're all set — they already give the app your games.
- **If you don't use Collections**, you'll only see *installed* games. To spin
  across your **entire owned library** (including games you haven't installed),
  add a free **Steam Web API key**:
  1. Open **Settings → Full Steam Library** (or use the first-run welcome).
  2. Click the link to get a key (sign in, enter any domain like `localhost`,
     agree, copy the key — takes ~30 seconds).
  3. Paste it in (tip: **right-click → Paste**) and click **Save**.

  The key is stored **encrypted** on your PC and only ever sent to Steam. Your
  Steam profile's *Game details* must be set to **Public** for it to read your
  library (Steam → Profile → Edit → Privacy).

  > **Steam Guard required.** Steam only issues API keys to accounts with
  > **Steam Guard** enabled. Most accounts already have it on. If the key page
  > won't let you in, turn Steam Guard on (Steam → Settings → Security, or the
  > Steam Mobile app), then go back. There's nothing the app can do here — it's
  > a Steam account-security requirement on their side.

### Getting your full Epic library

- **With GOG Galaxy** + the Epic integration: your full library shows
  automatically, with playtime and cover art.
- **Without Galaxy**: Settings → *Epic Library Source* → **Connect Epic Account**
  and follow the login steps. (Tokens are stored encrypted via Windows DPAPI.)
- Without either, only *installed* Epic games appear.

---

## Using it

- **Pick a tab** (Steam / GOG / Epic / RetroArch) to see that library's
  categories as cards.
- **Click a card** to open the spin screen, then hit **SPIN**. The wheel lands
  on a game — **Launch** it, or **Spin Again**.
- **"Whole Library"** card spins across everything on that platform.
- **Collection / Tag Roulette** (top-right button) first spins to pick a
  *category*, then lets you spin a game inside it.
- **Leave It To Fate** spins across **every game on every connected platform** at
  once and fires automatically.
- **Exclude from Future Spins** on a winner hides that game permanently (manage
  these in Settings).

---

## Settings reference

Open via the **☰** menu (top-right) → **Settings**.

- **Epic Library Source** — GOG Galaxy integration vs. direct Epic login.
- **Account Connections** — who's signed in on each platform.
- **Full Steam Library** — add/remove your Steam Web API key (see above).
- **Launcher Visibility** — show/hide each platform's tab. Hidden launchers are
  also excluded from Leave It To Fate.
- **Sound** — reel tick & landing sounds during a spin.
- **Edition Preference** — when you own multiple editions of the same game
  (e.g. *Mass Effect* vs *Mass Effect Legendary Edition*), prefer enhanced,
  original, or show both.
- **Cross-platform Duplicates** — hide a game on lower-priority platforms when
  you own it on several. Choose the priority order. *(Off by default. The first
  time you enable it, the app builds a Steam name database in the background —
  give it ~30 seconds; counts fill in as it completes.)*
- **Hidden Collections / Excluded Games** — un-hide anything you've tucked away.

There's also **☰ → Manage Non-Steam Shortcuts** to assign your non-Steam Steam
shortcuts to collections.

---

## Troubleshooting

**"No games found" / only one Steam game shows.**
You probably don't use Steam **Collections**, so the app is showing only your
*installed* games. Either (a) make a Collection in Steam and drop some games in,
or (b) add a **Steam Web API key** (Settings → Full Steam Library) to get your
full owned library. See *Getting your full Steam library* above.

**The "Windows protected your PC" popup.**
Expected — the app isn't code-signed. Click **More info → Run anyway**.

**GOG / Epic games aren't showing.**
GOG needs **GOG Galaxy** installed. For Epic, either install the Epic
integration in GOG Galaxy *or* connect your Epic account in Settings.

**RetroArch games aren't showing.**
RetroArch must be installed with at least one **scanned playlist**. If it's in an
unusual location it may not be auto-detected.

**A game I own but haven't installed never appears.**
Without a Steam API key (or a Collection containing it), the app can't see
owned-but-uninstalled Steam games — Steam doesn't expose them locally.

**Reset / start over.**
Your settings live in `%LOCALAPPDATA%\PC Game Roulette`. Delete that folder to
wipe all settings, caches, and saved logins.

---

## Privacy & your data

- Everything stays **on your PC**, in `%LOCALAPPDATA%\PC Game Roulette` — nothing
  is uploaded or shared.
- Epic tokens and your Steam API key are **encrypted at rest** with the Windows
  Data Protection API (tied to your Windows user — even another user on the same
  PC can't read them).
- No telemetry, no analytics, no account on our end (there is no "our end").
- The app talks only to the official Steam / GOG / Epic / HowLongToBeat services
  to look up game names, art, and (if you opt in) your Steam library.

---

## Building from source

Windows + Python 3.10+.

```sh
pip install pywebview pillow            # runtime deps
python main.py                          # run from source
python main.py --debug                  # run with DevTools
```

Generate the app icon (optional; `app.ico` is committed):

```sh
pip install pillow
python tools/generate_icon.py
```

Build the single-file exe and the installer:

```sh
pip install pyinstaller
python -m PyInstaller pc-game-roulette.spec --noconfirm   # -> dist/PC Game Roulette.exe
# then, with Inno Setup 6 installed:
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer.iss   # -> dist/PC Game Roulette Setup.exe
```

### How it works

- **Python backend** (`backend.py` + helper modules) detects launchers, parses
  their local data, and exposes a JS API.
- **HTML/CSS/JS frontend** (`web/`) renders the UI and the spin animation inside
  a native window via **pywebview** (Edge WebView2).
- Deliberately **no Steam Web API key required** for the default experience —
  Steam collections come from local files, GOG/Epic via GOG Galaxy's database,
  RetroArch from its playlists. The Steam API key is strictly opt-in for the
  full owned library.
- Game data (settings, caches, encrypted tokens, the WebView2 profile) is
  written next to nothing in the install folder — it lives in
  `%LOCALAPPDATA%`, so installs and updates stay clean.

---

*PC Game Roulette is an unofficial, fan-made tool. Steam, GOG, Epic Games, and
RetroArch are trademarks of their respective owners and are not affiliated with
this project.*
