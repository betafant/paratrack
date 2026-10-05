// The whole track of the selected flight: what /api/flights/{id}/track returned, plus every live point since.
// Pure data logic (no DOM, no map).

import { haversine } from './format.js';

const COLUMNS = ['t', 'lat', 'lon', 'alt', 'gnd', 'spd', 'vs', 'hdg'];

export class TrackBuffer {
  constructor() {
    this.reset(null);
  }

  reset(flightId) {
    this.flightId = flightId;
    for (const c of COLUMNS) this[c] = [];
    this.distance = 0; // metres along the track
    this.bounds = null; // [west, south, east, north]
    this.version = 0; // moves whenever points are added
    this.loaded = false; // the stored track has arrived (live points wait for it)
    this.waiting = []; // live points that came while it was loading
  }

  get length() {
    return this.t.length;
  }

  get lastTime() {
    return this.t.length ? this.t[this.t.length - 1] : -Infinity;
  }

  /** The stored track: columns as the API sends them. Points that arrived meanwhile are added after it. */
  setColumns(columns) {
    for (const c of COLUMNS) this[c] = [];
    this.distance = 0;
    this.bounds = null;
    const n = columns.t?.length ?? 0;
    for (let i = 0; i < n; i++) {
      this._push(
        columns.t[i], columns.lat[i], columns.lon[i], columns.alt[i],
        columns.gnd?.[i] ?? null, columns.spd?.[i] ?? null, columns.vs?.[i] ?? null, columns.hdg?.[i] ?? null,
      ); // prettier-ignore
    }
    this.loaded = true;
    const waiting = this.waiting;
    this.waiting = [];
    this.addPoints(waiting);
    this.version += 1;
  }

  /** Live points as the stream sends them: [t, lon, lat, alt, spd, vs, hdg, gnd]. */
  addPoints(points) {
    if (!points?.length) return 0;
    if (!this.loaded) {
      this.waiting.push(...points);
      return 0;
    }
    let added = 0;
    for (const p of points) {
      if (p[0] > this.lastTime) {
        this._push(p[0], p[2], p[1], p[3], p[7] ?? null, p[4] ?? null, p[5] ?? null, p[6] ?? null);
        added += 1;
      }
    }
    if (added) this.version += 1;
    return added;
  }

  _push(t, lat, lon, alt, gnd, spd, vs, hdg) {
    const n = this.t.length;
    if (n) this.distance += haversine(this.lat[n - 1], this.lon[n - 1], lat, lon);
    this.t.push(t);
    this.lat.push(lat);
    this.lon.push(lon);
    this.alt.push(alt);
    this.gnd.push(gnd);
    this.spd.push(spd);
    this.vs.push(vs);
    this.hdg.push(hdg);
    const b = this.bounds;
    if (b === null) this.bounds = [lon, lat, lon, lat];
    else {
      if (lon < b[0]) b[0] = lon;
      if (lat < b[1]) b[1] = lat;
      if (lon > b[2]) b[2] = lon;
      if (lat > b[3]) b[3] = lat;
    }
  }

  /** A deck.gl PathLayer in binary form: one path, positions [lon, lat, z], one colour per vertex. */
  path({ mode3d, color }) {
    const n = this.length;
    const positions = new Float64Array(n * 3);
    const colors = new Uint8Array(n * 4);
    for (let i = 0; i < n; i++) {
      positions[i * 3] = this.lon[i];
      positions[i * 3 + 1] = this.lat[i];
      positions[i * 3 + 2] = mode3d ? this.alt[i] : 0;
      const c = color(this.alt[i]);
      colors.set(c, i * 4);
    }
    return {
      length: n ? 1 : 0,
      startIndices: n ? [0] : [],
      attributes: { getPath: { value: positions, size: 3 }, getColor: { value: colors, size: 4, normalized: true } },
    };
  }

  /**
   * Vertical lines from the track down to the ground, about `count` of them: the faint curtain in the 3D view.
   * Where the track has no terrain height, `groundAt(lon, lat)` may know it (the map's terrain).
   */
  curtain({ count = 400, groundAt = () => null } = {}) {
    const step = Math.max(1, Math.floor(this.length / count));
    const from = [];
    const to = [];
    for (let i = 0; i < this.length; i += step) {
      const g = this.gnd[i] ?? groundAt(this.lon[i], this.lat[i]);
      if (g == null || this.alt[i] - g < 5) continue;
      from.push(this.lon[i], this.lat[i], this.alt[i]);
      to.push(this.lon[i], this.lat[i], g);
    }
    return {
      length: from.length / 3,
      attributes: {
        getSourcePosition: { value: new Float64Array(from), size: 3 },
        getTargetPosition: { value: new Float64Array(to), size: 3 },
      },
    };
  }
}
