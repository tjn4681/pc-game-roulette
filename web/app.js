/* PC Game Roulette — frontend */

let api = null;
let currentCollection   = null;  // active collection for game spin
let allCollections      = [];    // all real collections (for Collection Roulette)
let allShortcutAppids   = [];    // every non-Steam shortcut appid (from shortcuts.vdf)
let allHiddenCollections = [];   // names the user has hidden (incl. synthetic cards)
let spinMode            = 'game'; // 'game' | 'collection' | 'platform'
let currentWinnerAppid  = null;  // appid currently shown in footer-winner (guards stale callbacks)
let hltbSpinPromise     = null;  // Promise<hltb result> — started during spin, consumed by loadHltbData
let hltbSpinAppid       = null;  // which appid hltbSpinPromise is for
let prevGameWinner    = null;   // last winning appid (game mode) or game obj (platform mode)
let prevCollWinner    = null;   // last winning collection
let pendingStartFrom  = null;   // startFrom value queued by spinAgain for doSpin

let currentPlatform      = 'steam'; // 'steam' | 'gog' | 'epic' | 'battlenet' | 'all'
let gogGames             = [];      // [{id, raw_id, name, platform}] from get_gog_games()
let epicGames            = [];      // [{id, raw_id, name, platform}] from get_epic_games()
let battlenetGames       = [];      // [{id, raw_id, name, platform}] from get_battlenet_games()
let originGames          = [];      // EA App / Origin games
let uplayGames           = [];      // Ubisoft Connect games
let currentPlatformGames = [];      // active pool when spinMode === 'platform'

// Two separate filter sources that both produce per-platform ID exclude sets:
//   * Cross-platform dedup (Batman on Steam + Epic → hide the non-preferred)
//   * Edition preference  (Mass Effect + Mass Effect Legendary → hide one)
// We fetch each lazily and combine into a single set per platform for the
// filter helpers below — callers don't need to care WHY a game is hidden.
let dedupExcludes   = null;
let editionExcludes = null;

async function getDedupExcludes() {
  if (dedupExcludes !== null) return dedupExcludes;
  if (!api) {
    dedupExcludes = { steam: new Set(), gog: new Set(), epic: new Set() };
    return dedupExcludes;
  }
  const r = await api.get_duplicate_filter();
  dedupExcludes = (r && r.status === 'ok')
    ? { steam: new Set(r.steam), gog: new Set(r.gog), epic: new Set(r.epic) }
    : { steam: new Set(), gog: new Set(), epic: new Set() };
  return dedupExcludes;
}

async function getEditionExcludes() {
  if (editionExcludes !== null) return editionExcludes;
  if (!api) {
    editionExcludes = { steam: new Set(), gog: new Set(), epic: new Set() };
    return editionExcludes;
  }
  const r = await api.get_edition_filter();
  editionExcludes = (r && r.status === 'ok')
    ? { steam: new Set(r.steam), gog: new Set(r.gog), epic: new Set(r.epic) }
    : { steam: new Set(), gog: new Set(), epic: new Set() };
  return editionExcludes;
}

async function getAllExcludes() {
  const [d, e] = await Promise.all([getDedupExcludes(), getEditionExcludes()]);
  return {
    steam: new Set([...d.steam, ...e.steam]),
    gog:   new Set([...d.gog,   ...e.gog]),
    epic:  new Set([...d.epic,  ...e.epic]),
  };
}

function invalidateDedupCache()   { dedupExcludes = null; }
function invalidateEditionCache() { editionExcludes = null; }
function invalidateAllExcludes()  { dedupExcludes = null; editionExcludes = null; }

// Drop games whose id is in any of the per-platform exclude sets.
async function filterDuplicates(games) {
  if (!games || !games.length) return games;
  const ex = await getAllExcludes();
  return games.filter(g => !ex[g.platform]?.has(g.id));
}

// Variant for raw Steam appids (numeric) — used in Steam collections where the
// data shape predates the {id, raw_id, ...} structure.
async function filterSteamAppids(appids) {
  if (!appids || !appids.length) return appids;
  const ex = await getAllExcludes();
  if (!ex.steam.size) return appids;
  return appids.filter(a => !ex.steam.has(`steam_${a}`));
}

// ── Screen routing ────────────────────────────────────────────────────────

function showScreen(id) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

// ── Collection grid ───────────────────────────────────────────────────────

async function renderCollections(collections, shortcutAppids, hiddenList) {
  const grid  = document.getElementById('collection-grid');
  const empty = document.getElementById('empty-state');
  grid.innerHTML = '';

  allCollections       = collections    || [];
  allShortcutAppids    = shortcutAppids || allShortcutAppids;
  if (hiddenList)
    allHiddenCollections = hiddenList;
  const hiddenSet = new Set(allHiddenCollections);

  // If cross-platform dedup is enabled and Steam isn't the top priority, some
  // Steam appids may need to be hidden because they're available on a
  // higher-ranked platform.  Build a filtered view (without mutating the
  // canonical allCollections, which other UI consumers rely on).
  const stripExcludes = async (appids) => filterSteamAppids(appids);
  const filteredColls = await Promise.all(allCollections.map(async (c) => ({
    ...c,
    appids: await stripExcludes(c.appids),
  })));
  // Recompute counts to match what's actually spinnable
  filteredColls.forEach(c => { c.count = c.appids.length; });

  // Whole Library = union of every appid across visible collections + shortcuts
  let allAppIds = [...new Set([
    ...filteredColls.flatMap(c => c.appids),
    ...allShortcutAppids,
  ])];

  if (allAppIds.length > 0 && !hiddenSet.has('Whole Library')) {
    grid.appendChild(makeCollCard(
      { name: 'Whole Library', count: allAppIds.length, appids: allAppIds },
      'library'
    ));
  }

  if (allShortcutAppids.length > 0 && !hiddenSet.has('Non-Steam Shortcuts')) {
    grid.appendChild(makeCollCard(
      { name: 'Non-Steam Shortcuts', count: allShortcutAppids.length, appids: allShortcutAppids },
      'shortcuts'
    ));
  }

  if (!filteredColls.length) {
    if (allAppIds.length === 0) loadInstalledLibrary(grid, empty);
    else { empty.classList.remove('hidden'); showScreen('screen-main'); }
    return;
  }

  empty.classList.add('hidden');
  // Hide collections that ended up empty after dedup, otherwise render normally
  filteredColls.filter(c => c.count > 0)
               .forEach(c => grid.appendChild(makeCollCard(c, null)));
  showScreen('screen-main');
}

function makeCollCard(collection, variant) {
  const card = document.createElement('div');
  let classes = 'coll-card';
  if      (variant === 'library')   classes += ' coll-card-library';
  else if (variant === 'shortcuts') classes += ' coll-card-shortcuts';
  card.className = classes;
  const unitWord  = variant === 'shortcuts' ? 'shortcut' : 'game';
  const hideable  = true; // every card can be hidden; manage from Settings to un-hide
  card.innerHTML = `
    <div class="coll-name" title="${esc(collection.name)}">${esc(collection.name)}</div>
    <div class="coll-count">${collection.count.toLocaleString()} ${unitWord}${collection.count === 1 ? '' : 's'}</div>
    ${hideable ? `<button class="coll-hide-btn" title="Hide this collection">&times;</button>` : ''}
  `;
  card.addEventListener('click', () => openSpin(collection));
  const hideBtn = card.querySelector('.coll-hide-btn');
  if (hideBtn) {
    hideBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!api) return;
      const r = await api.toggle_hide_collection(collection.name);
      if (r.status === 'ok') {
        renderCollections(r.collections, r.shortcut_appids, r.hidden_collections);
        showToast(`"${collection.name}" hidden · manage in Settings`);
      } else {
        showToast(`Failed: ${r.message || r.status}`, 'error');
      }
    });
  }

  // Random background art from a game inside this collection.
  // Skip Whole Library (no single representative image makes sense).
  // Only use Steam appids — non-Steam shortcuts don't have CDN header images.
  // Tries up to 5 randomly-shuffled candidates so age-gated / delisted games
  // don't leave the card blank — moves to the next candidate on each failure.
  if (variant !== 'library') {
    const steamIds = (collection.appids || []).filter(id => !isLikelyNonSteam(id));
    if (steamIds.length > 0) {
      const candidates = [...steamIds].sort(() => Math.random() - 0.5).slice(0, 5);
      let ci = 0;
      const tryNextGame = () => {
        if (ci >= candidates.length) return;   // all candidates exhausted
        const pick = candidates[ci++];
        const urls = headerUrls(pick);
        let ui = 0;
        const tryNextUrl = () => {
          if (ui >= urls.length) { tryNextGame(); return; }  // all CDNs for this game failed
          const img = new Image();
          img.onload = () => {
            card.style.backgroundImage = `url('${img.src}')`;
            card.classList.add('has-bg-art');
          };
          img.onerror = () => { ui++; tryNextUrl(); };
          img.src = urls[ui];
        };
        tryNextUrl();
      };
      tryNextGame();
    }
  }

  return card;
}

async function loadInstalledLibrary(grid, empty) {
  if (!api) { empty.classList.remove('hidden'); showScreen('screen-main'); return; }
  const result = await api.get_installed_games();
  if (result.status === 'ok' && result.games.length > 0) {
    const appids = result.games.map(g => g.appid);
    grid.appendChild(makeCollCard({ name: 'Installed Games', count: appids.length, appids }, true));
    empty.classList.add('hidden');
  } else {
    empty.classList.remove('hidden');
  }
  showScreen('screen-main');
}

// ── Account picker ────────────────────────────────────────────────────────

function renderAccountPicker(accounts) {
  const list = document.getElementById('account-list');
  list.innerHTML = '';
  accounts.forEach(acc => {
    const btn = document.createElement('button');
    btn.className = 'account-btn';
    btn.innerHTML = `
      <span class="account-id">Account ${esc(acc.id)}</span>
      <span class="account-path">${esc(acc.path)}</span>
    `;
    btn.addEventListener('click', async () => {
      showScreen('screen-loading');
      document.querySelector('#screen-loading .loading-text').textContent = 'Loading…';
      handleLoadResult(await api.select_account(acc.path));
    });
    list.appendChild(btn);
  });
  showScreen('screen-pick');
}

// Friendly empty state for users with none of Steam/GOG/Epic installed.
// Replaces the Steam-specific error screen content with something that
// actually explains the situation.
function showNoPlatformsScreen() {
  const screen = document.getElementById('screen-error');
  if (!screen) return;
  const inner = screen.querySelector('.center-stack');
  if (inner) {
    inner.innerHTML = `
      <div class="logo">PC GAME ROULETTE</div>
      <p class="subtitle">No game launchers detected on this PC.</p>
      <p class="hint">
        This app picks random games from your installed launchers — but it
        looks like you don't have any of them set up yet.<br><br>
        To use it, install at least one of:<br>
        &bull; <strong>Steam</strong><br>
        &bull; <strong>GOG Galaxy</strong><br>
        &bull; <strong>Epic Games Launcher</strong>
        <br><br>
        Then re-launch the app — your library will be detected automatically.
      </p>
    `;
  }
  showScreen('screen-error');
}

// ── Result router ─────────────────────────────────────────────────────────

function handleLoadResult(result) {
  if      (result.status === 'ok')   renderCollections(result.collections, result.shortcut_appids, result.hidden_collections);
  else if (result.status === 'pick') renderAccountPicker(result.accounts);
  else {
    document.getElementById('error-message').textContent = result.message || 'Unknown error.';
    showScreen('screen-error');
  }
}

// ═════════════════════════════════════════════════════════════════════════
//  REEL ANIMATION  (shared by game mode + collection mode)
// ═════════════════════════════════════════════════════════════════════════

const CARD_H    = 300;
const CARD_GAP  = 12;
const CARD_SLOT = CARD_H + CARD_GAP;
const N_FILLERS = 25;
const TOTAL_TRAVEL = N_FILLERS * CARD_SLOT;

const PHASE1_MS   = 2000;
const PHASE2_MS   = 2500;
const _RATIO      = 1 + PHASE2_MS / (4 * PHASE1_MS);
const PHASE1_DIST = TOTAL_TRAVEL / _RATIO;
const PHASE2_DIST = TOTAL_TRAVEL - PHASE1_DIST;

function headerUrls(appid) {
  return [
    `https://cdn.cloudflare.steamstatic.com/steam/apps/${appid}/header.jpg`,
    `https://steamcdn-a.akamaihd.net/steam/apps/${appid}/header.jpg`,
  ];
}
function headerUrl(appid) { return headerUrls(appid)[0]; }

// Non-Steam shortcut IDs are 32-bit CRC32 values — typically > 1 billion or
// negative when stored as signed.  Steam's real appid range is currently under
// a few million, so anything in this range is almost certainly a shortcut.
function isLikelyNonSteam(appid) {
  return appid < 0 || appid > 100_000_000;
}

function nonSteamPlaceholder(extraClass = '', appid = null) {
  const el = document.createElement('div');
  el.className = ('non-steam-placeholder ' + extraClass).trim();
  // If we know the appid is in Steam's normal range, the failure is a missing
  // header image (delisted, region-locked, age-gated misfire), not an actual
  // non-Steam shortcut.  Use a different label so the user isn't misled.
  const label = (appid !== null && !isLikelyNonSteam(appid))
    ? 'Image Unavailable'
    : 'Non-Steam Game';
  el.innerHTML = `<span class="nsp-label">${label}</span>`;
  return el;
}

function attachImgFallback(img, appid) {
  const urls = headerUrls(appid);
  let urlIdx = 0;
  img.onerror = () => {
    urlIdx++;
    if (urlIdx < urls.length) {
      img.src = urls[urlIdx];
      return;
    }
    // All CDNs failed — replace with styled placeholder.
    const reelCard = img.closest('.reel-card');
    if (reelCard) {
      reelCard.classList.add('non-steam-card');
      reelCard.innerHTML = '';
      reelCard.appendChild(nonSteamPlaceholder('', appid));
    } else {
      img.style.display = 'none';
      img.after(nonSteamPlaceholder('winner-nsp', appid));
    }
  };
}
function randItem(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
function easeOutQuart(t) { return 1 - Math.pow(1 - t, 4); }
function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── Sound ─────────────────────────────────────────────────────────────────

let _audioCtx = null;
function getAudioCtx() {
  if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  return _audioCtx;
}

// User-controlled sound toggle (mirrored from backend get_sound_enabled).
// Loaded on init(); switched live when the Settings checkbox changes.
let soundEnabled = true;

function playTick(speedFraction) {
  if (!soundEnabled) return;
  try {
    const ctx  = getAudioCtx();
    const osc  = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);

    // Pitch rises slightly as the reel slows (higher pitch = faster feel when spinning)
    osc.type = 'square';
    osc.frequency.value = 660 + speedFraction * 440; // 660–1100 Hz

    const now = ctx.currentTime;
    gain.gain.setValueAtTime(0.04, now);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.06);

    osc.start(now);
    osc.stop(now + 0.07);
  } catch (_) {}
}

function playLanding() {
  if (!soundEnabled) return;
  try {
    const ctx   = getAudioCtx();
    const notes = [523.25, 659.25, 783.99]; // C5 E5 G5
    notes.forEach((freq, i) => {
      const osc  = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain); gain.connect(ctx.destination);
      osc.type = 'sine';
      osc.frequency.value = freq;
      const t = ctx.currentTime + i * 0.08;
      gain.gain.setValueAtTime(0, t);
      gain.gain.linearRampToValueAtTime(0.12, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.55);
      osc.start(t); osc.stop(t + 0.6);
    });
  } catch (_) {}
}

// ── Game reel ─────────────────────────────────────────────────────────────

function buildGameReel(appids, startFrom = null, forcedWinner = null) {
  const winner  = (forcedWinner !== null && appids.includes(forcedWinner))
    ? forcedWinner
    : randItem(appids);
  const pool    = appids.length > 1 ? appids.filter(id => id !== winner) : appids;
  const fillers = Array.from({ length: N_FILLERS }, (_, i) =>
    i === 0 && startFrom !== null ? startFrom : randItem(pool)
  );
  const sequence = [...fillers, winner];

  const reel = document.getElementById('reel');
  reel.innerHTML = '';
  reel.style.transform = 'translateY(0)';
  reel.style.filter    = '';

  sequence.forEach((appid, i) => {
    const card = document.createElement('div');
    card.className = 'reel-card' + (i === sequence.length - 1 ? ' reel-winner' : '');

    if (isLikelyNonSteam(appid)) {
      // Skip CDN attempt entirely — show placeholder immediately so the card
      // is visible the moment it scrolls past, not after a network timeout.
      card.classList.add('non-steam-card');
      card.appendChild(nonSteamPlaceholder('', appid));
    } else {
      const img = document.createElement('img');
      img.alt = ''; img.draggable = false;
      attachImgFallback(img, appid);
      img.src = headerUrl(appid);
      card.appendChild(img);
    }
    reel.appendChild(card);
  });

  return winner;
}

// ── Collection reel ───────────────────────────────────────────────────────

function buildCollectionReel(collections, startFrom = null, forcedWinner = null) {
  const winner = (forcedWinner && collections.find(c => c.name === forcedWinner.name))
    ? forcedWinner
    : randItem(collections);
  const pool   = collections.length > 1
    ? collections.filter(c => c.name !== winner.name) : collections;
  const fillers  = Array.from({ length: N_FILLERS }, (_, i) =>
    i === 0 && startFrom !== null ? startFrom : randItem(pool)
  );
  const sequence = [...fillers, winner];

  const reel = document.getElementById('reel');
  reel.innerHTML = '';
  reel.style.transform = 'translateY(0)';
  reel.style.filter    = '';

  sequence.forEach((coll, i) => {
    const card = document.createElement('div');
    card.className = 'reel-card reel-card-collection' + (i === sequence.length - 1 ? ' reel-winner' : '');
    card.innerHTML = `
      <div class="coll-reel-name">${esc(coll.name)}</div>
      <div class="coll-reel-count">${coll.count.toLocaleString()} games</div>
    `;
    reel.appendChild(card);
  });

  return winner;
}

// ── Core animation ────────────────────────────────────────────────────────

function runAnimation() {
  const reel     = document.getElementById('reel');
  const frameEl  = document.querySelector('.reel-frame');
  const viewport = document.querySelector('.reel-viewport');
  const start    = performance.now();
  let lastCardIdx = -1;

  frameEl.classList.add('spinning');
  viewport.classList.add('spinning');

  return new Promise(resolve => {
    function frame(now) {
      const elapsed = now - start;
      let pos, blur, speedFraction;

      if (elapsed <= PHASE1_MS) {
        pos           = (elapsed / PHASE1_MS) * PHASE1_DIST;
        blur          = 5;
        speedFraction = 1;
      } else {
        const t = Math.min((elapsed - PHASE1_MS) / PHASE2_MS, 1);
        pos           = PHASE1_DIST + easeOutQuart(t) * PHASE2_DIST;
        blur          = Math.pow(1 - t, 2) * 4;
        speedFraction = Math.pow(1 - t, 3); // derivative of easeOutQuart ∝ (1-t)^3
        if (t >= 1) {
          reel.style.transform = `translateY(-${TOTAL_TRAVEL}px)`;
          reel.style.filter    = '';
          frameEl.classList.remove('spinning');
          viewport.classList.remove('spinning');
          resolve(); return;
        }
      }

      // Tick sound on each card-slot crossing
      const cardIdx = Math.floor(pos / CARD_SLOT);
      if (cardIdx !== lastCardIdx) {
        lastCardIdx = cardIdx;
        playTick(speedFraction);
      }

      reel.style.transform = `translateY(-${pos}px)`;
      reel.style.filter    = blur > 0.25 ? `blur(${blur.toFixed(1)}px)` : '';
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  });
}

// ═════════════════════════════════════════════════════════════════════════
//  SPIN ORCHESTRATION
// ═════════════════════════════════════════════════════════════════════════

let currentSpinToken = 0;

async function doSpin(forceNonSteam = false) {
  const spinBtn = document.getElementById('btn-spin');
  spinBtn.disabled = true; spinBtn.textContent = '…';

  const startFrom = pendingStartFrom;
  pendingStartFrom = null;

  // Pick the winner up-front so we can pre-fetch its art in parallel with the
  // animation.  Otherwise the reel uses CDN-via-WebView2 which fails for
  // age-gated games — the card shows "Non-Steam Game" until the winner panel
  // lazily loads the real art via Python.
  let winner;
  if (spinMode === 'collection') {
    winner = randItem(allCollections);
  } else if (spinMode === 'platform') {
    winner = randItem(currentPlatformGames);
  } else {
    const candidates = forceNonSteam
      ? currentCollection.appids.filter(isLikelyNonSteam)
      : currentCollection.appids;
    const pool = candidates.length ? candidates : currentCollection.appids;
    winner = randItem(pool);
  }

  const myToken = ++currentSpinToken;

  // Pre-fetch backend art so the reel winner card shows the reliable
  // Python-fetched image (handles age-gated games that CDN blocks in WebView2).
  const _artAppid = spinMode === 'platform' && winner.platform === 'steam'
    ? winner.raw_id : (spinMode === 'game' ? winner : null);
  if (api && _artAppid !== null && !isLikelyNonSteam(_artAppid)) {
    api.get_game_art(String(_artAppid)).then(result => {
      if (myToken !== currentSpinToken) return;
      if (result.status !== 'ok') return;
      const winnerCard = document.querySelector('#reel .reel-winner');
      if (!winnerCard) return;
      winnerCard.classList.remove('non-steam-card');
      winnerCard.innerHTML = '';
      const img = document.createElement('img');
      img.alt = ''; img.draggable = false;
      img.src = result.data;
      winnerCard.appendChild(img);
    });
  }

  // Pre-fetch HLTB during the spin so data is ready the moment the winner panel appears.
  // Chain: get_game_name (usually instant from cache) → get_hltb_data (network, ~3s).
  hltbSpinAppid   = spinMode === 'platform' ? winner.id : winner;
  hltbSpinPromise = null;
  if (api && spinMode === 'game' && !isLikelyNonSteam(winner)) {
    // Steam game mode — name must be fetched first
    hltbSpinPromise = (async () => {
      const nameResult = await api.get_game_name(String(winner));
      if (myToken !== currentSpinToken) return null;
      if (nameResult.status !== 'ok') return null;
      return api.get_hltb_data(String(winner), nameResult.name);
    })();
  } else if (api && spinMode === 'platform' && winner.name) {
    // GOG / Epic — name already known, go straight to HLTB
    hltbSpinPromise = (async () => {
      if (myToken !== currentSpinToken) return null;
      return api.get_hltb_data(winner.id, winner.name);
    })();
  } else if (api && spinMode === 'platform' && winner.platform === 'steam'
             && !isLikelyNonSteam(winner.raw_id)) {
    // Steam game in All mode — fetch name first, then HLTB
    hltbSpinPromise = (async () => {
      const nameResult = await api.get_game_name(String(winner.raw_id));
      if (myToken !== currentSpinToken) return null;
      if (nameResult.status !== 'ok') return null;
      return api.get_hltb_data(winner.id, nameResult.name);
    })();
  }

  if (spinMode === 'collection') {
    buildCollectionReel(allCollections, startFrom, winner);
  } else if (spinMode === 'platform') {
    buildPlatformReel(currentPlatformGames, startFrom, winner);
  } else {
    buildGameReel(currentCollection.appids, startFrom, winner);
  }

  await runAnimation();

  const winnerCard = document.querySelector('#reel .reel-winner');
  if (winnerCard) winnerCard.classList.add('winner-pulse');
  playLanding();
  await delay(650);

  // Show winner info below the reel — no animation, no stage swap
  if      (spinMode === 'collection') showCollectionWinner(winner);
  else if (spinMode === 'platform')   showPlatformWinner(winner);
  else                                showGameWinner(winner);

  document.getElementById('footer-spin').classList.add('hidden');
  document.getElementById('footer-winner').classList.remove('hidden');
}

function showGameWinner(appid) {
  prevGameWinner     = appid;
  currentWinnerAppid = appid;

  // Exclude button
  const exclBtn = document.getElementById('btn-exclude');
  exclBtn.classList.remove('hidden');
  exclBtn.onclick = () => excludeWinningGame(appid);

  // Reset meta and HLTB — populated async below
  const metaEl = document.getElementById('winner-meta');
  metaEl.textContent = '';
  metaEl.classList.add('hidden');
  const hltbRow = document.getElementById('hltb-row');
  if (hltbRow) { hltbRow.classList.add('hidden'); hltbRow.innerHTML = ''; }

  const nameEl = document.getElementById('winner-name');
  nameEl.textContent = 'Loading…';
  nameEl.classList.remove('hidden');

  document.getElementById('btn-launch').classList.remove('hidden');
  document.getElementById('btn-launch').onclick = () => api && api.launch_game(String(appid));
  document.getElementById('winner-coll-card').classList.add('hidden');
  document.getElementById('btn-spin-game').classList.add('hidden');
  document.getElementById('btn-spin-again').textContent = 'Spin Again';

  if (api) {
    api.get_game_name(String(appid)).then(result => {
      if (currentWinnerAppid !== appid) return;
      const name = result.status === 'ok' ? result.name : `App ${appid}`;
      nameEl.textContent = name;
      const pretty = formatPlaytime(result.playtime_minutes || 0);
      if (pretty) {
        metaEl.textContent = `${pretty} played`;
        metaEl.classList.remove('hidden');
      } else {
        metaEl.classList.add('hidden');
      }
      if (result.status === 'ok' && !isLikelyNonSteam(appid)) loadHltbData(appid, name);
    });
  } else {
    nameEl.textContent = `App ${appid}`;
  }
}

function showCollectionWinner(collection) {
  prevCollWinner     = collection;
  currentWinnerAppid = null;  // collection mode — no game appid

  // Hide game-mode elements
  document.getElementById('winner-name').classList.add('hidden');
  document.getElementById('btn-launch').classList.add('hidden');
  document.getElementById('btn-exclude').classList.add('hidden');
  const hltbRow = document.getElementById('hltb-row');
  if (hltbRow) { hltbRow.classList.add('hidden'); hltbRow.innerHTML = ''; }

  // Show collection card
  document.getElementById('winner-coll-card').classList.remove('hidden');
  document.getElementById('winner-coll-title').textContent = collection.name;
  document.getElementById('winner-coll-sub').textContent =
    `${collection.count.toLocaleString()} game${collection.count === 1 ? '' : 's'}`;

  // "Spin a Game" goes straight into game spin for this collection
  const spinGameBtn = document.getElementById('btn-spin-game');
  spinGameBtn.classList.remove('hidden');
  spinGameBtn.onclick = () => openSpin(collection);

  document.getElementById('btn-spin-again').textContent = 'Pick Another';
}

async function spinAgain() {
  const startFrom = spinMode === 'collection' ? prevCollWinner : prevGameWinner;
  pendingStartFrom = startFrom;

  // Hide winner info instantly — reel stays visible the whole time
  document.getElementById('footer-winner').classList.add('hidden');
  currentWinnerAppid = null;

  // Pre-build so the reel shows the previous winner at position 0 briefly
  if      (spinMode === 'collection') buildCollectionReel(allCollections, startFrom);
  else if (spinMode === 'platform')   buildPlatformReel(currentPlatformGames, startFrom);
  else                                buildGameReel(currentCollection.appids, startFrom);

  await delay(80);
  doSpin();
}

// ── Entry points ──────────────────────────────────────────────────────────

function openSpin(collection) {
  spinMode          = 'game';
  currentCollection = collection;

  const nonSteam = collection.appids.filter(isLikelyNonSteam).length;
  const games    = `${collection.count.toLocaleString()} game${collection.count === 1 ? '' : 's'}`;
  const tail     = nonSteam > 0
    ? ` · ${nonSteam} non-Steam shortcut${nonSteam === 1 ? '' : 's'}`
    : '';

  document.getElementById('spin-coll-name').textContent  = collection.name;
  document.getElementById('spin-coll-count').textContent = games + tail;

  document.getElementById('footer-spin').classList.remove('hidden');
  document.getElementById('footer-winner').classList.add('hidden');
  document.getElementById('btn-spin').disabled    = false;
  document.getElementById('btn-spin').textContent = 'SPIN';
  currentWinnerAppid = null;

  buildGameReel(collection.appids);
  showScreen('screen-spin');
}

function openCollectionRoulette() {
  if (allCollections.length === 0) return;
  spinMode = 'collection';

  document.getElementById('spin-coll-name').textContent  = 'Collection Roulette';
  document.getElementById('spin-coll-count').textContent =
    `${allCollections.length} collection${allCollections.length === 1 ? '' : 's'}`;

  document.getElementById('footer-spin').classList.remove('hidden');
  document.getElementById('footer-winner').classList.add('hidden');
  document.getElementById('btn-spin').disabled    = false;
  document.getElementById('btn-spin').textContent = 'SPIN';
  currentWinnerAppid = null;

  buildCollectionReel(allCollections);
  showScreen('screen-spin');
}

// ── Browse fallback ───────────────────────────────────────────────────────

async function browseForFile() {
  const result = await api.browse_for_file();
  if (result.status === 'cancelled') return;
  handleLoadResult(result);
}

// Show feedback after a GOG / Epic launch so the user can verify the right URI
// is being attempted.  Most failures here are silent at the OS level (Windows
// just opens the wrong launcher or does nothing), so surfacing the URI helps
// diagnose URI-scheme issues.
function reportLaunch(result) {
  if (!result) return;
  if (result.status === 'ok') {
    showToast(`Launching: ${result.uri || 'game'}`);
  } else {
    showToast(`Launch failed: ${result.message || result.status}`, 'error');
  }
}

function showToast(message, kind = 'success') {
  let toast = document.getElementById('toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.className = 'toast ' + (kind === 'error' ? 'toast-error' : 'toast-success');
  // Force reflow so re-triggering the animation works
  void toast.offsetWidth;
  toast.classList.add('show');
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toast.classList.remove('show'), 2400);
}

// ── Exclude / Settings / User info ────────────────────────────────────────

async function excludeWinningGame(appid) {
  if (!api) return;
  const r = await api.toggle_exclude(String(appid));
  if (r.status !== 'ok') {
    showToast(`Failed: ${r.message || r.status}`, 'error');
    return;
  }
  allCollections    = r.collections;
  allShortcutAppids = r.shortcut_appids;
  // Keep currentCollection in sync if it still exists
  if (currentCollection) {
    const updated = allCollections.find(c => c.name === currentCollection.name);
    if (updated) currentCollection = updated;
  }
  prevGameWinner = null; // don't start the next reel on the now-excluded game
  showToast(`Excluded from future spins`);
  spinAgain();
}

// Sibling for GOG/Epic winners — the Steam version above uses integer appids
// and re-fetches Steam collections; this one operates on the platform pool
// and uses the prefixed-ID exclude list.
async function excludePlatformWinningGame(game) {
  if (!api) return;
  const r = await api.toggle_exclude_platform_game(game.id, game.name || '');
  if (r.status !== 'ok') {
    showToast(`Failed: ${r.message || r.status}`, 'error');
    return;
  }
  // Drop the game from the current spin pool so spinAgain doesn't pick it
  currentPlatformGames = currentPlatformGames.filter(g => g.id !== game.id);
  // Also clean it out of the cached lists so re-entering the tab is consistent
  if (game.platform === 'gog')        gogGames       = gogGames.filter(g => g.id !== game.id);
  if (game.platform === 'epic')       epicGames      = epicGames.filter(g => g.id !== game.id);
  if (game.platform === 'battlenet')  battlenetGames = battlenetGames.filter(g => g.id !== game.id);
  if (game.platform === 'origin')     originGames    = originGames.filter(g => g.id !== game.id);
  if (game.platform === 'uplay')      uplayGames     = uplayGames.filter(g => g.id !== game.id);
  prevGameWinner = null;
  showToast(`Excluded "${game.name}" from future spins`);
  spinAgain();
}

async function openSettings() {
  if (!api) { showToast('Backend not available', 'error'); return; }
  const r = await api.get_settings();
  if (r.status !== 'ok') {
    showToast(`Failed: ${r.message || r.status}`, 'error');
    return;
  }
  renderSettings(r);
  await refreshEpicSettings();
  await refreshBattlenetSource();
  await refreshOriginSource();
  await refreshUplaySource();
  await refreshDedupSettings();
  await refreshEditionPreference();
  await refreshSoundSettings();
  await refreshLauncherVisibility();
  showScreen('screen-settings');
}

// ── Per-launcher visibility (Settings) ───────────────────────────────────

let launcherStatusCache = null;

async function refreshLauncherVisibility() {
  if (!api) return;
  const r = await api.get_launcher_status();
  if (r.status !== 'ok') return;
  launcherStatusCache = r.launchers;
  renderLauncherVisibilityList(r.launchers);
  applyLauncherVisibility(r.launchers);
}

function renderLauncherVisibilityList(launchers) {
  const list = document.getElementById('launcher-vis-list');
  if (!list) return;
  list.innerHTML = '';
  launchers.forEach(l => {
    const row = document.createElement('label');
    row.className = 'launcher-vis-item';
    const statusClass = l.installed ? '' : 'not-installed';
    const statusText  = l.installed ? '' : 'not installed';
    row.innerHTML = `
      <input type="checkbox" data-launcher="${l.id}" ${l.enabled ? 'checked' : ''}>
      <span class="launcher-vis-name">${esc(l.name)}</span>
      <span class="launcher-vis-status ${statusClass}">${statusText}</span>
    `;
    row.querySelector('input').addEventListener('change', async (e) => {
      if (!api) return;
      const upd = await api.set_launcher_enabled(l.id, e.target.checked);
      if (upd.status === 'ok') {
        launcherStatusCache = upd.launchers;
        applyLauncherVisibility(upd.launchers);
      }
    });
    list.appendChild(row);
  });
}

// Hide tab buttons for launchers the user has disabled.  Called both at app
// startup (from init) and after Settings changes (live update).
function applyLauncherVisibility(launchers) {
  launchers.forEach(l => {
    const tab = document.getElementById(`tab-${l.id}`);
    if (!tab) return;
    tab.style.display = l.enabled ? '' : 'none';
  });
  // If the user disabled their currently-active tab, fall back to Steam
  // (or any remaining enabled tab) so we don't leave them stranded.
  const activeTab = document.querySelector('.platform-tab.active');
  if (activeTab && activeTab.style.display === 'none') {
    const fallback = launchers.find(l => l.enabled);
    if (fallback) switchPlatform(fallback.id);
  }
}

async function refreshBattlenetSource() {
  if (!api) return;
  const r = await api.get_battlenet_source();
  if (r.status !== 'ok') return;
  document.querySelectorAll('input[name="battlenet-source"]').forEach(el => {
    el.checked = (el.value === r.source);
  });
}

async function refreshOriginSource() {
  if (!api) return;
  const r = await api.get_origin_source();
  if (r.status !== 'ok') return;
  document.querySelectorAll('input[name="origin-source"]').forEach(el => {
    el.checked = (el.value === r.source);
  });
}

async function refreshUplaySource() {
  if (!api) return;
  const r = await api.get_uplay_source();
  if (r.status !== 'ok') return;
  document.querySelectorAll('input[name="uplay-source"]').forEach(el => {
    el.checked = (el.value === r.source);
  });
}

async function refreshSoundSettings() {
  if (!api) return;
  const r = await api.get_sound_enabled();
  if (r.status !== 'ok') return;
  soundEnabled = !!r.enabled;
  const cb = document.getElementById('sound-enabled');
  if (cb) cb.checked = soundEnabled;
}

// ── Edition preference (same-game variants like Mass Effect vs Legendary) ──

async function refreshEditionPreference() {
  if (!api) return;
  const r = await api.get_edition_preference();
  if (r.status !== 'ok') return;
  document.querySelectorAll('input[name="edition-pref"]').forEach(el => {
    el.checked = (el.value === r.preference);
  });
  await refreshEditionPreview();
}

async function refreshEditionPreview() {
  const preview = document.getElementById('edition-pref-preview');
  if (!api || !preview) return;
  const r = await api.get_edition_preference();
  if (r.preference === 'both') {
    preview.innerHTML = 'Currently disabled — every edition you own shows up independently.';
    return;
  }
  preview.innerHTML = 'Computing edition variants…';
  const filter = await api.get_edition_filter();
  if (filter.status !== 'ok') {
    preview.textContent = 'Could not compute edition variants.';
    return;
  }
  const c = filter.counts || {};
  const total = (c.steam_hidden || 0) + (c.gog_hidden || 0) + (c.epic_hidden || 0);
  if (total === 0) {
    preview.innerHTML = 'No multi-edition titles detected in your library yet.';
    return;
  }
  const parts = [];
  if (c.steam_hidden) parts.push(`<strong>${c.steam_hidden}</strong> on Steam`);
  if (c.gog_hidden)   parts.push(`<strong>${c.gog_hidden}</strong> on GOG`);
  if (c.epic_hidden)  parts.push(`<strong>${c.epic_hidden}</strong> on Epic`);
  const kind = r.preference === 'enhanced' ? 'original' : 'enhanced';
  preview.innerHTML = `Hiding ${parts.join(', ')} ${kind} edition${total === 1 ? '' : 's'} — keeping the other variant.`;
}

// ── Cross-platform duplicate settings ──────────────────────────────────────

const _PLATFORM_LABELS = { steam: 'Steam', gog: 'GOG', epic: 'Epic',
                            battlenet: 'Battle.net', origin: 'EA App', uplay: 'Ubisoft' };

async function refreshDedupSettings() {
  if (!api) return;
  const s = await api.get_dedup_settings();
  if (s.status !== 'ok') return;

  document.getElementById('dedup-enabled').checked = !!s.enabled;
  const block = document.getElementById('dedup-priority-block');
  block.classList.toggle('enabled', !!s.enabled);

  renderDedupPriorityList(s.priority);
  await refreshDedupPreview();
}

function renderDedupPriorityList(priority) {
  const list = document.getElementById('dedup-priority-list');
  list.innerHTML = '';
  priority.forEach((p, idx) => {
    const li = document.createElement('li');
    li.className = 'dedup-priority-item';
    li.innerHTML = `
      <span class="dedup-priority-rank">${idx + 1}</span>
      <span class="dedup-priority-name">${esc(_PLATFORM_LABELS[p] || p)}</span>
      <span class="dedup-priority-btns">
        <button class="dedup-priority-btn" data-dir="up"   ${idx === 0 ? 'disabled' : ''} title="Move up">&uarr;</button>
        <button class="dedup-priority-btn" data-dir="down" ${idx === priority.length - 1 ? 'disabled' : ''} title="Move down">&darr;</button>
      </span>
    `;
    li.querySelectorAll('.dedup-priority-btn').forEach(btn => {
      btn.addEventListener('click', () => moveDedupPriority(p, btn.dataset.dir));
    });
    list.appendChild(li);
  });
}

async function moveDedupPriority(platform, direction) {
  if (!api) return;
  const s = await api.get_dedup_settings();
  if (s.status !== 'ok') return;
  const priority = [...s.priority];
  const idx = priority.indexOf(platform);
  if (idx === -1) return;
  const swapWith = direction === 'up' ? idx - 1 : idx + 1;
  if (swapWith < 0 || swapWith >= priority.length) return;
  [priority[idx], priority[swapWith]] = [priority[swapWith], priority[idx]];
  await api.set_dedup_settings(s.enabled, priority);
  await refreshDedupSettings();
  // Clear cached filter so the next platform load re-applies
  invalidateDedupCache();
}

async function refreshDedupPreview() {
  const preview = document.getElementById('dedup-preview');
  if (!api || !preview) return;
  const s = await api.get_dedup_settings();
  if (!s.enabled) {
    preview.innerHTML = 'Currently disabled — every game shows up on every platform tab where you own it.';
    return;
  }

  // Heads-up: the first time dedup runs we need to download the bulk Steam
  // name database (~30 sec from SteamSpy). After that it's cached for a week.
  const status = await api.get_steam_names_status();
  if (!status.cached) {
    preview.innerHTML =
      '<strong>One-time setup:</strong> Building Steam game name database… ' +
      'this fetches ~30,000 game names from SteamSpy and takes about 30 seconds. ' +
      'Subsequent loads are instant.';
  } else {
    preview.innerHTML = 'Computing duplicates…';
  }

  const filter = await api.get_duplicate_filter();
  if (filter.status !== 'ok') {
    preview.textContent = 'Could not compute duplicates.';
    return;
  }
  const c = filter.counts || {};
  const total = (c.steam_hidden || 0) + (c.gog_hidden || 0) + (c.epic_hidden || 0);
  if (total === 0) {
    preview.innerHTML = 'No cross-platform duplicates detected in your library.';
    return;
  }
  const parts = [];
  if (c.steam_hidden) parts.push(`<strong>${c.steam_hidden}</strong> on Steam`);
  if (c.gog_hidden)   parts.push(`<strong>${c.gog_hidden}</strong> on GOG`);
  if (c.epic_hidden)  parts.push(`<strong>${c.epic_hidden}</strong> on Epic`);
  preview.innerHTML = `Hiding ${parts.join(', ')} — duplicates present on a higher-ranked platform.`;
}

// ── Epic source picker + OAuth flow ────────────────────────────────────────

async function refreshEpicSettings() {
  if (!api) return;
  const [src, status] = await Promise.all([
    api.get_epic_source(),
    api.epic_oauth_status(),
  ]);

  // Source radio
  const value = (src && src.source) || 'galaxy';
  const radio = document.querySelector(`input[name="epic-source"][value="${value}"]`);
  if (radio) radio.checked = true;

  // Connection status row
  const statusEl   = document.getElementById('epic-connection-status');
  const connectBtn = document.getElementById('epic-connect-btn');
  const disconnect = document.getElementById('epic-disconnect-btn');

  if (status && status.connected) {
    const who = status.displayName ? esc(status.displayName) : 'Epic account';
    statusEl.innerHTML = `Connected as <strong>${who}</strong>`;
    statusEl.classList.add('connected');
    connectBtn.classList.add('hidden');
    disconnect.classList.remove('hidden');
  } else {
    statusEl.textContent = 'Not connected to Epic';
    statusEl.classList.remove('connected');
    connectBtn.classList.remove('hidden');
    disconnect.classList.add('hidden');
  }

  // The Connect button is only meaningful when the source is set to 'oauth' —
  // otherwise it's a no-op for the active source.  Dim it but still allow
  // pre-connecting before flipping the radio.
  connectBtn.style.opacity = value === 'oauth' ? '1' : '0.7';
}

async function handleEpicSourceChange(newSource) {
  if (!api) return;
  const r = await api.set_epic_source(newSource);
  if (r.status === 'ok') {
    showToast(`Epic source: ${newSource === 'oauth' ? 'Direct Epic' : 'GOG Galaxy'}`);
    // If switching to OAuth and not yet connected, auto-open the connect modal
    if (newSource === 'oauth') {
      const s = await api.epic_oauth_status();
      if (!s.connected) openEpicAuthModal();
    }
    refreshEpicSettings();
  } else {
    showToast(`Failed: ${r.message || r.status}`, 'error');
  }
}

async function disconnectEpic() {
  if (!api) return;
  if (!confirm('Disconnect Epic account? You can reconnect anytime.')) return;
  const r = await api.epic_oauth_disconnect();
  if (r.status === 'ok') {
    showToast('Epic disconnected');
    refreshEpicSettings();
  }
}

// ── OAuth modal ───────────────────────────────────────────────────────────

function openEpicAuthModal() {
  document.getElementById('epic-auth-modal').classList.remove('hidden');
  document.getElementById('epic-auth-code').value = '';
  setEpicAuthStatus('', '');
}

function closeEpicAuthModal() {
  document.getElementById('epic-auth-modal').classList.add('hidden');
}

function setEpicAuthStatus(text, kind) {
  const el = document.getElementById('epic-auth-status');
  el.textContent = text;
  el.className = 'epic-auth-status' + (kind ? ' ' + kind : '');
}

// Accept either a raw code OR a pasted JSON blob — extract authorizationCode
// from the latter so the user doesn't have to surgically copy just the value.
function extractAuthCode(input) {
  const trimmed = (input || '').trim();
  if (!trimmed) return '';
  // Try JSON parse first — Epic returns a JSON object with authorizationCode
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed === 'object' && parsed.authorizationCode) {
      return String(parsed.authorizationCode).trim();
    }
  } catch (_) { /* not JSON — fall through */ }
  // Fall back to assuming the user pasted just the code
  return trimmed;
}

async function openEpicLoginPage() {
  if (!api) return;
  const r = await api.epic_oauth_url();
  if (r.status === 'ok' && r.url) {
    // Open in the user's default browser via Windows shell
    await api.open_external_url(r.url);
    setEpicAuthStatus('Login page opened in browser', '');
  }
}

async function verifyEpicAuthCode() {
  if (!api) return;
  const raw  = document.getElementById('epic-auth-code').value;
  const code = extractAuthCode(raw);
  if (!code) {
    setEpicAuthStatus('Paste the authorization code first', 'error');
    return;
  }
  setEpicAuthStatus('Connecting…', '');
  const r = await api.epic_oauth_complete(code);
  if (r.status === 'ok') {
    const who = r.displayName ? r.displayName : 'your Epic account';
    setEpicAuthStatus(`Connected as ${who}!`, 'success');
    // Make sure the source is set to oauth now that auth succeeded
    await api.set_epic_source('oauth');
    setTimeout(() => {
      closeEpicAuthModal();
      refreshEpicSettings();
      showToast(`Epic connected: ${who}`);
    }, 900);
  } else {
    setEpicAuthStatus(r.message || 'Connection failed', 'error');
  }
}

function renderSettings(data) {
  const hiddenList   = document.getElementById('hidden-list');
  const excludedList = document.getElementById('excluded-list');
  hiddenList.innerHTML   = '';
  excludedList.innerHTML = '';

  if (!data.hidden_collections.length) {
    hiddenList.innerHTML = `<div class="settings-empty">No collections hidden. Hover over a collection card and click × to hide it.</div>`;
  } else {
    data.hidden_collections.forEach(c => {
      const row = document.createElement('div');
      row.className = 'settings-row';
      row.innerHTML = `
        <div><span class="settings-row-label">${esc(c.name)}</span><span class="settings-row-count">${c.count} games</span></div>
        <button class="settings-row-btn">Show again</button>
      `;
      row.querySelector('button').addEventListener('click', async () => {
        const r = await api.toggle_hide_collection(c.name);
        if (r.status === 'ok') {
          allCollections       = r.collections;
          allShortcutAppids    = r.shortcut_appids;
          allHiddenCollections = r.hidden_collections || [];
          openSettings();  // re-render with fresh data
          showToast(`"${c.name}" shown`);
        }
      });
      hiddenList.appendChild(row);
    });
  }

  // Steam excludes (numeric appid)
  const platformExcluded = data.excluded_platform_games || [];
  if (!data.excluded_games.length && !platformExcluded.length) {
    excludedList.innerHTML = `<div class="settings-empty">No games excluded. Use "Exclude from Future Spins" on the winner panel to exclude one.</div>`;
  } else {
    data.excluded_games.forEach(g => {
      const row = document.createElement('div');
      row.className = 'settings-row';
      row.innerHTML = `
        <div><span class="settings-row-label">${esc(g.name)}</span><span class="settings-row-count">Steam · appid ${g.appid}</span></div>
        <button class="settings-row-btn">Include again</button>
      `;
      row.querySelector('button').addEventListener('click', async () => {
        const r = await api.toggle_exclude(String(g.appid));
        if (r.status === 'ok') {
          allCollections       = r.collections;
          allShortcutAppids    = r.shortcut_appids;
          allHiddenCollections = r.hidden_collections || allHiddenCollections;
          openSettings();
          showToast(`"${g.name}" included again`);
        }
      });
      excludedList.appendChild(row);
    });
    // GOG/Epic excludes (prefixed string ID)
    platformExcluded.forEach(g => {
      const row = document.createElement('div');
      row.className = 'settings-row';
      const platformLabel = g.platform === 'gog' ? 'GOG' : g.platform === 'epic' ? 'Epic' : g.platform;
      row.innerHTML = `
        <div><span class="settings-row-label">${esc(g.name)}</span><span class="settings-row-count">${platformLabel}</span></div>
        <button class="settings-row-btn">Include again</button>
      `;
      row.querySelector('button').addEventListener('click', async () => {
        const r = await api.toggle_exclude_platform_game(g.id, g.name);
        if (r.status === 'ok') {
          openSettings();
          showToast(`"${g.name}" included again`);
        }
      });
      excludedList.appendChild(row);
    });
  }
}

// ── Per-platform user badges ────────────────────────────────────────────────
//
// Cached at module level so tab switches don't trigger refetches.  Populated
// once at startup; cleared+rebuilt when reload is called.

let platformUserInfo = null;   // { steam: {name,avatar}, gog: {...}, epic: {...} }

async function loadUserInfo() {
  if (!api) return;
  try {
    const r = await api.get_platform_user_info();
    if (r.status !== 'ok') return;
    platformUserInfo = {
      steam: r.steam || {name: null, avatar: null},
      gog:   r.gog   || {name: null, avatar: null},
      epic:  r.epic  || {name: null, avatar: null},
    };
    renderUserBadgeFor(currentPlatform);
  } catch (_) { /* silently ignore */ }
}

// Render the user badge for the active platform. On LITF we show all three.
function renderUserBadgeFor(platform) {
  const badge = document.getElementById('user-badge');
  if (!badge || !platformUserInfo) return;

  // Single platform — name + avatar (or placeholder dot if no avatar)
  const single = (info, label) => {
    const name = info.name || `${label} User`;
    const av   = info.avatar
      ? `<img src="${esc(info.avatar)}" alt="">`
      : `<span class="user-avatar-fallback">${label[0]}</span>`;
    return `${av}<span>${esc(name)}</span>`;
  };

  if (platform === 'all') {
    // Stacked: Steam · GOG · Epic, all three at once for the Leave It To Fate spin
    const parts = [];
    if (platformUserInfo.steam.name) parts.push(['S', platformUserInfo.steam]);
    if (platformUserInfo.gog.name)   parts.push(['G', platformUserInfo.gog]);
    if (platformUserInfo.epic.name)  parts.push(['E', platformUserInfo.epic]);
    if (parts.length === 0) { badge.classList.add('hidden'); return; }
    badge.classList.add('user-badge-stacked');
    badge.innerHTML = parts.map(([lbl, info]) => {
      const av = info.avatar
        ? `<img src="${esc(info.avatar)}" alt="" title="${esc(info.name)}">`
        : `<span class="user-avatar-fallback" title="${esc(info.name)}">${lbl}</span>`;
      return `<span class="user-badge-mini">${av}<span>${esc(info.name)}</span></span>`;
    }).join('');
    badge.classList.remove('hidden');
    return;
  }

  badge.classList.remove('user-badge-stacked');
  const info = platformUserInfo[platform];
  if (!info) { badge.classList.add('hidden'); return; }
  badge.innerHTML = single(info, ({steam:'Steam',gog:'GOG',epic:'Epic'}[platform] || ''));
  badge.classList.remove('hidden');
}

// ── Manage Non-Steam Shortcuts ────────────────────────────────────────────

let manageData = {
  shortcuts:   [],
  collections: [],
  selected:    new Set(),   // appids selected (multi-select)
  lastClicked: null,        // for Shift-click range select
};

async function openManageShortcuts() {
  if (!api) { showToast('Backend not available', 'error'); return; }
  const r = await api.get_shortcuts_with_assignments();
  if (r.status !== 'ok') {
    showToast(`Could not load shortcuts: ${r.message || r.status}`, 'error');
    return;
  }
  manageData.shortcuts   = r.shortcuts;
  manageData.collections = r.available_collections;
  manageData.selected    = new Set();
  manageData.lastClicked = null;

  document.getElementById('manage-search').value = '';
  document.getElementById('manage-subtitle').textContent =
    `${r.shortcuts.length} shortcuts · ${r.available_collections.length} collections · ` +
    `click to select, Ctrl-click to toggle, Shift-click for a range`;

  renderManageList('');
  renderManageDetail();
  showScreen('screen-manage-shortcuts');
}

function currentVisibleShortcuts(filterText) {
  const q = (filterText || '').trim().toLowerCase();
  return q ? manageData.shortcuts.filter(s => s.name.toLowerCase().includes(q))
           : manageData.shortcuts;
}

function renderManageList(filterText) {
  const list = document.getElementById('manage-list');
  list.innerHTML = '';
  const visible = currentVisibleShortcuts(filterText);
  visible.forEach(sc => {
    const row = document.createElement('div');
    row.className = 'manage-row';
    if (manageData.selected.has(sc.appid)) {
      row.classList.add('selected');
      if (manageData.selected.size > 1) row.classList.add('multi');
    }
    row.dataset.appid = sc.appid;
    const tags = sc.collections.length
      ? sc.collections.map(c => `<span class="chip-mini">${esc(c)}</span>`).join('')
      : `<span class="muted-mini">no collections assigned</span>`;
    row.innerHTML = `
      <div class="manage-row-name">${esc(sc.name)}</div>
      <div class="manage-row-tags">${tags}</div>
    `;
    row.addEventListener('click', (e) => handleShortcutClick(e, sc, filterText));
    list.appendChild(row);
  });
}

function handleShortcutClick(e, sc, filterText) {
  const filter = document.getElementById('manage-search').value;
  if (e.shiftKey && manageData.lastClicked != null) {
    // Range select within the currently-visible (filtered) list
    const visible = currentVisibleShortcuts(filter);
    const a = visible.findIndex(s => s.appid === manageData.lastClicked);
    const b = visible.findIndex(s => s.appid === sc.appid);
    if (a >= 0 && b >= 0) {
      const [from, to] = a <= b ? [a, b] : [b, a];
      for (let i = from; i <= to; i++) manageData.selected.add(visible[i].appid);
    }
  } else if (e.ctrlKey || e.metaKey) {
    if (manageData.selected.has(sc.appid)) manageData.selected.delete(sc.appid);
    else                                    manageData.selected.add(sc.appid);
    manageData.lastClicked = sc.appid;
  } else {
    manageData.selected.clear();
    manageData.selected.add(sc.appid);
    manageData.lastClicked = sc.appid;
  }
  renderManageList(filter);
  renderManageDetail();
}

function renderManageDetail() {
  const detail = document.getElementById('manage-detail');
  const selected = manageData.shortcuts.filter(s => manageData.selected.has(s.appid));
  if (selected.length === 0) {
    detail.innerHTML = `<div class="manage-empty">Select a shortcut on the left to assign collections.<br><br>Tip: Ctrl-click to add/remove from selection, Shift-click for a range.</div>`;
    return;
  }

  // Title + subtitle
  const titleText = selected.length === 1
    ? esc(selected[0].name)
    : `${selected.length} shortcuts selected`;
  const subText = selected.length === 1
    ? `appid ${selected[0].appid} · check a collection to add this shortcut to it`
    : (selected.slice(0, 4).map(s => esc(s.name)).join(', ') +
       (selected.length > 4 ? `, +${selected.length - 4} more` : '')) +
      ` · checking a box adds ALL selected shortcuts to that collection`;

  detail.innerHTML = `
    <div class="manage-detail-title">${titleText}</div>
    <div class="manage-detail-sub">${subText}</div>
    <div class="manage-checkboxes" id="manage-checkboxes"></div>
  `;

  const box = document.getElementById('manage-checkboxes');
  manageData.collections.forEach(cname => {
    // Determine state across all selected shortcuts: none / some / all
    const count = selected.filter(s => s.collections.includes(cname)).length;
    const state = count === 0 ? 'none' : count === selected.length ? 'all' : 'some';

    const label = document.createElement('label');
    label.className = 'manage-check'
                    + (state === 'all'  ? ' checked'       : '')
                    + (state === 'some' ? ' indeterminate' : '');
    label.innerHTML = `
      <input type="checkbox" ${state === 'all' ? 'checked' : ''}>
      <span>${esc(cname)}</span>
    `;
    const cb = label.querySelector('input');
    if (state === 'some') cb.indeterminate = true;

    cb.addEventListener('change', async (e) => {
      const shouldAdd = e.target.checked;
      const updates = selected.map(sc => {
        const set = new Set(sc.collections);
        if (shouldAdd) set.add(cname); else set.delete(cname);
        return { appid: sc.appid, collections: Array.from(set) };
      });
      const r = await api.batch_set_shortcut_collections(updates);
      if (r.status === 'ok') {
        // Mutate in-memory shortcut state to match
        selected.forEach(sc => {
          const set = new Set(sc.collections);
          if (shouldAdd) set.add(cname); else set.delete(cname);
          sc.collections = Array.from(set);
        });
        allCollections    = r.collections;
        allShortcutAppids = r.shortcut_appids;
        renderManageList(document.getElementById('manage-search').value);
        renderManageDetail();
      } else {
        showToast(`Save failed: ${r.message || r.status}`, 'error');
      }
    });
    box.appendChild(label);
  });
}

async function reloadCollections() {
  if (!api) return;
  // Library composition may have changed (new game purchased, etc.) so the
  // dedup AND edition decisions need to be recomputed from scratch.
  invalidateAllExcludes();
  const btn = document.getElementById('btn-reload-main');
  btn.classList.add('spinning');
  btn.disabled = true;

  const grid  = document.getElementById('collection-grid');
  const empty = document.getElementById('empty-state');

  if (currentPlatform !== 'steam') {
    // Refresh GOG / Epic / All — re-fetch in parallel with the spinner delay
    grid.innerHTML = '';
    await Promise.all([
      (async () => {
        if (currentPlatform === 'gog')       await loadGogGrid(grid, empty);
        if (currentPlatform === 'epic')      await loadEpicGrid(grid, empty);
        if (currentPlatform === 'battlenet') await loadBattlenetGrid(grid, empty);
        if (currentPlatform === 'origin')    await loadOriginGrid(grid, empty);
        if (currentPlatform === 'uplay')     await loadUplayGrid(grid, empty);
        // 'all' is the LITF action button — re-running it means re-spinning
        if (currentPlatform === 'all')       await leaveItToFate();
      })(),
      delay(800),
    ]);
    btn.classList.remove('spinning');
    btn.disabled = false;
    return;
  }

  // Steam: reload the collections file, keep the 800ms minimum for the animation
  const [result] = await Promise.all([api.reload_collections(), delay(800)]);
  btn.classList.remove('spinning');
  btn.disabled = false;

  if (result.status === 'ok') {
    renderCollections(result.collections, result.shortcut_appids, result.hidden_collections);
    const n = (result.collections || []).length;
    showToast(`Collections updated · ${n} collection${n === 1 ? '' : 's'}`);
  } else {
    showToast(`Update failed: ${result.message || result.status}`, 'error');
  }
}

function showDebugModal(text) {
  const modal = document.getElementById('debug-modal');
  document.getElementById('debug-modal-content').textContent = text;
  modal.classList.remove('hidden');
}

function hideDebugModal() {
  const modal = document.getElementById('debug-modal');
  if (modal) modal.classList.add('hidden');
}

async function copyDebugLog() {
  const text = document.getElementById('debug-modal-content').textContent;
  // Try the modern clipboard API first
  try {
    await navigator.clipboard.writeText(text);
    showToast('Debug log copied to clipboard');
    return;
  } catch (_) { /* fall through */ }
  // Fallback: select-and-execCommand (works in WebView2 even without HTTPS)
  try {
    const pre = document.getElementById('debug-modal-content');
    const range = document.createRange();
    range.selectNodeContents(pre);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    const ok = document.execCommand('copy');
    sel.removeAllRanges();
    showToast(ok ? 'Debug log copied to clipboard' : 'Copy failed — use Save instead',
              ok ? 'success' : 'error');
  } catch (e) {
    showToast('Copy failed — use Save instead', 'error');
  }
}

async function saveDebugLog() {
  if (!api) { showToast('Backend not available', 'error'); return; }
  const text = document.getElementById('debug-modal-content').textContent;
  const result = await api.save_debug_log(text);
  if (result.status === 'ok') {
    showToast(`Saved to ${result.path}`);
  } else if (result.status === 'cancelled') {
    /* user cancelled — no toast */
  } else {
    showToast(`Save failed: ${result.message || result.status}`, 'error');
  }
}

async function debugCurrentCollection() {
  if (!api) { showDebugModal('Backend not available.'); return; }
  const name = currentCollection ? currentCollection.name : null;

  const calls = [
    api.debug_all_keys(),
    api.debug_shortcuts(),
  ];
  if (name) calls.unshift(api.debug_collection(name));
  const results = await Promise.all(calls);
  const [coll, keys, scuts] = name ? results : [null, ...results];

  let out = '';

  if (coll) {
    if (coll.status === 'ok') {
      const samples = coll.added_samples.slice(0, 15)
        .map(s => `  ${s.type.padEnd(6)} ${JSON.stringify(s.value)}`).join('\n');
      out += `=== Collection: ${coll.name} ===\n`
          +  `Keys: ${coll.keys.join(', ')}\n`
          +  `'added' length: ${coll.added_count}\n\n`
          +  `First 15 entries:\n${samples}\n\n`;
    } else {
      out += `Collection debug failed: ${coll.message || coll.status}\n\n`;
    }
  } else {
    out += `(Open a collection first to see its raw data here)\n\n`;
  }

  if (keys.status === 'ok') {
    const summary = Object.entries(keys.by_prefix)
      .sort((a, b) => b[1] - a[1])
      .map(([p, n]) => `  ${String(n).padStart(5)}  ${p}`).join('\n');
    out += `=== All ${keys.total_entries} JSON entries by prefix ===\n${summary}\n\n`;

    if (keys.user_subtypes) {
      const subs = Object.entries(keys.user_subtypes)
        .sort((a, b) => b[1] - a[1])
        .map(([p, n]) => {
          const eg = (keys.user_examples?.[p] || []).slice(0, 2).join(', ');
          return `  ${String(n).padStart(5)}  ${p.padEnd(28)} e.g. ${eg}`;
        }).join('\n');
      out += `=== user-* sub-prefixes ===\n${subs}\n\n`;
    }

    if (keys.notable_keys && keys.notable_keys.length) {
      out += `=== Keys mentioning "shortcut" or "collection" ===\n`;
      keys.notable_keys.forEach(k => { out += `  ${k}\n`; });
      out += '\n';
    }

    if (keys.shortcut_id_search) {
      const s = keys.shortcut_id_search;
      out += `=== Searching JSON for ANY of ${s.shortcut_count} shortcut appids ===\n`;
      out += `Entries that reference a shortcut: ${s.matching_entries}\n\n`;
      if (s.matches && s.matches.length) {
        s.matches.forEach(m => {
          out += `→ ${m.key}  (${m.hit_count} hits, e.g. ${m.hit_samples.join(', ')})\n`;
          out += `  ${m.preview}\n\n`;
        });
      } else {
        out += `  (no entries in the JSON reference any shortcut appid — Steam\n`;
        out += `   isn't writing shortcut→collection memberships into this file\n`;
        out += `   for the new Collections feature.  Check cloudstorage_dir_files\n`;
        out += `   below for sibling JSON files.)\n\n`;
      }
    }

    if (keys.cloudstorage_dir_files) {
      out += `=== Files in ${keys.cloudstorage_folder} ===\n`;
      keys.cloudstorage_dir_files.forEach(f => {
        out += `  ${String(f.size).padStart(10)} bytes  ${f.name}\n`;
      });
      out += '\n';
    }

    if (keys.small_cloudstorage_files && Object.keys(keys.small_cloudstorage_files).length) {
      out += `=== Contents of tiny cloudstorage files ===\n`;
      for (const [name, content] of Object.entries(keys.small_cloudstorage_files)) {
        out += `── ${name} ──\n${content}\n\n`;
      }
    }

    if (keys.config_probe && keys.config_probe.length) {
      out += `=== Probing Steam config files for shortcut memberships ===\n`;
      keys.config_probe.forEach(p => {
        out += `${p.path}\n  exists: ${p.exists}`;
        if (p.exists) {
          out += `  size: ${p.size} bytes\n`;
          out += `  shortcut appid hits: ${p.shortcut_id_hits}`;
          if (p.sample_id_hits && p.sample_id_hits.length) {
            out += `  e.g. ${p.sample_id_hits.join(', ')}`;
          }
          out += `\n  collection name hits (${(p.collection_name_hits || []).length}): ${JSON.stringify(p.collection_name_hits || [])}\n`;
          if (p.collection_id_contexts && p.collection_id_contexts.length) {
            p.collection_id_contexts.forEach(c => {
              out += `\n  ── context around collection ${c.id} (${c.name}) at offset ${c.offset} ──\n`;
              out += c.context + '\n';
              out += `  ── end ──\n`;
            });
          } else if (p.context_around_first_hit) {
            out += `\n  context around first shortcut-id hit:\n  ─────\n${p.context_around_first_hit}\n  ─────\n`;
          }
        } else {
          out += '\n';
        }
        out += '\n';
      });
    }
  }

  if (scuts.status === 'ok') {
    out += `=== shortcuts.vdf ===\n`;
    out += `Path:   ${scuts.vdf_path}\n`;
    out += `Exists: ${scuts.vdf_exists}\n`;
    if (scuts.vdf_exists) {
      out += `Size:   ${scuts.vdf_size_bytes} bytes\n`;
      out += `Total shortcuts parsed: ${scuts.total_shortcuts}\n\n`;
      out += `Unique tags found across all shortcuts: ${scuts.unique_tags.length}\n`;
      out += `  Matching a collection name: ${JSON.stringify(scuts.tags_matching_collections)}\n`;
      out += `  Not matching:               ${JSON.stringify(scuts.tags_not_matching)}\n\n`;
      out += `Collection names we have:\n  ${scuts.collection_names.join('\n  ')}\n\n`;
      out += `First 30 shortcuts (appid · name · tags):\n`;
      scuts.shortcuts.forEach(sc => {
        out += `  ${String(sc.appid).padStart(12)}  ${(sc.name || '(no name)').padEnd(40)}  ${JSON.stringify(sc.tags)}\n`;
      });
    }
  } else {
    out += `shortcuts debug failed: ${scuts.message || scuts.status}\n`;
  }

  showDebugModal(out);
}

// ── Platform switching ─────────────────────────────────────────────────────

async function switchPlatform(platform) {
  currentPlatform = platform;

  // Update tab highlights
  document.querySelectorAll('.platform-tab').forEach(t => t.classList.remove('active'));
  document.getElementById(`tab-${platform}`).classList.add('active');

  // Swap the user badge for the active platform
  renderUserBadgeFor(platform);

  // Collection Roulette only applies to Steam
  document.getElementById('btn-coll-roulette').style.display =
    platform === 'steam' ? '' : 'none';

  if (platform === 'steam') {
    renderCollections(allCollections, allShortcutAppids, allHiddenCollections);
    return;
  }

  const grid  = document.getElementById('collection-grid');
  const empty = document.getElementById('empty-state');
  grid.innerHTML = '';
  empty.classList.add('hidden');
  showScreen('screen-main');

  if (platform === 'gog')       await loadGogGrid(grid, empty);
  if (platform === 'epic')      await loadEpicGrid(grid, empty);
  if (platform === 'battlenet') await loadBattlenetGrid(grid, empty);
  if (platform === 'origin')    await loadOriginGrid(grid, empty);
  if (platform === 'uplay')     await loadUplayGrid(grid, empty);
}

async function loadOriginGrid(grid, empty) {
  if (!api) { empty.classList.remove('hidden'); return; }
  const [result, tagResult] = await Promise.all([
    api.get_origin_games(),
    api.get_galaxy_collections('origin'),
  ]);
  if (result.status === 'ok' && result.games.length > 0) {
    originGames = await filterDuplicates(result.games);
    grid.appendChild(makePlatformCard('origin', originGames));
    appendTagCollections(grid, 'origin', tagResult, originGames);
    empty.classList.add('hidden');
    if (result.source === 'native') {
      const note = document.createElement('div');
      note.className = 'platform-hint';
      note.innerHTML =
        `Only showing <strong>installed</strong> EA / Origin games. ` +
        `To see your full owned library, install the ` +
        `<strong>EA / Origin integration</strong> in GOG Galaxy ` +
        `(Settings → Integrations → search "Origin").`;
      grid.appendChild(note);
    }
  } else {
    originGames = [];
    empty.innerHTML =
      '<p>No EA / Origin games found.</p>' +
      '<p class="hint">For your <strong>full owned EA library</strong>, install the ' +
      'Origin integration in GOG Galaxy (Settings → Integrations → search "Origin"). ' +
      'Without it, only games with a footprint in your local registry will appear.</p>';
    empty.classList.remove('hidden');
  }
}

async function loadUplayGrid(grid, empty) {
  if (!api) { empty.classList.remove('hidden'); return; }
  const [result, tagResult] = await Promise.all([
    api.get_uplay_games(),
    api.get_galaxy_collections('uplay'),
  ]);
  if (result.status === 'ok' && result.games.length > 0) {
    uplayGames = await filterDuplicates(result.games);
    grid.appendChild(makePlatformCard('uplay', uplayGames));
    appendTagCollections(grid, 'uplay', tagResult, uplayGames);
    empty.classList.add('hidden');
    if (result.source === 'native') {
      const note = document.createElement('div');
      note.className = 'platform-hint';
      note.innerHTML =
        `Only showing <strong>installed</strong> Ubisoft games (and Ubisoft Connect ` +
        `no longer populates per-game registry data, so names are partial). For your ` +
        `full owned library plus proper names, install the <strong>Ubisoft Connect ` +
        `integration</strong> in GOG Galaxy (Settings → Integrations → search "Ubisoft").`;
      grid.appendChild(note);
    }
  } else {
    uplayGames = [];
    empty.innerHTML =
      '<p>No Ubisoft Connect games found.</p>' +
      '<p class="hint">For your <strong>full owned Ubisoft library</strong>, install the ' +
      'Ubisoft Connect integration in GOG Galaxy (Settings → Integrations → search "Ubisoft").</p>';
    empty.classList.remove('hidden');
  }
}

async function loadBattlenetGrid(grid, empty) {
  if (!api) { empty.classList.remove('hidden'); return; }
  const [result, tagResult] = await Promise.all([
    api.get_battlenet_games(),
    api.get_galaxy_collections('battlenet'),
  ]);
  if (result.status === 'ok' && result.games.length > 0) {
    battlenetGames = await filterDuplicates(result.games);
    grid.appendChild(makePlatformCard('battlenet', battlenetGames));
    appendTagCollections(grid, 'battlenet', tagResult, battlenetGames);
    empty.classList.add('hidden');

    // Native source = installed games only — surface that to the user so
    // they understand why their library looks smaller than expected.
    if (result.source === 'native') {
      const note = document.createElement('div');
      note.className = 'platform-hint';
      note.innerHTML =
        `Only showing <strong>installed</strong> Battle.net games. ` +
        `To see your full owned library, install the ` +
        `<strong>Battle.net integration</strong> in GOG Galaxy ` +
        `(Settings → Integrations → search "Battle.net").`;
      grid.appendChild(note);
    }
  } else {
    battlenetGames = [];
    empty.innerHTML =
      '<p>No Battle.net games found.</p>' +
      '<p class="hint">For your <strong>full owned Battle.net library</strong>, install the ' +
      'Battle.net integration in GOG Galaxy (Settings → Integrations → search "Battle.net"). ' +
      'Without it, only installed Blizzard games will appear.</p>';
    empty.classList.remove('hidden');
  }
}

// ── Leave It To Fate — combined-platform auto-spinning button ──────────────
//
// Behaves more like an action button than a tab: clicking it fetches every
// available game across Steam, GOG, and Epic, jumps straight to the spin
// screen, and fires off the wheel automatically.  Each click = a fresh spin.

async function leaveItToFate() {
  // Visual: light up the LITF tab, dim the others
  document.querySelectorAll('.platform-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-litf').classList.add('active');
  document.getElementById('btn-coll-roulette').style.display = 'none';
  currentPlatform = 'all';
  renderUserBadgeFor('all');

  // Fetch every non-Steam platform in parallel; Steam is already in memory.
  // Each fetch is gated by the per-launcher "enabled" setting — disabled
  // launchers are excluded from Leave It To Fate entirely.
  const enabledIds = launcherStatusCache
    ? new Set(launcherStatusCache.filter(l => l.enabled).map(l => l.id))
    : new Set(['steam', 'gog', 'epic', 'battlenet', 'origin', 'uplay']);
  const empty = {status: 'ok', games: []};
  const fetchIf = (id, fn) => enabledIds.has(id) ? fn() : Promise.resolve(empty);

  const [gogResult, epicResult, bnetResult, originResult, uplayResult] = api
    ? await Promise.all([
        fetchIf('gog',       () => api.get_gog_games()),
        fetchIf('epic',      () => api.get_epic_games()),
        fetchIf('battlenet', () => api.get_battlenet_games()),
        fetchIf('origin',    () => api.get_origin_games()),
        fetchIf('uplay',     () => api.get_uplay_games()),
      ])
    : [empty, empty, empty, empty, empty];
  gogGames       = gogResult.status    === 'ok' ? gogResult.games    : [];
  epicGames      = epicResult.status   === 'ok' ? epicResult.games   : [];
  battlenetGames = bnetResult.status   === 'ok' ? bnetResult.games   : [];
  originGames    = originResult.status === 'ok' ? originResult.games : [];
  uplayGames     = uplayResult.status  === 'ok' ? uplayResult.games  : [];

  let steamIds = enabledIds.has('steam') ? [...new Set([
    ...allCollections.flatMap(c => c.appids),
    ...allShortcutAppids,
  ])].map(id => ({ id: `steam_${id}`, raw_id: id, name: null, platform: 'steam' })) : [];

  // Apply cross-platform dedup if enabled
  const filteredSteam  = await filterDuplicates(steamIds);
  const filteredGog    = await filterDuplicates(gogGames);
  const filteredEpic   = await filterDuplicates(epicGames);
  const filteredBnet   = await filterDuplicates(battlenetGames);
  const filteredOrigin = await filterDuplicates(originGames);
  const filteredUplay  = await filterDuplicates(uplayGames);
  // Mutate module-level lists so the spin sources match what we just spawned
  gogGames       = filteredGog;
  epicGames      = filteredEpic;
  battlenetGames = filteredBnet;
  originGames    = filteredOrigin;
  uplayGames     = filteredUplay;

  const allGames = [
    ...filteredSteam, ...filteredGog, ...filteredEpic,
    ...filteredBnet,  ...filteredOrigin, ...filteredUplay,
  ];
  if (allGames.length === 0) {
    showToast('No games found across any platform', 'error');
    return;
  }

  // Wire up the spin screen for the combined pool
  spinMode             = 'platform';
  currentPlatformGames = allGames;
  prevGameWinner       = null;   // never start the reel on a stale game
  pendingStartFrom     = null;

  document.getElementById('spin-coll-name').textContent  = 'Leave It To Fate';
  const parts = [];
  if (steamIds.length)       parts.push(`${steamIds.length.toLocaleString()} Steam`);
  if (gogGames.length)       parts.push(`${gogGames.length.toLocaleString()} GOG`);
  if (epicGames.length)      parts.push(`${epicGames.length.toLocaleString()} Epic`);
  if (battlenetGames.length) parts.push(`${battlenetGames.length.toLocaleString()} Battle.net`);
  if (originGames.length)    parts.push(`${originGames.length.toLocaleString()} EA`);
  if (uplayGames.length)     parts.push(`${uplayGames.length.toLocaleString()} Ubisoft`);
  document.getElementById('spin-coll-count').textContent = parts.join(' · ');

  document.getElementById('footer-spin').classList.remove('hidden');
  document.getElementById('footer-winner').classList.add('hidden');
  document.getElementById('btn-spin').disabled    = false;
  document.getElementById('btn-spin').textContent = 'SPIN';
  currentWinnerAppid = null;

  buildPlatformReel(allGames);
  showScreen('screen-spin');

  // Brief pause so the user sees the reel land at rest, then fire automatically
  await delay(280);
  doSpin();
}

async function loadGogGrid(grid, empty) {
  if (!api) { empty.classList.remove('hidden'); return; }
  const [result, tagResult] = await Promise.all([
    api.get_gog_games(),
    api.get_galaxy_collections('gog'),
  ]);
  if (result.status === 'ok' && result.games.length > 0) {
    gogGames = await filterDuplicates(result.games);
    grid.appendChild(makePlatformCard('gog', gogGames));
    appendTagCollections(grid, 'gog', tagResult, gogGames);
    empty.classList.add('hidden');
  } else {
    gogGames = [];
    empty.innerHTML = '<p>No GOG games found. GOG Galaxy must be installed with at least one game.</p>';
    empty.classList.remove('hidden');
  }
}

async function loadEpicGrid(grid, empty) {
  if (!api) { empty.classList.remove('hidden'); return; }
  const [result, tagResult] = await Promise.all([
    api.get_epic_games(),
    api.get_galaxy_collections('epic'),
  ]);
  if (result.status === 'ok' && result.games.length > 0) {
    epicGames = await filterDuplicates(result.games);
    grid.appendChild(makePlatformCard('epic', epicGames));
    appendTagCollections(grid, 'epic', tagResult, epicGames);
    empty.classList.add('hidden');

    // If we're only seeing installed games, suggest the Galaxy integration
    // so the user can get their full owned library too.
    if (result.source === 'manifests') {
      const note = document.createElement('div');
      note.className = 'platform-hint';
      note.innerHTML =
        `Only showing <strong>installed</strong> Epic games. ` +
        `To see your full owned library, install the ` +
        `<strong>Epic Games integration</strong> in GOG Galaxy ` +
        `(Settings → Integrations → search "Epic").`;
      grid.appendChild(note);
    }
  } else {
    epicGames = [];
    empty.innerHTML =
      '<p>No Epic games found.</p>' +
      '<p class="hint">For your <strong>full owned Epic library</strong>, install the ' +
      'Epic Games integration in GOG Galaxy (Settings → Integrations → search "Epic"). ' +
      'Without it, only installed Epic games will show up.</p>';
    empty.classList.remove('hidden');
  }
}

// Append per-tag "collection" cards alongside the full-library card. Tags come
// from Galaxy's UserReleaseTags table (whatever the user has manually labeled
// games with in GOG Galaxy). Each tag becomes its own clickable card that
// spins only within that tag's games.
function appendTagCollections(grid, platform, tagResult, games) {
  if (!tagResult || tagResult.status !== 'ok' || !tagResult.collections.length) return;
  if (!games || !games.length) return;

  // Build a lookup so we can match a Galaxy releaseKey to one of our game
  // objects. For Epic, Galaxy keys on app_name (epic_<AppName>) while our
  // OAuth library keys on catalogItemId — so we have to try both.
  const byKey = {};
  games.forEach(g => {
    if (g.id) byKey[g.id] = g;
    if (platform === 'epic' && g.app_name) byKey[`epic_${g.app_name}`] = g;
  });

  tagResult.collections.forEach(tagColl => {
    const tagGames = tagColl.appids
      .map(k => byKey[k])
      .filter(Boolean);
    if (!tagGames.length) return;
    grid.appendChild(makeTagCard(platform, tagColl.name, tagGames));
  });
}

function makeTagCard(platform, tagName, games) {
  const card = document.createElement('div');
  const CLASS = { gog: 'coll-card-gog', epic: 'coll-card-epic',
                  battlenet: 'coll-card-battlenet',
                  origin: 'coll-card-origin', uplay: 'coll-card-uplay' }[platform] || '';
  card.className = `coll-card ${CLASS} coll-card-tag`;
  card.innerHTML = `
    <div class="coll-name">${esc(tagName)}</div>
    <div class="coll-count">${games.length.toLocaleString()} game${games.length === 1 ? '' : 's'}</div>
  `;
  card.addEventListener('click', () => openPlatformSpin(platform, games));

  // Pick a random game's art as the card background, just like the full-library card
  const withArt = games.filter(g => g.image_background);
  if (withArt.length > 0) {
    const candidates = [...withArt].sort(() => Math.random() - 0.5).slice(0, 5);
    let i = 0;
    const tryNext = () => {
      if (i >= candidates.length) return;
      const url = candidates[i++].image_background;
      const img = new Image();
      img.onload = () => {
        card.style.backgroundImage = `url('${url}')`;
        card.classList.add('has-bg-art');
      };
      img.onerror = tryNext;
      img.src = url;
    };
    tryNext();
  }
  return card;
}

function makePlatformCard(platform, games, subtitle = null) {
  const card = document.createElement('div');
  const NAMES   = { gog: 'GOG Library', epic: 'Epic Library', battlenet: 'Battle.net Library',
                    origin: 'EA App Library', uplay: 'Ubisoft Library', all: 'All Libraries' };
  const CLASSES = { gog: 'coll-card-gog', epic: 'coll-card-epic', battlenet: 'coll-card-battlenet',
                    origin: 'coll-card-origin', uplay: 'coll-card-uplay', all: 'coll-card-all' };
  card.className = `coll-card ${CLASSES[platform] || ''}`;

  const sub = subtitle !== null
    ? subtitle
    : `${games.length.toLocaleString()} game${games.length === 1 ? '' : 's'}`;

  card.innerHTML = `
    <div class="coll-name">${esc(NAMES[platform] || platform)}</div>
    <div class="coll-count">${esc(sub)}</div>
  `;
  card.addEventListener('click', () => openPlatformSpin(platform, games));

  // Background art: try a random game's cover.  For GOG/Epic we have Galaxy-
  // enriched image_background URLs; for 'all' mode (legacy code path) we use
  // Steam header images.
  const artCandidates = (() => {
    if (platform === 'gog' || platform === 'epic') {
      const withArt = games.filter(g => g.image_background);
      return [...withArt].sort(() => Math.random() - 0.5).slice(0, 5)
                         .map(g => [g.image_background]);
    }
    if (platform === 'all') {
      const steamGames = games.filter(g => g.platform === 'steam' && !isLikelyNonSteam(g.raw_id));
      return [...steamGames].sort(() => Math.random() - 0.5).slice(0, 5)
                            .map(g => headerUrls(g.raw_id));
    }
    return [];
  })();
  if (artCandidates.length > 0) {
    let ci = 0;
    const tryNextGame = () => {
      if (ci >= artCandidates.length) return;
      const urls = artCandidates[ci++];
      let ui = 0;
      const tryNextUrl = () => {
        if (ui >= urls.length) { tryNextGame(); return; }
        const img = new Image();
        img.onload = () => {
          card.style.backgroundImage = `url('${img.src}')`;
          card.classList.add('has-bg-art');
        };
        img.onerror = () => { ui++; tryNextUrl(); };
        img.src = urls[ui];
      };
      tryNextUrl();
    };
    tryNextGame();
  }
  return card;
}

function openPlatformSpin(platform, games) {
  spinMode             = 'platform';
  currentPlatformGames = games;

  const NAMES = { gog: 'GOG Library', epic: 'Epic Library', battlenet: 'Battle.net Library',
                  origin: 'EA App Library', uplay: 'Ubisoft Library', all: 'All Libraries' };
  document.getElementById('spin-coll-name').textContent  = NAMES[platform] || platform;
  document.getElementById('spin-coll-count').textContent =
    `${games.length.toLocaleString()} game${games.length === 1 ? '' : 's'}`;

  document.getElementById('footer-spin').classList.remove('hidden');
  document.getElementById('footer-winner').classList.add('hidden');
  document.getElementById('btn-spin').disabled    = false;
  document.getElementById('btn-spin').textContent = 'SPIN';
  currentWinnerAppid = null;

  buildPlatformReel(games);
  showScreen('screen-spin');
}

function buildPlatformReel(games, startFrom = null, forcedWinner = null) {
  const winner = (forcedWinner && games.find(g => g.id === forcedWinner.id))
    ? forcedWinner
    : randItem(games);
  const pool    = games.length > 1 ? games.filter(g => g.id !== winner.id) : games;
  const fillers = Array.from({ length: N_FILLERS }, (_, i) =>
    i === 0 && startFrom !== null ? startFrom : randItem(pool)
  );
  const sequence = [...fillers, winner];

  const reel = document.getElementById('reel');
  reel.innerHTML = '';
  reel.style.transform = 'translateY(0)';
  reel.style.filter    = '';

  // Show platform badge only in mixed-platform (All) mode
  const isMulti = new Set(games.map(g => g.platform)).size > 1;
  const PLABELS = { gog: 'GOG', epic: 'Epic', steam: 'Steam',
                    battlenet: 'Battle.net', origin: 'EA', uplay: 'Ubi' };

  sequence.forEach((game, i) => {
    const isWinner = i === sequence.length - 1;
    const card = document.createElement('div');

    if (game.platform === 'steam') {
      // Steam games: use header image (same as regular game reel)
      card.className = 'reel-card' + (isWinner ? ' reel-winner' : '');
      if (isLikelyNonSteam(game.raw_id)) {
        card.classList.add('non-steam-card');
        card.appendChild(nonSteamPlaceholder('', game.raw_id));
      } else {
        const img = document.createElement('img');
        img.alt = ''; img.draggable = false;
        attachImgFallback(img, game.raw_id);
        img.src = headerUrl(game.raw_id);
        card.appendChild(img);
      }
    } else {
      // GOG / Epic: use cover art if Galaxy enrichment provided one;
      // otherwise fall back to a text card with the title + platform badge.
      const imgUrl = game.image_background || game.image_vertical || '';
      const badgeHtml = isMulti
        ? `<span class="platform-badge platform-badge--${game.platform}">${PLABELS[game.platform] || ''}</span>`
        : '';
      if (imgUrl) {
        card.className = 'reel-card reel-card-platform-img' + (isWinner ? ' reel-winner' : '');
        const img = document.createElement('img');
        img.alt = ''; img.draggable = false;
        // Galaxy's image CDN sometimes 404s on a specific image — gracefully
        // fall back to a text card so the reel doesn't show a broken icon.
        img.onerror = () => {
          card.className = 'reel-card reel-card-platform' + (isWinner ? ' reel-winner' : '');
          card.innerHTML = `
            <div class="reel-card-platform-inner">
              <div class="platform-game-name">${esc(game.name || game.id)}</div>
              ${badgeHtml}
            </div>
          `;
        };
        img.src = imgUrl;
        card.appendChild(img);
        if (isMulti) {
          const badge = document.createElement('div');
          badge.className = 'reel-card-platform-overlay';
          badge.innerHTML = badgeHtml;
          card.appendChild(badge);
        }
      } else {
        card.className = 'reel-card reel-card-platform' + (isWinner ? ' reel-winner' : '');
        card.innerHTML = `
          <div class="reel-card-platform-inner">
            <div class="platform-game-name">${esc(game.name || game.id)}</div>
            ${badgeHtml}
          </div>
        `;
      }
    }
    reel.appendChild(card);
  });

  return winner;
}

function showPlatformWinner(game) {
  prevGameWinner     = game;   // spin-again re-uses this as startFrom
  currentWinnerAppid = game.id;

  // Show the Exclude button — wired to the GOG/Epic exclusion API
  const exclBtn = document.getElementById('btn-exclude');
  exclBtn.classList.remove('hidden');
  exclBtn.onclick = () => excludePlatformWinningGame(game);

  document.getElementById('winner-coll-card').classList.add('hidden');
  document.getElementById('btn-spin-game').classList.add('hidden');
  document.getElementById('btn-spin-again').textContent = 'Spin Again';

  const nameEl  = document.getElementById('winner-name');
  const metaEl  = document.getElementById('winner-meta');
  const hltbRow = document.getElementById('hltb-row');
  nameEl.classList.remove('hidden');
  metaEl.textContent = ''; metaEl.classList.add('hidden');
  if (hltbRow) { hltbRow.classList.add('hidden'); hltbRow.innerHTML = ''; }

  const launchBtn = document.getElementById('btn-launch');
  launchBtn.classList.remove('hidden');

  // Build a meta line: "<Platform> · <playtime> played" (playtime optional)
  const _platformMeta = (label, game) => {
    const pretty = formatPlaytime(game.playtime_minutes || 0);
    return pretty ? `${label} · ${pretty} played` : label;
  };

  if (game.platform === 'gog') {
    nameEl.textContent = game.name;
    metaEl.textContent = _platformMeta('GOG Galaxy', game);
    metaEl.classList.remove('hidden');
    launchBtn.onclick  = async () => {
      if (!api) return;
      const r = await api.launch_gog_game(game.raw_id, game.source);
      reportLaunch(r);
    };
    if (game.name) loadHltbData(game.id, game.name);

  } else if (game.platform === 'epic') {
    nameEl.textContent = game.name;
    metaEl.textContent = _platformMeta('Epic Games', game);
    metaEl.classList.remove('hidden');
    launchBtn.onclick  = async () => {
      if (!api) return;
      // app_name is present on OAuth-sourced games — lets us skip Galaxy
      // and launch via Epic's own URI scheme directly.
      const r = await api.launch_epic_game(game.raw_id, game.source, game.app_name || null);
      reportLaunch(r);
    };
    if (game.name) loadHltbData(game.id, game.name);

  } else if (game.platform === 'battlenet') {
    nameEl.textContent = game.name;
    metaEl.textContent = _platformMeta('Battle.net', game);
    metaEl.classList.remove('hidden');
    launchBtn.onclick  = async () => {
      if (!api) return;
      const r = await api.launch_battlenet_game(game.raw_id, game.source);
      reportLaunch(r);
    };
    if (game.name) loadHltbData(game.id, game.name);

  } else if (game.platform === 'origin') {
    nameEl.textContent = game.name;
    metaEl.textContent = _platformMeta('EA App', game);
    metaEl.classList.remove('hidden');
    launchBtn.onclick  = async () => {
      if (!api) return;
      const r = await api.launch_origin_game(game.raw_id, game.source);
      reportLaunch(r);
    };
    if (game.name) loadHltbData(game.id, game.name);

  } else if (game.platform === 'uplay') {
    nameEl.textContent = game.name;
    metaEl.textContent = _platformMeta('Ubisoft Connect', game);
    metaEl.classList.remove('hidden');
    launchBtn.onclick  = async () => {
      if (!api) return;
      const r = await api.launch_uplay_game(game.raw_id, game.source);
      reportLaunch(r);
    };
    if (game.name) loadHltbData(game.id, game.name);

  } else {
    // Steam game (from All mode) — fetch name + playtime async
    nameEl.textContent = 'Loading…';
    launchBtn.onclick  = () => api && api.launch_game(String(game.raw_id));
    if (api) {
      api.get_game_name(String(game.raw_id)).then(result => {
        if (currentWinnerAppid !== game.id) return;
        const name = result.status === 'ok' ? result.name : `App ${game.raw_id}`;
        nameEl.textContent = name;
        const pretty = formatPlaytime(result.playtime_minutes || 0);
        metaEl.textContent = 'Steam' + (pretty ? ` · ${pretty} played` : '');
        metaEl.classList.remove('hidden');
        if (result.status === 'ok' && !isLikelyNonSteam(game.raw_id)) {
          loadHltbData(game.id, name);
        }
      });
    } else {
      nameEl.textContent = `App ${game.raw_id}`;
    }
  }
}

// ── Init ──────────────────────────────────────────────────────────────────

let _initRan = false;
async function init() {
  // pywebview fires both DOMContentLoaded AND pywebviewready, each calling
  // this function.  We only want to wire listeners once, but the SECOND
  // call is often when pywebview's API actually becomes available — so
  // when we re-enter, promote the api ref and trigger the real load.
  if (_initRan) {
    if (window.pywebview && !api) {
      api = window.pywebview.api;
      handleLoadResult(await api.auto_load());
      loadUserInfo();
    }
    return;
  }
  _initRan = true;

  api = window.pywebview ? window.pywebview.api : null;
  showScreen('screen-loading');

  document.getElementById('btn-browse-error').addEventListener('click', browseForFile);
  document.getElementById('btn-browse-pick').addEventListener('click',  browseForFile);
  document.getElementById('btn-reload-main').addEventListener('click',  reloadCollections);
  // Back-to-main from spin: LITF has no grid to return to, so go to Steam tab
  document.getElementById('btn-back-to-main').addEventListener('click', () => {
    if (currentPlatform === 'all') switchPlatform('steam');
    else                           showScreen('screen-main');
  });

  // ☰ header menu — force inline styles via setProperty('...', '...', 'important')
  // so nothing in the cascade can override our show/hide.
  const menuBtn  = document.getElementById('btn-menu');
  const menuDrop = document.getElementById('header-menu');

  function openMenu() {
    const rect = menuBtn.getBoundingClientRect();
    menuDrop.style.setProperty('top',      (rect.bottom + 4) + 'px',                'important');
    menuDrop.style.setProperty('right',    (window.innerWidth - rect.right) + 'px', 'important');
    menuDrop.style.setProperty('left',     'auto',                                  'important');
    menuDrop.style.setProperty('display',  'flex',                                  'important');
    menuDrop.style.setProperty('z-index',  '9999',                                  'important');
    menuDrop.classList.remove('hidden');
  }
  function closeMenu() {
    menuDrop.style.setProperty('display', 'none', 'important');
    menuDrop.classList.add('hidden');
  }

  menuBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    e.preventDefault();
    const wasHidden = menuDrop.classList.contains('hidden') || menuDrop.style.display === 'none';
    if (wasHidden) openMenu();
    else           closeMenu();
  });
  document.addEventListener('click', (e) => {
    if (menuDrop.classList.contains('hidden')) return;
    if (menuBtn.contains(e.target) || menuDrop.contains(e.target)) return;
    closeMenu();
  });
  document.getElementById('menu-settings').addEventListener('click', () => {
    closeMenu();
    openSettings();
  });
  document.getElementById('menu-manage-shortcuts').addEventListener('click', () => {
    closeMenu();
    openManageShortcuts();
  });
  document.getElementById('menu-change-file').addEventListener('click', () => {
    closeMenu();
    browseForFile();
  });
  document.getElementById('btn-back-settings').addEventListener('click', () => {
    renderCollections(allCollections, allShortcutAppids, allHiddenCollections);
  });

  // Epic source picker (radios)
  document.querySelectorAll('input[name="epic-source"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
      if (e.target.checked) handleEpicSourceChange(e.target.value);
    });
  });
  // Connect / Disconnect buttons
  document.getElementById('epic-connect-btn').addEventListener('click', openEpicAuthModal);
  document.getElementById('epic-disconnect-btn').addEventListener('click', disconnectEpic);
  // Sound on/off toggle (live — no need to revisit Settings to hear effect)
  document.getElementById('sound-enabled').addEventListener('change', async (e) => {
    if (!api) return;
    soundEnabled = e.target.checked;
    await api.set_sound_enabled(soundEnabled);
  });

  // Cross-platform duplicates: toggle handler
  document.getElementById('dedup-enabled').addEventListener('change', async (e) => {
    if (!api) return;
    const s = await api.get_dedup_settings();
    await api.set_dedup_settings(e.target.checked, s.priority);
    await refreshDedupSettings();
    invalidateDedupCache();
  });

  // Battle.net source picker
  document.querySelectorAll('input[name="battlenet-source"]').forEach(el => {
    el.addEventListener('change', async () => {
      if (!api || !el.checked) return;
      await api.set_battlenet_source(el.value);
      // Invalidate excludes since the source change may add/remove games
      invalidateAllExcludes();
    });
  });
  // EA App source picker
  document.querySelectorAll('input[name="origin-source"]').forEach(el => {
    el.addEventListener('change', async () => {
      if (!api || !el.checked) return;
      await api.set_origin_source(el.value);
      invalidateAllExcludes();
    });
  });
  // Ubisoft Connect source picker
  document.querySelectorAll('input[name="uplay-source"]').forEach(el => {
    el.addEventListener('change', async () => {
      if (!api || !el.checked) return;
      await api.set_uplay_source(el.value);
      invalidateAllExcludes();
    });
  });

  // Edition preference: any of the three radios
  document.querySelectorAll('input[name="edition-pref"]').forEach(el => {
    el.addEventListener('change', async () => {
      if (!api || !el.checked) return;
      await api.set_edition_preference(el.value);
      invalidateEditionCache();
      await refreshEditionPreview();
    });
  });
  // OAuth modal controls
  document.getElementById('epic-auth-close').addEventListener('click', closeEpicAuthModal);
  document.getElementById('epic-auth-open').addEventListener('click', openEpicLoginPage);
  document.getElementById('epic-auth-verify').addEventListener('click', verifyEpicAuthCode);
  document.getElementById('epic-auth-modal').addEventListener('click', (e) => {
    if (e.target.id === 'epic-auth-modal') closeEpicAuthModal();
  });

  // Manage Shortcuts screen
  document.getElementById('btn-back-manage').addEventListener('click', () => {
    renderCollections(allCollections, allShortcutAppids, allHiddenCollections);
  });
  document.getElementById('manage-search').addEventListener('input', (e) => {
    renderManageList(e.target.value);
  });
  // Platform tab switchers
  ['steam', 'gog', 'epic', 'battlenet', 'origin', 'uplay'].forEach(p => {
    document.getElementById(`tab-${p}`).addEventListener('click', () => switchPlatform(p));
  });
  // Leave It To Fate — action button, not a tab destination
  document.getElementById('tab-litf').addEventListener('click', leaveItToFate);

  document.getElementById('btn-coll-roulette').addEventListener('click', openCollectionRoulette);
  document.getElementById('btn-spin').addEventListener('click',          () => doSpin(false));
  document.getElementById('btn-spin-again').addEventListener('click',    spinAgain);
  document.getElementById('btn-back-colls').addEventListener('click', () => {
    if (currentPlatform === 'all') switchPlatform('steam');
    else                           showScreen('screen-main');
  });

  // Debug modal close/copy/save handlers
  const debugModal = document.getElementById('debug-modal');
  document.getElementById('debug-modal-close').addEventListener('click', hideDebugModal);
  document.getElementById('debug-modal-copy').addEventListener('click', copyDebugLog);
  document.getElementById('debug-modal-save').addEventListener('click', saveDebugLog);
  debugModal.addEventListener('click', (e) => { if (e.target === debugModal) hideDebugModal(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !debugModal.classList.contains('hidden')) hideDebugModal();
  });

  // Debug shortcuts:
  //   N             — on spin screen, force the next spin onto a non-Steam shortcut
  //   Ctrl+Shift+D  — dump current collection + all top-level JSON keys
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.shiftKey && (e.key === 'D' || e.key === 'd')) {
      e.preventDefault();
      debugCurrentCollection();
      return;
    }
    if (e.key === 'n' || e.key === 'N') {
      if (!document.getElementById('screen-spin').classList.contains('active')) return;
      if (spinMode !== 'game') return;
      if (document.getElementById('btn-spin').disabled) return;
      e.preventDefault();
      doSpin(true);
    }
  });

  if (api) {
    // Detect which launchers exist on this PC.  Used to (a) show a friendly
    // empty state when none are installed and (b) auto-switch to whatever
    // platform IS available if Steam isn't.
    const detected = await api.detect_platforms();
    if (!detected.any) {
      showNoPlatformsScreen();
      loadUserInfo();
      refreshSoundSettings();
      return;
    }

    const result = await api.auto_load();
    if (result.status !== 'ok' && (detected.gog || detected.epic)) {
      // Steam isn't usable but the user has GOG or Epic — open the first
      // available platform tab instead of dumping them on the Steam error
      // screen.
      switchPlatform(detected.gog ? 'gog' : 'epic');
      loadUserInfo();
      refreshSoundSettings();
      return;
    }
    handleLoadResult(result);
    loadUserInfo();
    refreshSoundSettings();          // pull persisted sound on/off
    refreshLauncherVisibility();     // hide tabs for any disabled launchers
    return;
  }

  // pywebview isn't ready yet.  Wait a bit — pywebviewready will trigger the
  // real load through init's re-entry path.  Only fall back to demo data if
  // we're genuinely running in a plain browser (no pywebview at all).
  setTimeout(() => {
    if (!api) {
      renderCollections([
        { name: 'Horror',       count: 523, appids: [292030,220,377160,413150,271590,105600,400,570,730,440] },
        { name: 'Metroidvania', count: 243, appids: [291550,230190,367520,1145360,444200,753640,548430] },
        { name: 'Roguelike',    count: 174, appids: [1046930,246900,312530,632360,1061910,814010] },
      ]);
    }
  }, 1500);
}

// ── HowLongToBeat ─────────────────────────────────────────────────────────

async function loadHltbData(appid, gameName) {
  const row = document.getElementById('hltb-row');
  if (!api || !row) return;

  // If this spin already kicked off a fetch, await it (likely done or nearly done).
  // Otherwise fall back to fetching now.
  let result;
  if (hltbSpinAppid === appid && hltbSpinPromise) {
    result = await hltbSpinPromise;
    hltbSpinPromise = null;
    hltbSpinAppid   = null;
  } else {
    result = await api.get_hltb_data(String(appid), gameName);
  }

  // Guard: user may have spun again while we were waiting
  if (!result || currentWinnerAppid !== appid) return;
  if (result.status !== 'ok') return;

  const boxes = [
    { label: 'Main Story',    value: result.main_story },
    { label: 'Main + Sides',  value: result.main_extra },
    { label: 'Completionist', value: result.completionist },
  ].filter(b => b.value && b.value > 0);

  if (!boxes.length) return;

  row.innerHTML =
    boxes.map(b => `
      <div class="hltb-box">
        <div class="hltb-label">${esc(b.label)}</div>
        <div class="hltb-value">${formatHltbHours(b.value)}</div>
      </div>`).join('') +
    `<div class="hltb-source" style="width:100%">via HowLongToBeat</div>`;
  row.classList.remove('hidden');
}

function formatHltbHours(hours) {
  if (!hours || hours <= 0) return '—';
  const h = Math.floor(hours);
  const frac = hours - h;
  if (h === 0) return `${Math.round(hours * 60)} Min`;
  if (frac >= 0.4 && frac < 0.6) return `${h}½ Hrs`;
  return `${Math.round(hours)} Hrs`;
}

// ── Utility ───────────────────────────────────────────────────────────────

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatPlaytime(minutes) {
  if (!minutes || minutes < 1) return null;
  if (minutes < 60) return `${minutes} min`;
  const hours = minutes / 60;
  if (hours < 10)  return `${hours.toFixed(1)} hrs`;
  return `${Math.round(hours).toLocaleString()} hrs`;
}

window.addEventListener('pywebviewready', init);
if (!window.pywebview) document.addEventListener('DOMContentLoaded', init);
