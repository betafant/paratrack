// The faded trails of all flying aircraft as one deck.gl PathLayer in binary form (positions and a colour per vertex).
// Pure data logic, so it runs under `node --test` too.

/**
 * aircraft: store entries (`trail` is [[t, lon, lat, alt], ...]); now: server clock, epoch seconds.
 * Colour: the accent colour, transparent at the oldest end of the window and strong at the newest.
 */
export function buildTrails(aircraft, { mode3d, now, seconds = 360, spacing = 0, rgb, skip = null, maxAlpha = 200 }) {
  const picked = [];
  let vertices = 0;
  for (const a of aircraft) {
    if (!a.flying || a.address === skip || a.trail.length < 2) continue;
    const kept = thin(a.trail, spacing);
    picked.push(kept);
    vertices += kept.length;
  }
  const positions = new Float64Array(vertices * 3);
  const colors = new Uint8Array(vertices * 4);
  const startIndices = [];
  let v = 0;
  for (const kept of picked) {
    startIndices.push(v);
    for (const [t, lon, lat, alt] of kept) {
      positions[v * 3] = lon;
      positions[v * 3 + 1] = lat;
      positions[v * 3 + 2] = mode3d ? alt : 0;
      const age = Math.min(1, Math.max(0, (t - (now - seconds)) / seconds));
      colors[v * 4] = rgb[0];
      colors[v * 4 + 1] = rgb[1];
      colors[v * 4 + 2] = rgb[2];
      colors[v * 4 + 3] = Math.round(maxAlpha * age ** 1.3);
      v += 1;
    }
  }
  return {
    length: picked.length,
    startIndices,
    attributes: { getPath: { value: positions, size: 3 }, getColor: { value: colors, size: 4, normalized: true } },
  };
}

/**
 * Keep a point only when it is at least `spacing` metres from the last kept one: at a low zoom a trail needs a few
 * points, not hundreds. The first and the newest point always stay.
 */
export function thin(trail, spacing) {
  if (spacing <= 0 || trail.length <= 2) return trail;
  const kept = [trail[0]];
  const last = trail.length - 1;
  const lat0 = (trail[0][2] * Math.PI) / 180;
  const kx = 111320 * Math.cos(lat0);
  let [, px, py] = trail[0];
  const limit = spacing * spacing;
  for (let i = 1; i < last; i++) {
    const dx = (trail[i][1] - px) * kx;
    const dy = (trail[i][2] - py) * 110540;
    if (dx * dx + dy * dy >= limit) {
      kept.push(trail[i]);
      px = trail[i][1];
      py = trail[i][2];
    }
  }
  kept.push(trail[last]);
  return kept;
}
