// prack: the map. Wires the live stream, the day view, the map, the card and the URL together.
// Two modes share one map: Live (aircraft in the air, selected by device address) and History (the flights of one
// day, selected by flight id).

import { getConfig, getDayFlights, getDays, getStatus, getTrack, LiveStream } from './api.js';
import { createCalendar } from './calendar.js';
import { altitudeColor, palette } from './colors.js';
import { DayView } from './dayview.js';
import { declutter } from './declutter.js';
import { addDays, compareDays, formatDay, todayIn } from './dates.js';
import { fmtAlt, fmtDistance, fmtDuration, fmtHeading, fmtSpeed, fmtVario, labelText, timeFormatter } from './format.js';
import { buildDayLayers, buildLayers, FONT, LABEL_PX, makeIconAtlas } from './layers.js';
import { MapView } from './map.js';
import { buildHighlight, buildPreviews, unionBounds } from './previews.js';
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
const TODAY_REFRESH_MS = 60_000; // the flights of today are loaded again this often
const START_COLOR = [61, 220, 132, 255];
const END_COLOR = [255, 90, 95, 255];

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
  const utcClock = timeFormatter('UTC');
  const today = () => todayIn(region.timezone);
  const store = new LiveStore();
  const track = new TrackBuffer();
  const atlas = makeIconAtlas();

  let nav = parseHash(location.hash);
  let mode = nav.route === 'day' ? 'history' : 'live';
  let day = nav.day; // the day shown in History
  let lastDay = nav.day; // where History comes back to
  let theme = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
  const bases = region.basemaps.map((b) => b.id);
  let baseId = bases.includes(recall('prack.base')) ? recall('prack.base') : region.default_basemap;
  let showGround = recall('prack.ground') === '1';
  let selected = mode === 'live' ? nav.sel : null; // live: device address; history: flight id (as text)
  let lastKnown = null; // the selected aircraft as last seen, for the card after it has left the map
  let follow = false;
  let streamState = 'connecting';
  let serverLink = null;
  let loadToken = 0;
  let dayToken = 0;
  let hovered = -1; // History: index of the flight under the pointer
  let lastPreviews = null;
  let announcedMissing = false;
  let pendingFly = Boolean(mode === 'live' && nav.sel && !nav.ll); // a bookmarked selection without a camera: look at it

  const terrainAvailable = Boolean(config.terrain?.enabled);
  if (nav.view === '3d' && !terrainAvailable) nav = { ...nav, view: '2d' };

  // ------------------------------------------------------------------ the day's flights

  const dayView = new DayView({
    fetchDay: (d) => getDayFlights(d),
    fetchTrack: (id) => getTrack(id),
    onChange: () => {
      if (mode !== 'history') return;
      refreshFlights();
      updateCard();
      schedule();
    },
  });

  /** The clock time of a stored flight's take-off, in the local time of the place it took off. */
  const flightClock = (f, epoch = f.takeoff?.t ?? f.start) => (epoch == null ? '–' : utcClock(epoch + (f.utc_offset_s ?? 0)));

  // ------------------------------------------------------------------ the map and the controls

  const view = new MapView({
    container: document.getElementById('map'),
    config,
    theme,
    baseId,
    reducedMotion,
    camera: nav.ll && nav.z !== null ? { center: [nav.ll[1], nav.ll[0]], zoom: nav.z } : undefined,
    onClick: (info) => {
      if (mode === 'history') {
        const flight = info?.picked && info.layer?.id === 'previews' ? dayView.flights[lastPreviews?.owners[info.index]] : null;
        if (flight) selectFlight(flight.id);
        else if (selected) deselect();
        return;
      }
      const hit = info?.object && ['markers', 'labels'].includes(info.layer?.id);
      if (hit) select(info.object.address);
      else if (selected) deselect();
    },
    onHover: (info) => {
      if (mode !== 'history') return;
      const flight = info?.picked && info.layer?.id === 'previews' ? dayView.flights[lastPreviews?.owners[info.index]] : null;
      const index = flight ? dayView.flights.indexOf(flight) : -1;
      const named = flight && String(flight.id) !== selected; // the selected one is on the card already
      ui.hint(named ? `${flight.label} · ${flightClock(flight)}` : null, info?.x, info?.y);
      if (index !== hovered) {
        hovered = index;
        schedule();
      }
    },
  });

  const ui = createUi(document.getElementById('ui'), {
    onMode: (m) => (m === 'history' ? showDay(lastDay ?? today()) : showLive()),
    onDay: (step) => showDay(step === 'today' ? today() : addDays(day, step === 'prev' ? -1 : 1)),
    onPickFlight: (id) => selectFlight(id, { fit: true }),
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

  const calendar = createCalendar({
    host: ui.root,
    anchor: ui.calendarButton,
    loadDays: (first, last) => getDays(first, last),
    onPick: (d) => showDay(d),
    today,
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
    ui.setGround(showGround, ground);
    ui.setBase(region.basemaps.find((b) => b.id === baseId)?.name ?? baseId);
    ui.setView(view.mode3d ? '3d' : '2d', { terrain: terrainAvailable });
    if (mode === 'live') ui.setCount(flying === 0 ? S.empty : S.count(flying));
  }

  /** The strip of the day's flights and the line that counts them. */
  function refreshFlights() {
    if (mode !== 'history') return ui.setFlights(null);
    const flights = dayView.flights;
    ui.setFlights(flights.map((f) => ({ id: f.id, name: f.label, time: flightClock(f) })), selected);
    if (dayView.loading && !flights.length) ui.setCount(S.flights.loading);
    else if (dayView.failed) ui.setCount(S.flights.failed);
    else ui.setCount(flights.length ? S.flights.count(flights.length) : S.flights.none);
  }

  function updateDayControls() {
    const d = mode === 'history' ? day : today();
    ui.setDay({ text: formatDay(d), isToday: d === today(), canNext: compareDays(d, today()) < 0 });
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

  /** Record a state the address bar already shows (back button), or write it. */
  function record(patch, push) {
    if (push) navigate(patch);
    else nav = { ...nav, ...patch };
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
    if (next.route === 'day') {
      if (mode !== 'history' || day !== next.day) showDay(next.day, { record: false, fit: false, sel: next.sel });
      else if (next.sel !== selected) {
        if (next.sel && dayView.get(next.sel)) selectFlight(next.sel, { record: false });
        else deselect({ record: false });
      }
    } else if (mode !== 'live') {
      showLive({ record: false });
    } else if (next.sel !== selected) {
      if (next.sel) select(next.sel, { record: false });
      else deselect({ record: false });
    }
  }
  window.addEventListener('popstate', onLocationChange);
  window.addEventListener('hashchange', onLocationChange);

  // ------------------------------------------------------------------ 2D / 3D

  async function setView(next, { record: write = true } = {}) {
    const want3d = next === '3d' && terrainAvailable;
    if (want3d === view.mode3d) return refreshChips();
    if (write) navigate({ view: want3d ? '3d' : '2d' });
    const done = view.set3d(want3d);
    if (want3d) refreshChips();
    schedule();
    await done;
    refreshChips();
    schedule();
  }

  // ------------------------------------------------------------------ Live / History

  function clearSelection() {
    selected = null;
    follow = false;
    lastKnown = null;
    hovered = -1;
    track.reset(null);
    loadToken += 1;
    ui.hideCard();
    ui.hint(null);
  }

  function showLive({ record: write = true } = {}) {
    if (mode === 'live') return;
    dayToken += 1;
    clearSelection();
    dayView.clear();
    mode = 'live';
    record({ route: 'live', day: null, sel: null }, write);
    ui.setMode('live');
    updateDayControls();
    refreshFlights();
    refreshChips();
    schedule();
  }

  /**
   * Show the flights of a day. `silent`: a refresh of the day on screen (nothing else changes); `fit`: move the camera
   * over the flights; `sel`: a flight to select once the day is loaded (from the address bar).
   */
  async function showDay(d, { record: write = true, fit = true, silent = false, sel = null } = {}) {
    if (!silent) {
      clearSelection();
      mode = 'history';
      day = d;
      lastDay = d;
      record({ route: 'day', day: d, sel: null }, write);
      ui.setMode('history');
      calendar.setSelected(d);
      updateDayControls();
      refreshChips();
      refreshFlights();
      schedule();
    }
    const token = ++dayToken;
    try {
      await dayView.load(d, { keep: silent });
    } catch {
      if (token === dayToken && !silent) ui.toast(S.toast.dayFailed);
      refreshFlights();
      return;
    }
    if (token !== dayToken || mode !== 'history') return;
    refreshFlights();
    if (!silent) {
      const flight = sel ? dayView.get(sel) : null;
      if (sel && !flight) {
        ui.toast(S.toast.flightNotFound);
        record({ sel: null }, false);
        writeHash({ push: false });
      }
      const box = unionBounds(dayView.flights);
      if (fit && !flight && box) view.fitBounds(box, { padding: fitPadding() });
      if (flight) selectFlight(flight.id, { record: false, fit: !nav.ll });
    }
    updateCard();
    schedule();
  }

  setInterval(() => {
    if (mode === 'history' && day === today()) showDay(day, { silent: true });
  }, TODAY_REFRESH_MS);

  // ------------------------------------------------------------------ selection and track

  function select(address, { fly = false, record: write = true } = {}) {
    if (selected === address && ui.cardVisible()) return;
    selected = address;
    follow = false;
    announcedMissing = false;
    lastKnown = null;
    track.reset(null);
    loadToken += 1;
    record({ sel: address }, write); // when not writing, the address bar already says so (back button)
    const a = store.find(address);
    if (a && fly) view.flyTo(a.lon, a.lat, { zoom: Math.max(view.zoom, FOCUS_ZOOM) });
    syncTrack();
    updateCard();
    schedule();
  }

  function selectFlight(id, { record: write = true, fit = false } = {}) {
    const flight = dayView.get(id);
    if (!flight) return;
    selected = String(flight.id);
    follow = false;
    hovered = -1;
    ui.hint(null);
    track.reset(flight.id);
    record({ sel: selected }, write);
    loadTrack(flight.id, { fit });
    ui.scrollToFlight(flight.id);
    refreshFlights();
    updateCard();
    schedule();
  }

  function deselect({ record: write = true } = {}) {
    clearSelection();
    record({ sel: null }, write);
    refreshFlights();
    schedule();
  }

  async function loadTrack(flightId, { fit = false } = {}) {
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
    if (fit) fitTrack();
    updateCard();
    schedule();
  }

  /** After every message (Live): follow the selected aircraft into a new flight, and add its new points to the track. */
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

  /** Room to leave around a fitted box so that it ends up in the free part of the screen, not under the controls. */
  function fitPadding() {
    const rect = (selector) => document.querySelector(selector)?.getBoundingClientRect();
    const bar = rect('.bar');
    const dock = document.querySelector('.dock');
    const card = document.querySelector('.card');
    const padding = { top: (bar?.bottom ?? 0) + 16, bottom: 28, left: 28, right: 76 };
    if (card && !card.hidden) {
      const r = card.getBoundingClientRect();
      if (r.width > window.innerWidth * 0.8) padding.bottom = window.innerHeight - r.top + 16; // a bottom sheet
      else padding.left = r.right + 16;
    } else if (dock && getComputedStyle(dock).display !== 'none' && !document.querySelector('.flights')?.hidden) {
      padding.bottom = window.innerHeight - dock.getBoundingClientRect().top + 16;
    }
    // never more than the map can give
    padding.top = Math.min(padding.top, window.innerHeight * 0.4);
    padding.bottom = Math.min(padding.bottom, window.innerHeight * 0.4);
    padding.left = Math.min(padding.left, window.innerWidth * 0.4);
    return padding;
  }

  function fitTrack() {
    const box = track.bounds ?? (mode === 'history' ? dayView.get(selected)?.bbox : null);
    if (box) view.fitBounds(box, { padding: fitPadding() });
  }

  function liveCardModel() {
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
    const sign = a.vs > 0.05 ? 'up' : a.vs < -0.05 ? 'down' : '';
    const grids = [[
      { key: 'alt', label: S.card.altitude, value: fmtAlt(a.alt) },
      { key: 'agl', label: S.card.agl, value: a.agl === null ? '–' : fmtAlt(a.agl) },
    ]]; // prettier-ignore
    if (flying) {
      grids[0].push(
        { key: 'spd', label: S.card.speed, value: fmtSpeed(a.spd) },
        { key: 'vs', label: S.card.vario, value: fmtVario(a.vs), sign },
        { key: 'hdg', label: S.card.heading, value: fmtHeading(a.hdg) },
      );
      grids.push([
        { key: 'takeoff', label: S.card.takeoff, value: time(a.takeoff) },
        { key: 'duration', label: S.card.duration, value: fmtDuration(duration) },
        { key: 'distance', label: S.card.distance, value: track.loaded ? fmtDistance(track.distance) : '–' },
      ]);
    }
    return {
      name: a.name,
      source: S.source[a.src] ?? a.src,
      who,
      note,
      grids,
      follow: { show: true, pressed: follow, enabled: live },
      canFit: Boolean(track.bounds),
    };
  }

  function flightCardModel() {
    const f = dayView.get(selected);
    if (!f) return null;
    const st = f.stats;
    const who = [f.pilot, f.cn && f.reg ? `${f.cn} · ${f.reg}` : (f.reg ?? f.cn), f.model].filter((x) => x && x !== f.label).join(' · ');
    const takeoffAt = f.takeoff?.t ?? f.start;
    const duration = f.live ? Math.max(0, (store.now || Date.now() / 1000) - takeoffAt) : (st.airtime_s ?? st.duration_s);
    const distance = f.live && track.loaded ? track.distance : (st.distance_km ?? 0) * 1000;
    let note = '';
    if (f.live) note = S.card.stillFlying;
    else if (f.landing?.t) note = `${S.card.landing} ${flightClock(f, f.landing.t)}`;
    if (!track.loaded) note = S.card.trackLoading;
    return {
      name: f.label,
      source: S.source[f.source] ?? f.source,
      who,
      note,
      grids: [
        [
          { key: 'maxalt', label: S.card.maxAlt, value: fmtAlt(st.max_alt) },
          { key: 'gain', label: S.card.gain, value: fmtAlt(st.alt_gain) },
          { key: 'climb', label: S.card.bestClimb, value: `${fmtVario(st.max_climb)} ${S.units.ms}` },
        ],
        [
          { key: 'takeoff', label: S.card.takeoff, value: flightClock(f, takeoffAt) },
          { key: 'duration', label: S.card.duration, value: fmtDuration(duration) },
          { key: 'distance', label: S.card.distance, value: fmtDistance(distance) },
        ],
      ],
      follow: { show: false, pressed: false, enabled: false },
      canFit: Boolean(track.bounds || f.bbox),
    };
  }

  /** The card is shown whenever something is selected and known: not before the first snapshot has told us about it. */
  function updateCard() {
    const model = selected ? (mode === 'history' ? flightCardModel() : liveCardModel()) : null;
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

  /** The selected flight's full track, in the current dimension; null while it is not loaded. */
  function selectedTrack() {
    if (!selected || track.length < 2) return null;
    return memoised('track', `${track.flightId}|${track.version}|${view.mode3d}`, () => ({
      path: track.path({ mode3d: view.mode3d, color: altitudeColor }),
      curtain: view.mode3d ? track.curtain({ groundAt: (lon, lat) => view.groundAt(lon, lat) }) : null,
    }));
  }

  function drawDay(colors) {
    const mode3d = view.mode3d;
    const full = selectedTrack();
    const flights = dayView.flights;
    lastPreviews = memoised('previews', `${dayView.version}|${mode3d}|${full ? selected : ''}|${selected !== null}`, () =>
      buildPreviews(flights, { mode3d, selectedId: full ? selected : null, dim: selected !== null }),
    );
    // The flight to draw bold: the one under the pointer, or the selected one while its track is still on its way.
    const bold = hovered >= 0 ? flights[hovered] : selected && !full ? dayView.get(selected) : null;
    const highlight = bold ? memoised('highlight', `${bold.id}|${dayView.version}|${mode3d}`, () => buildHighlight(bold, { mode3d })) : null;
    const f = dayView.get(selected);
    let ends = null;
    if (full && f) {
      const z = (i) => (mode3d ? track.alt[i] : 0);
      const last = track.length - 1;
      ends = [{ pos: [track.lon[0], track.lat[0], z(0)], color: START_COLOR }];
      if (!f.live) ends.push({ pos: [track.lon[last], track.lat[last], z(last)], color: END_COLOR });
    }
    lastLayers = buildDayLayers({ palette: colors, mode3d, previews: lastPreviews, highlight, track: full, ends });
    view.setLayers(lastLayers);
  }

  function draw() {
    frame = 0;
    const colors = palette(theme);
    layerVersion += 1;
    if (mode === 'history') return drawDay(colors);
    const aircraft = store.list({ ground: showGround });
    const zoom = view.zoom;
    const spacing = trailSpacing(zoom, view.centerLat());
    const full = selectedTrack();
    const skip = full ? selected : null;
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
    lastLayers = buildLayers({
      atlas,
      palette: colors,
      mode3d: view.mode3d,
      bearing: view.bearing,
      selected,
      aircraft,
      labels: planLabels(aircraft),
      trails,
      track: full,
      version: `${store.version}|${theme}`,
      labelVersion: layerVersion,
    });
    view.setLayers(lastLayers);
  }

  // ------------------------------------------------------------------ the live stream

  function onMessage(message) {
    store.apply(message);
    if (mode === 'history') {
      const f = selected ? dayView.get(selected) : null;
      const a = f?.live ? store.findByFlight(f.id) : null;
      if (a && track.flightId === f.id) track.addPoints(a.fresh); // a flight still in the air keeps growing
      updateCard();
      schedule();
      return;
    }
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
  ui.setMode(mode);
  updateDayControls();
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
  if (mode === 'history') showDay(nav.day, { record: false, fit: !nav.ll, sel: nav.sel });
  schedule();

  // What the browser tests (and anyone with the console open) can look at; nothing in the app reads it.
  window.prack = {
    store, track, view, dayView, calendar, select, selectFlight, deselect, setView, showDay, showLive, draw,
    version: config.version,
    mode: () => mode,
    layer: (id) => lastLayers.find((l) => l.id === id) ?? null,
  }; // prettier-ignore
}

main().catch((error) => {
  console.error(error);
  fatal(String(error?.message ?? error));
});
