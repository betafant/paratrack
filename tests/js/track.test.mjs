import assert from 'node:assert/strict';
import { test } from 'node:test';
import { TrackBuffer } from '../../prack/static/js/track.js';

const columns = (n, t0 = 100) => ({
  t: Array.from({ length: n }, (_, i) => t0 + i),
  lat: Array.from({ length: n }, () => 46.8),
  lon: Array.from({ length: n }, (_, i) => 8.2 + i * 0.001),
  alt: Array.from({ length: n }, (_, i) => 1000 + i),
  gnd: Array.from({ length: n }, () => 800),
  spd: Array.from({ length: n }, () => 35),
  vs: Array.from({ length: n }, () => 1),
  hdg: Array.from({ length: n }, () => 90),
});
const live = (t, lon = 8.3) => [t, lon, 46.8, 1200, 33, 0.5, 91, 790];

test('the stored track sets length, distance and bounds', () => {
  const track = new TrackBuffer();
  track.reset(7);
  track.setColumns(columns(11));
  assert.equal(track.length, 11);
  assert.ok(Math.abs(track.distance - 10 * 75.9) < 3, `distance ${track.distance}`); // 0.001 deg of lon at 46.8 N
  assert.deepEqual(track.bounds.map((v) => Math.round(v * 1e6) / 1e6), [8.2, 46.8, 8.21, 46.8]);
  assert.equal(track.loaded, true);
});

test('live points wait for the stored track and are not repeated after it', () => {
  const track = new TrackBuffer();
  track.reset(7);
  track.addPoints([live(108), live(111), live(112)]);
  assert.equal(track.length, 0);
  track.setColumns(columns(11)); // t = 100..110
  assert.deepEqual(track.t.slice(-3), [110, 111, 112]);
  assert.equal(track.addPoints([live(112), live(113)]), 1);
  assert.equal(track.lastTime, 113);
});

test('live points use the stream order of fields', () => {
  const track = new TrackBuffer();
  track.reset(1);
  track.setColumns({ t: [] });
  track.addPoints([[50, 8.5, 46.9, 1234, 30, 2.5, 180, 900]]);
  assert.deepEqual([track.lon[0], track.lat[0], track.alt[0], track.spd[0], track.vs[0], track.hdg[0], track.gnd[0]], [8.5, 46.9, 1234, 30, 2.5, 180, 900]);
});

test('reset forgets everything and bumps nothing it should not', () => {
  const track = new TrackBuffer();
  track.reset(1);
  track.setColumns(columns(5));
  track.reset(2);
  assert.equal(track.length, 0);
  assert.equal(track.distance, 0);
  assert.equal(track.bounds, null);
  assert.equal(track.loaded, false);
  assert.equal(track.flightId, 2);
});

test('the version moves when points are added', () => {
  const track = new TrackBuffer();
  track.reset(1);
  track.setColumns(columns(3));
  const v = track.version;
  track.addPoints([live(100)]); // old
  assert.equal(track.version, v);
  track.addPoints([live(200)]);
  assert.equal(track.version, v + 1);
});

test('path(): binary deck.gl data, flat in 2D and at true altitude in 3D', () => {
  const track = new TrackBuffer();
  track.reset(1);
  track.setColumns(columns(4));
  const flat = track.path({ mode3d: false, color: () => [1, 2, 3, 255] });
  assert.equal(flat.length, 1);
  assert.deepEqual(flat.startIndices, [0]);
  assert.equal(flat.attributes.getPath.value.length, 12);
  assert.equal(flat.attributes.getPath.value[2], 0);
  const high = track.path({ mode3d: true, color: (alt) => [alt % 256, 0, 0, 255] });
  assert.equal(high.attributes.getPath.value[2], 1000);
  assert.equal(high.attributes.getPath.value[5], 1001);
  assert.deepEqual([...high.attributes.getColor.value.slice(0, 4)], [1000 % 256, 0, 0, 255]);
});

test('an empty track has no path', () => {
  const track = new TrackBuffer();
  track.reset(1);
  assert.equal(track.path({ mode3d: false, color: () => [0, 0, 0, 0] }).length, 0);
});

test('curtain(): vertical lines from the track to the ground, thinned to about the requested count', () => {
  const track = new TrackBuffer();
  track.reset(1);
  track.setColumns(columns(1000));
  const c = track.curtain({ count: 100 });
  assert.ok(c.length >= 90 && c.length <= 110, `${c.length} lines`);
  const from = c.attributes.getSourcePosition.value;
  const to = c.attributes.getTargetPosition.value;
  assert.equal(from[2], 1000);
  assert.equal(to[2], 800);
  assert.equal(from[0], to[0]);
});

test('curtain(): the map may know the ground where the track does not; no ground means no line', () => {
  const track = new TrackBuffer();
  track.reset(1);
  const cols = columns(10);
  cols.gnd = cols.gnd.map(() => null);
  track.setColumns(cols);
  assert.equal(track.curtain({}).length, 0);
  assert.equal(track.curtain({ groundAt: () => 995 }).length, 10);
  assert.equal(track.curtain({ groundAt: () => 1000 }).length, 5); // lines shorter than 5 m are not worth drawing
  assert.equal(track.curtain({ groundAt: () => 2000 }).length, 0); // a point under the ground has no line
});
