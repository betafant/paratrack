// The live picture: every aircraft the server has told us about, with its recent trail.
// Pure data logic (no DOM, no map), so it runs under `node --test` too.

export const TRAIL_SECONDS = 360;
const NO_POINTS = Object.freeze([]);

export class LiveStore {
  constructor({ trailSeconds = TRAIL_SECONDS } = {}) {
    this.trailSeconds = trailSeconds;
    this.aircraft = new Map(); // id -> entry
    this.byAddress = new Map(); // device address -> id
    this.now = 0; // the server's clock at the last message, epoch seconds
    this.seq = 0;
    this.version = 0; // moves with every message; layers are rebuilt when it does
    this.hasSnapshot = false;
  }

  /** Apply one message from /api/live/stream (or a snapshot from /api/live). */
  apply(message) {
    this.now = message.now ?? this.now;
    this.seq = message.seq ?? this.seq;
    for (const entry of this.aircraft.values()) entry.fresh = NO_POINTS;

    const seen = new Set();
    for (const a of message.aircraft ?? []) seen.add(this._upsert(a).id);
    // An aircraft that left and came back inside one window is in both lists: the newer news wins. One that changed
    // protocol (FNT -> FLR) has already moved to its new id, so the old id is simply gone.
    for (const id of message.removed ?? []) if (!seen.has(id)) this._delete(id);
    if (message.full) {
      for (const id of [...this.aircraft.keys()]) if (!seen.has(id)) this._delete(id);
      this.hasSnapshot = true;
    }
    this._prune();
    this.version += 1;
  }

  get size() {
    return this.aircraft.size;
  }

  get(id) {
    return this.aircraft.get(id);
  }

  /** The aircraft behind a device address, whatever protocol it is heard on right now. */
  find(address) {
    const id = this.byAddress.get(address);
    return id === undefined ? undefined : this.aircraft.get(id);
  }

  /** The aircraft that is flying flight `flightId` (ids compare as text), if it is in the air. */
  findByFlight(flightId) {
    for (const entry of this.aircraft.values()) {
      if (entry.flightId !== null && String(entry.flightId) === String(flightId)) return entry;
    }
    return undefined;
  }

  list({ ground = true } = {}) {
    const all = [...this.aircraft.values()];
    return ground ? all : all.filter((a) => a.flying);
  }

  counts() {
    let flying = 0;
    for (const a of this.aircraft.values()) if (a.flying) flying += 1;
    return { flying, ground: this.aircraft.size - flying };
  }

  _upsert(a) {
    let entry = this.aircraft.get(a.id);
    if (!entry) {
      const oldId = this.byAddress.get(a.address);
      const old = oldId === undefined ? undefined : this.aircraft.get(oldId);
      if (old) {
        // The same device now arrives under another protocol (FNT -> FLR): one aircraft, one trail.
        this.aircraft.delete(oldId);
        entry = old;
        entry.id = a.id;
        this.aircraft.set(a.id, entry);
      } else {
        entry = { id: a.id, trail: [], fresh: NO_POINTS };
        this.aircraft.set(a.id, entry);
      }
    }
    this.byAddress.set(a.address, a.id);

    entry.address = a.address;
    entry.name = a.name;
    entry.pilot = a.pilot ?? null;
    entry.reg = a.reg ?? null;
    entry.cn = a.cn ?? null;
    entry.src = a.src;
    entry.flightId = a.flight_id ?? null;
    entry.flying = Boolean(a.flying);
    entry.takeoff = a.takeoff ?? null;
    entry.t = a.t;
    entry.lat = a.lat;
    entry.lon = a.lon;
    entry.alt = a.alt;
    entry.gnd = a.gnd ?? null;
    entry.spd = a.spd ?? null;
    entry.vs = a.vs ?? null;
    entry.hdg = a.hdg ?? null;
    entry.agl = entry.gnd === null ? null : entry.alt - entry.gnd;

    if (a.pts?.length) {
      entry.fresh = a.pts;
      for (const p of a.pts) appendPoint(entry.trail, p[0], p[1], p[2], p[3]);
    }
    if (a.trail?.length) entry.trail = mergeTrail(entry.trail, a.trail);
    if (!entry.trail.length || entry.trail[entry.trail.length - 1][0] < a.t) {
      appendPoint(entry.trail, a.t, a.lon, a.lat, a.alt); // the newest position is always on the trail
    }
    return entry;
  }

  _delete(id) {
    const entry = this.aircraft.get(id);
    if (!entry) return;
    this.aircraft.delete(id);
    if (this.byAddress.get(entry.address) === id) this.byAddress.delete(entry.address);
  }

  _prune() {
    const oldest = this.now - this.trailSeconds;
    for (const entry of this.aircraft.values()) {
      const trail = entry.trail;
      let drop = 0;
      while (drop < trail.length - 1 && trail[drop][0] < oldest) drop += 1;
      if (drop) entry.trail = trail.slice(drop);
    }
  }
}

function appendPoint(trail, t, lon, lat, alt) {
  if (trail.length === 0 || t > trail[trail.length - 1][0]) trail.push([t, lon, lat, alt]);
}

/** Our own full-resolution points win; the server's thinner trail only adds what is older or newer. */
export function mergeTrail(own, theirs) {
  if (own.length === 0) return theirs.map((p) => [p[0], p[1], p[2], p[3]]);
  const first = own[0][0];
  const last = own[own.length - 1][0];
  const before = theirs.filter((p) => p[0] < first);
  const after = theirs.filter((p) => p[0] > last);
  return [...before, ...own, ...after].map((p) => [p[0], p[1], p[2], p[3]]);
}
