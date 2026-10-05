// The day view's paths: every flight of a day as one deck.gl PathLayer in binary form. Pure data logic.

import { altitudeColor } from './colors.js';

/** A track from the API's columns, kept to about `maxPoints` points: [[lon, lat, alt], ...]. */
export function simplifyColumns(columns, maxPoints = 500) {
  const n = columns?.t?.length ?? 0;
  const step = Math.max(1, Math.ceil(n / maxPoints));
  const out = [];
  for (let i = 0; i < n; i += step) out.push([columns.lon[i], columns.lat[i], columns.alt[i]]);
  if (n && (n - 1) % step !== 0) out.push([columns.lon[n - 1], columns.lat[n - 1], columns.alt[n - 1]]);
  return out;
}

/**
 * flights: [{ id, path: [[lon, lat, alt], ...] | null }]. Returns { data, owners }: the binary PathLayer data and,
 * for each path in it, the index of its flight in `flights` (deck.gl reports a picked path by its number).
 * The flight with `selectedId` is left out (its full track is drawn instead); with a selection the others are dimmed
 * (`dim` asks for that without leaving a flight out, while the selected one's track is still loading).
 */
export function buildPreviews(flights, { mode3d, selectedId = null, dim = selectedId !== null, alpha = 175, dimmedAlpha = 55 }) {
  const picked = [];
  let vertices = 0;
  flights.forEach((flight, index) => {
    if (!flight.path || flight.path.length < 2 || String(flight.id) === String(selectedId)) return;
    picked.push([flight, index]);
    vertices += flight.path.length;
  });
  const a = dim ? dimmedAlpha : alpha;
  const positions = new Float64Array(vertices * 3);
  const colors = new Uint8Array(vertices * 4);
  const startIndices = [];
  const owners = [];
  let v = 0;
  for (const [flight, index] of picked) {
    startIndices.push(v);
    owners.push(index);
    for (const [lon, lat, alt] of flight.path) {
      positions[v * 3] = lon;
      positions[v * 3 + 1] = lat;
      positions[v * 3 + 2] = mode3d ? alt : 0;
      colors.set(altitudeColor(alt, a), v * 4);
      v += 1;
    }
  }
  return {
    owners,
    data: {
      length: picked.length,
      startIndices,
      attributes: { getPath: { value: positions, size: 3 }, getColor: { value: colors, size: 4, normalized: true } },
    },
  };
}

/** One flight, bold, for the one under the pointer. */
export const buildHighlight = (flight, { mode3d }) =>
  flight?.path ? buildPreviews([flight], { mode3d, alpha: 255 }).data : null;

/** The box around the flights of a day: [west, south, east, north], or null when there is nothing to show. */
export function unionBounds(flights) {
  let box = null;
  for (const f of flights) {
    const b = f.bbox;
    if (!b || b.some((v) => v === null || v === undefined || !Number.isFinite(v))) continue;
    box = box ? [Math.min(box[0], b[0]), Math.min(box[1], b[1]), Math.max(box[2], b[2]), Math.max(box[3], b[3])] : [...b];
  }
  return box;
}
