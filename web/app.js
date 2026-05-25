/* Steam Roulette — Phase 4 */

let api = null;
let currentCollection   = null;  // active collection for game spin
let allCollections      = [];    // all real collections (for Collection Roulette)
let allShortcutAppids   = [];    // every non-Steam shortcut appid (from shortcuts.vdf)
let allHiddenCollections = [];   // names the user has hidden (incl. synthetic cards)
let spinMode            = 'game'; // 'game' | 'collection'
let currentWinnerAppid  = null;  // appid currently shown in footer-winner (guards stale callbacks)
let hltbSpinPromise     = null;  // Promise<hltb result> — started during spin, consumed by loadHltbData
let hltbSpinAppid       = null;  // which appid hltbSpinPromise is for
let prevGameWinner    = null;   // last winning appid — used to start Spin Again reel
let prevCollWinner    = null;   // last winning collection
let pendingStartFrom  = null;   // startFrom value queued by spinAgain for doSpin

// ── Screen routing ────────────────────────────────────────────────────────

function showScreen(id) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

// ── Collection grid ───────────────────────────────────────────────────────

function renderCollections(collections, shortcutAppids, hiddenList) {
  const grid  = document.getElementById('collection-grid');
  const empty = document.getElementById('empty-state');
  grid.innerHTML = '';

  allCollections       = collections    || [];
  allShortcutAppids    = shortcutAppids || allShortcutAppids;
  if (hiddenList)
    allHiddenCollections = hiddenList;
  const hiddenSet = new Set(allHiddenCollections);

  // Whole Library = union of every appid across visible collections + shortcuts
  const allAppIds = [...new Set([
    ...allCollections.flatMap(c => c.appids),
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

  if (!allCollections.length) {
    if (allAppIds.length === 0) loadInstalledLibrary(grid, empty);
    else { empty.classList.remove('hidden'); showScreen('screen-main'); }
    return;
  }

  empty.classList.add('hidden');
  allCollections.forEach(c => grid.appendChild(makeCollCard(c, null)));
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

function playTick(speedFraction) {
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
  if (api && spinMode === 'game' && !isLikelyNonSteam(winner)) {
    api.get_game_art(String(winner)).then(result => {
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
  hltbSpinAppid    = winner;
  hltbSpinPromise  = null;
  if (api && spinMode === 'game' && !isLikelyNonSteam(winner)) {
    hltbSpinPromise = (async () => {
      const nameResult = await api.get_game_name(String(winner));
      if (myToken !== currentSpinToken) return null;
      if (nameResult.status !== 'ok') return null;
      return api.get_hltb_data(String(winner), nameResult.name);
    })();
  }

  if (spinMode === 'collection') {
    buildCollectionReel(allCollections, startFrom, winner);
  } else {
    buildGameReel(currentCollection.appids, startFrom, winner);
  }

  await runAnimation();

  const winnerCard = document.querySelector('#reel .reel-winner');
  if (winnerCard) winnerCard.classList.add('winner-pulse');
  playLanding();
  await delay(650);

  // Show winner info below the reel — no animation, no stage swap
  if (spinMode === 'collection') showCollectionWinner(winner);
  else                           showGameWinner(winner);

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
  if (spinMode === 'collection') buildCollectionReel(allCollections, startFrom);
  else                           buildGameReel(currentCollection.appids, startFrom);

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

async function openSettings() {
  if (!api) { showToast('Backend not available', 'error'); return; }
  const r = await api.get_settings();
  if (r.status !== 'ok') {
    showToast(`Failed: ${r.message || r.status}`, 'error');
    return;
  }
  renderSettings(r);
  showScreen('screen-settings');
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

  if (!data.excluded_games.length) {
    excludedList.innerHTML = `<div class="settings-empty">No games excluded. Use "Don't show again" on the winner panel to exclude one.</div>`;
  } else {
    data.excluded_games.forEach(g => {
      const row = document.createElement('div');
      row.className = 'settings-row';
      row.innerHTML = `
        <div><span class="settings-row-label">${esc(g.name)}</span><span class="settings-row-count">appid ${g.appid}</span></div>
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
  }
}

async function loadUserInfo() {
  if (!api) return;
  try {
    const r = await api.get_user_info();
    if (r.status !== 'ok') return;
    const badge = document.getElementById('user-badge');
    const av    = document.getElementById('user-avatar');
    const name  = document.getElementById('user-name');
    name.textContent = r.persona_name || 'Steam User';
    if (r.avatar) {
      av.src = r.avatar;
      av.style.display = '';
    } else {
      av.style.display = 'none';
    }
    badge.classList.remove('hidden');
  } catch (_) { /* silently ignore */ }
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
  const btn = document.getElementById('btn-reload-main');
  btn.classList.add('spinning');
  btn.disabled = true;

  // Even if Python returns in 5ms, we want the user to actually SEE the spin.
  const [result] = await Promise.all([
    api.reload_collections(),
    delay(800),
  ]);

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
  document.getElementById('btn-back-to-main').addEventListener('click', () => showScreen('screen-main'));

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

  // Manage Shortcuts screen
  document.getElementById('btn-back-manage').addEventListener('click', () => {
    renderCollections(allCollections, allShortcutAppids, allHiddenCollections);
  });
  document.getElementById('manage-search').addEventListener('input', (e) => {
    renderManageList(e.target.value);
  });
  document.getElementById('btn-coll-roulette').addEventListener('click', openCollectionRoulette);
  document.getElementById('btn-spin').addEventListener('click',          () => doSpin(false));
  document.getElementById('btn-spin-again').addEventListener('click',    spinAgain);
  document.getElementById('btn-back-colls').addEventListener('click',    () => showScreen('screen-main'));

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
    handleLoadResult(await api.auto_load());
    loadUserInfo();
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
