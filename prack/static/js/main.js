// prack: the live map. Wires the stream, the store, the map, the card and the URL together.

import { getConfig, getStatus, getTrack, LiveStream } from './api.js';
import { altitudeColor, palette } from './colors.js';
import { declutter } from './declutter.js';
import { fmtAlt, fmtDistance, fmtDuration, fmtHeading, fmtSpeed, fmtVario, labelText, timeFormatter } from './format.js';
import { buildLayers, FONT, LABEL_PX, makeIconAtlas } from './layers.js';
import { MapView } from './map.js';
import { formatHash, navigationKey, parseHash } from './state.js';
import { LiveStore } from './store.js';
import { S } from './strings.js';
import { TrackBuffer } from './track.js';
import { buildTrails } from './trails.js';
import { createUi } from './ui.js';

const LABEL_NAME_ZOOM = 9;
const LABEL_FULL_ZOOM = 11;
const FOCUS_ZOOM = 12;
const TRAIL_PIXELS = 3; // a trail keeps a point about every 3 screen pixels, however far the map is zoomed out

/**
 * Metres between kept trail points at this zoom: about TRAIL_PIXELS screen pixels, rounded to a power of two so the
 * value (and with it the trail data) stays the same while the map is panned and zoomed a little.
 */
function trailSpacing(zoom, lat) {
  const metresPerPixel = (156543.03 * Math.cos((lat * Math.PI) / 180)) / 2 ** zoom;
  return metresPerPixel > 2 ? 2 ** Math.round(Math.log2(metresPerPixel * TRAIL_PIXELS)) : 0;
}

function remember(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* private mode or blocked storage: the setting just does not stick */
  }
}

function recall(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function webglAvailable() {
  try {
    const canvas = document.createElement('canvas');
    return Boolean(canvas.getContext('webgl2') || canvas.getContext('webgl'));
  } catch {
    return false;
  }
}

function fatal(message) {
  const box = document.createElement('div');
  box.className = 'fatal glass';
  box.textContent = message;
  document.body.append(box);
}

async function main() {
  if (!webglAvailable()) return fatal(S.noWebgl);
  let config;
  try {
    config = await getConfig();
  } catch (error) {
    return fatal(`${S.noServer}: ${error.message}`);
  }

  const region = config.regions[0];
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const time = timeFormatter(region.timezone);
  const store = new LiveStore();
  const track = new TrackBuffer();
  const atlas = makeIconAtlas();

  let nav = parseHash(location.hash);
  let theme = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
  const bases = region.basemaps.map((b) => b.id);
  let baseId = bases.includes(recall('prack.base')) ? recall('prack.base') : region.default_basemap;
  let showGround = recall('prack.ground') === '1';
  let selected = nav.sel; // device address
  let lastKnown = null; // the selected aircraft as last seen, for the card after it has left the map
  let follow = false;
  let streamState = 'connecting';
  let serverLink = null;
  let loadToken = 0;
  let announcedMissing = false;
  let pendingFly = Boolean(nav.sel && !nav.ll); // a bookmarked selection without a camera: go and look at it

  const terrainAvailable = Boolean(config.terrain?.enabled);
  if (nav.view === '3d' && !terrainAvailable) nav = { ...nav, view: '2d' };

  // ------------------------------------------------------------------ the map and the controls

  const mapHost = document.getElementById('map');
  const view = new MapView({
    container: mapHost,
    config,
    theme,
    baseId,
    reducedMotion,
    camera: nav.ll && nav.z !== null ? { center: [nav.ll[1], nav.ll[0]], zoom: nav.z } : undefined,
    onClick: (info) => {
      const hit = info?.object && ['markers', 'labels'].includes(info.layer?.id);
      if (hit) select(info.object.address);
      else if (selected) deselect();
    },
  });

  const ui = createUi(document.getElementById('ui'), {
    onView: (v) => setView(v),
    onGround: (on) => {
      showGround = on;
      remember('prack.ground', on ? '1' : '0');
      refreshChips();
      schedule();
    },
    onBase: () => {
      baseId = bases[(bases.indexOf(baseId) + 1) % bases.length];
      remember('prack.base', baseId);
      view.setBase(baseId);
      refreshChips();
    },
    onTheme: () => applyTheme(theme === 'dark' ? 'light' : 'dark'),
    onClose: () => deselect(),
    onFollow: (on) => {
      follow = on;
      const a = store.find(selected);
      if (on && a) view.flyTo(a.lon, a.lat);
      updateCard();
    },
    onFit: () => fitTrack(),
  });

  function applyTheme(next) {
    theme = next;
    document.documentElement.dataset.theme = next;
    remember('prack.theme', next);
    ui.setTheme(next);
    view.setTheme(next);
    schedule();
  }

  function refreshChips() {
    const { flying, ground } = store.counts();
    ui.setCount(flying);
    ui.setGround(showGround, ground);
    ui.setBase(region.basemaps.find((b) => b.id === baseId)?.name ?? baseId);
    ui.setView(view.mode3d ? '3d' : '2d', { terrain: terrainAvailable });
  }

  function refreshLink() {
    if (streamState === 'retrying') return ui.setLink('bad', S.link.offline);
    if (streamState !== 'open') return ui.setLink('warn', S.link.connecting);
    const state = serverLink?.state;
    if (state === 'demo') return ui.setLink('ok', S.link.demo);
    if (state === 'connected') return ui.setLink('ok', S.link.up);
    if (state === 'connecting' || state === 'waiting' || state === undefined) return ui.setLink('warn', S.link.connecting);
    return ui.setLink('bad', S.link.down);
  }

  // ------------------------------------------------------------------ URL

  function writeHash({ push }) {
    const hash = formatHash(nav);
    if (hash === location.hash) return;
    history[push ? 'pushState' : 'replaceState'](null, '', hash);
  }

  /** Change the view state and the address bar; a new history entry when the view (not just the camera) changed. */
  function navigate(patch) {
    const before = navigationKey(nav);
    nav = { ...nav, ...patch };
    writeHash({ push: navigationKey(nav) !== before });
  }

  view.map.on('moveend', () => {
    const c = view.camera();
    nav = { ...nav, ll: [c.center[1], c.center[0]], z: c.zoom };
    writeHash({ push: false });
  });

  function onLocationChange() {
    const next = parseHash(location.hash);
    if (next.view === '3d' && !terrainAvailable) next.view = '2d';
    nav = { ...nav, view: next.view };
    if ((next.view === '3d') !== view.mode3d) setView(next.view, { record: false });
    if (next.sel !== selected) {
      if (next.sel) select(next.sel, { record: false });
      else deselect({ record: false });
    }
  }
  window.addEventListener('popstate', onLocationChange);
  window.addEventListener('hashchange', onLocationChange);

  // ------------------------------------------------------------------ 2D / 3D

  async function setView(next, { record = true } = {}) {
    const want3d = next === '3d' && terrainAvailable;
    if (want3d === view.mode3d) return refreshChips();
    if (record) navigate({ view: want3d ? '3d' : '2d' });
    const done = view.set3d(want3d);
    if (want3d) refreshChips();
    schedule();
    await done;
    refreshChips();
    schedule();
  }

  // ------------------------------------------------------------------ selection and track

  function select(address, { fly = false, record = true } = {}) {
    if (selected === address && ui.cardVisible()) return;
    selected = address;
    follow = false;
    announcedMissing = false;
    lastKnown = null;
    track.reset(null);
    loadToken += 1;
    if (record) navigate({ sel: address });
    else nav = { ...nav, sel: address }; // the address bar already says so (back button)
    const a = store.find(address);
    if (a && fly) view.flyTo(a.lon, a.lat, { zoom: Math.max(view.zoom, FOCUS_ZOOM) });
    syncTrack();
    updateCard();
    schedule();
  }

  function deselect({ record = true } = {}) {
    selected = null;
    follow = false;
    lastKnown = null;
    track.reset(null);
    loadToken += 1;
    if (record) navigate({ sel: null });
    else nav = { ...nav, sel: null };
    ui.hideCard();
    schedule();
  }

  async function loadTrack(flightId) {
    const token = ++loadToken;
    try {
      const columns = await getTrack(flightId);
      if (token !== loadToken || track.flightId !== flightId) return;
      track.setColumns(columns);
    } catch {
      if (token !== loadToken) return;
      track.setColumns({ t: [] }); // live points may start the track instead
      ui.toast(S.toast.trackFailed);
    }
    updateCard();
    schedule();
  }

  /** After every message: follow the selected aircraft into a new flight, and add its new points to the track. */
  function syncTrack() {
    const a = selected ? store.find(selected) : null;
    if (!a) return;
    if (a.flightId === null) return; // landed or not yet flying: the flight just flown stays on the map
    if (a.flightId !== track.flightId) {
      track.reset(a.flightId);
      loadTrack(a.flightId);
    } else {
      track.addPoints(a.fresh);
    }
  }

  function fitTrack() {
    if (track.bounds) view.fitBounds(track.bounds);
  }

  function cardModel() {
    const a = store.find(selected) ?? lastKnown;
    if (!a) return null;
    const live = Boolean(store.find(selected));
    const flying = a.flying && live;
    const who = [a.pilot, a.cn && a.reg ? `${a.cn} · ${a.reg}` : (a.reg ?? a.cn)].filter((x) => x && x !== a.name).join(' · ');
    const duration = a.takeoff ? Math.max(0, (live ? store.now : a.t) - a.takeoff) : null;
    let note = '';
    if (!live) note = S.card.lastHeard(fmtDuration(store.now - a.t));
    else if (!a.flying) note = S.card.onGround;
    else if (a.flightId !== null && !track.loaded) note = S.card.trackLoading;
    return {
      name: a.name,
      source: S.source[a.src] ?? a.src,
      who,
      flying,
      live,
      follow,
      canFit: Boolean(track.bounds),
      vsSign: a.vs > 0.05 ? 'up' : a.vs < -0.05 ? 'down' : '',
      note,
      values: {
        alt: fmtAlt(a.alt),
        agl: a.agl === null ? '–' : fmtAlt(a.agl),
        spd: fmtSpeed(a.spd),
        vs: fmtVario(a.vs),
        hdg: fmtHeading(a.hdg),
        takeoff: time(a.takeoff),
        duration: fmtDuration(duration),
        distance: track.loaded ? fmtDistance(track.distance) : '–',
      },
    };
  }

  /** The card is shown whenever something is selected and known: not before the first snapshot has told us about it. */
  function updateCard() {
    const model = selected ? cardModel() : null;
    if (model) {
      ui.updateCard(model);
      ui.showCard();
    } else {
      ui.hideCard();
    }
  }

  // ------------------------------------------------------------------ drawing

  const measureContext = document.createElement('canvas').getContext('2d');
  const widths = new Map();
  function textWidth(text) {
    let w = widths.get(text);
    if (w === undefined) {
      measureContext.font = `600 ${LABEL_PX}px ${FONT}`;
      w = Math.ceil(measureContext.measureText(text).width) + 14;
      if (widths.size > 4000) widths.clear();
      widths.set(text, w);
    }
    return w;
  }

  function planLabels(aircraft) {
    const zoom = view.zoom;
    const [width, height] = view.size();
    const items = [];
    const texts = new Map();
    for (const a of aircraft) {
      const isSelected = a.address === selected;
      const full = isSelected || zoom >= LABEL_FULL_ZOOM;
      const named = full || zoom >= LABEL_NAME_ZOOM;
      const variants = [];
      if (full) variants.push({ id: 'full', width: textWidth(labelText(a, 'full')) });
      if (named) variants.push({ id: 'name', width: textWidth(labelText(a, 'name')) });
      if (!variants.length) continue;
      const [x, y] = view.project(a.lon, a.lat, view.mode3d ? a.alt : 0);
      items.push({ key: a.id, x, y, height: LABEL_PX + 8, priority: isSelected ? 1e9 : (a.flying ? 1e5 : 0) + a.alt, variants });
      texts.set(a.id, a);
    }
    const placed = declutter(items, { width, height });
    const labels = new Map();
    for (const [id, { side, variant }] of placed) labels.set(id, { side, text: labelText(texts.get(id), variant) });
    return labels;
  }

  const memo = new Map();
  /** The same object while nothing it depends on has changed, so deck.gl does not rebuild its buffers. */
  function memoised(name, key, build) {
    const hit = memo.get(name);
    if (hit && hit.key === key) return hit.value;
    const value = build();
    memo.set(name, { key, value });
    return value;
  }

  let frame = 0;
  let layerVersion = 0;
  let lastLayers = [];
  function schedule() {
    if (!frame) frame = requestAnimationFrame(draw);
  }

  function draw() {
    frame = 0;
    const aircraft = store.list({ ground: showGround });
    const colors = palette(theme);
    layerVersion += 1;
    const zoom = view.zoom;
    const spacing = trailSpacing(zoom, view.centerLat());
    const skip = track.length > 1 ? selected : null;
    const trails = memoised('trails', `${store.version}|${view.mode3d}|${spacing}|${theme}|${showGround}|${skip}`, () =>
      buildTrails(aircraft, {
        mode3d: view.mode3d,
        now: store.now,
        seconds: store.trailSeconds,
        spacing,
        rgb: colors.accent,
        skip,
      }),
    );
    const selectedTrack =
      selected && track.length > 1
        ? memoised('track', `${track.flightId}|${track.version}|${view.mode3d}`, () => ({
            path: track.path({ mode3d: view.mode3d, color: altitudeColor }),
            curtain: view.mode3d ? track.curtain({ groundAt: (lon, lat) => view.groundAt(lon, lat) }) : null,
          }))
        : null;
    lastLayers = buildLayers({
      atlas,
      palette: colors,
      mode3d: view.mode3d,
      bearing: view.bearing,
      selected,
      aircraft,
      labels: planLabels(aircraft),
      trails,
      track: selectedTrack,
      version: `${store.version}|${theme}`,
      labelVersion: layerVersion,
    });
    view.setLayers(lastLayers);
  }

  // ------------------------------------------------------------------ the live stream

  function onMessage(message) {
    store.apply(message);
    if (selected) {
      const a = store.find(selected);
      if (a) {
        lastKnown = { ...a };
        syncTrack();
        if (pendingFly) {
          pendingFly = false;
          view.flyTo(a.lon, a.lat, { zoom: FOCUS_ZOOM });
        } else if (follow) view.follow(a.lon, a.lat);
      } else if (store.hasSnapshot && !announcedMissing && !lastKnown) {
        announcedMissing = true; // asked for by the URL, but not in the air
        ui.toast(S.toast.notFound);
        deselect({ record: false });
        nav = { ...nav, sel: null };
        writeHash({ push: false });
      }
    }
    refreshChips();
    updateCard();
    schedule();
  }

  const stream = new LiveStream({
    onMessage,
    onState: (state) => {
      streamState = state;
      refreshLink();
    },
  });

  async function pollStatus() {
    try {
      serverLink = (await getStatus()).link;
    } catch {
      /* the stream state already shows a broken connection */
    }
    refreshLink();
  }

  // ------------------------------------------------------------------ start

  ui.setTheme(theme);
  refreshChips();
  refreshLink();
  await view.ready;
  view.map.on('zoom', schedule);
  view.map.on('rotate', schedule);
  view.map.on('move', schedule);
  view.map.on('dragstart', (event) => {
    if (follow && event.originalEvent) {
      follow = false;
      updateCard();
    }
  });
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && selected) deselect();
  });
  document.addEventListener('visibilitychange', schedule);

  if (nav.view === '3d') await setView('3d', { record: false });
  stream.start();
  pollStatus();
  setInterval(pollStatus, 10000);
  schedule();

  // What the browser tests (and anyone with the console open) can look at; nothing in the app reads it.
  window.prack = {
    store, track, view, select, deselect, setView, draw, version: config.version,
    layer: (id) => lastLayers.find((l) => l.id === id) ?? null,
  }; // prettier-ignore
}

main().catch((error) => {
  console.error(error);
  fatal(String(error?.message ?? error));
});
