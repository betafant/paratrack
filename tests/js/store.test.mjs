import assert from 'node:assert/strict';
import { test } from 'node:test';
import { LiveStore, mergeTrail } from '../../prack/static/js/store.js';

const T0 = 1_800_000_000;

function aircraft(id, t, extra = {}) {
  return {
    id, address: id.slice(3), name: id, pilot: null, reg: null, cn: null, src: 'FLARM', flight_id: 1, flying: true,
    takeoff: T0, t, lat: 46.8, lon: 8.2 + (t - T0) * 1e-4, alt: 2000, gnd: 1500, spd: 36, vs: 1.5, hdg: 90, ...extra,
  };
}
const pt = (t) => [t, 8.2 + (t - T0) * 1e-4, 46.8, 2000, 36, 1.5, 90, 1500];
const message = (patch) => ({ seq: 1, now: T0 + 10, full: false, aircraft: [], removed: [], ...patch });

test('a full snapshot fills the store and builds each trail', () => {
  const store = new LiveStore();
  store.apply(message({
    full: true,
    aircraft: [{ ...aircraft('FLR112880', T0 + 9), trail: [[T0, 8.2, 46.8, 1900], [T0 + 3, 8.2003, 46.8, 1910]] }],
  }));
  assert.equal(store.size, 1);
  assert.equal(store.hasSnapshot, true);
  const a = store.find('112880');
  assert.deepEqual(a.trail.map((p) => p[0]), [T0, T0 + 3, T0 + 9]); // the newest position is always on the trail
  assert.equal(a.agl, 500);
});

test('a delta appends every received position, in order, without repeats', () => {
  const store = new LiveStore();
  store.apply(message({ full: true, aircraft: [aircraft('FLR1', T0 + 1)] }));
  store.apply(message({ aircraft: [{ ...aircraft('FLR1', T0 + 4), pts: [pt(T0 + 2), pt(T0 + 3), pt(T0 + 4)] }] }));
  store.apply(message({ aircraft: [{ ...aircraft('FLR1', T0 + 4), pts: [pt(T0 + 3), pt(T0 + 4)] }] })); // a repeat
  assert.deepEqual(store.find('1').trail.map((p) => p[0]), [T0 + 1, T0 + 2, T0 + 3, T0 + 4]);
  assert.equal(store.find('1').fresh.length, 2);
});

test('fresh points only live for one message', () => {
  const store = new LiveStore();
  store.apply(message({ aircraft: [{ ...aircraft('FLR1', T0 + 1), pts: [pt(T0 + 1)] }] }));
  store.apply(message({ aircraft: [] }));
  assert.equal(store.find('1').fresh.length, 0);
});

test('a full snapshot removes what it does not list, and keeps our finer trail', () => {
  const store = new LiveStore();
  store.apply(message({ aircraft: [
    { ...aircraft('FLR1', T0 + 3), pts: [pt(T0 + 1), pt(T0 + 2), pt(T0 + 3)] },
    aircraft('FLR2', T0 + 3),
  ] }));
  store.apply(message({ full: true, now: T0 + 12, aircraft: [{ ...aircraft('FLR1', T0 + 6), trail: [[T0 - 30, 8.0, 46.8, 1800], [T0 + 3, 8.2003, 46.8, 2000], [T0 + 6, 8.2006, 46.8, 2000]] }] }));
  assert.deepEqual([...store.aircraft.keys()], ['FLR1']);
  assert.deepEqual(store.find('1').trail.map((p) => p[0]), [T0 - 30, T0 + 1, T0 + 2, T0 + 3, T0 + 6]);
});

test('an aircraft that left and came back inside one window stays', () => {
  const store = new LiveStore();
  store.apply(message({ full: true, aircraft: [aircraft('FLR1', T0 + 1)] }));
  store.apply(message({ removed: ['FLR1'], aircraft: [aircraft('FLR1', T0 + 5, { flying: false })] }));
  assert.equal(store.find('1').flying, false);
  store.apply(message({ removed: ['FLR1'] }));
  assert.equal(store.size, 0);
  assert.equal(store.find('1'), undefined);
});

test('the same device under another protocol is one aircraft with one trail', () => {
  const store = new LiveStore();
  store.apply(message({ aircraft: [{ ...aircraft('FNT112880', T0 + 2), src: 'FANET', pts: [pt(T0 + 1), pt(T0 + 2)] }] }));
  store.apply(message({ aircraft: [{ ...aircraft('FLR112880', T0 + 4), pts: [pt(T0 + 3), pt(T0 + 4)] }], removed: ['FNT112880'] }));
  assert.equal(store.size, 1);
  assert.equal(store.get('FNT112880'), undefined);
  const a = store.find('112880');
  assert.equal(a.id, 'FLR112880');
  assert.equal(a.src, 'FLARM');
  assert.deepEqual(a.trail.map((p) => p[0]), [T0 + 1, T0 + 2, T0 + 3, T0 + 4]);
});

test('trails keep six minutes', () => {
  const store = new LiveStore();
  const pts = [];
  for (let i = 0; i <= 500; i++) pts.push(pt(T0 + i));
  store.apply(message({ now: T0 + 500, aircraft: [{ ...aircraft('FLR1', T0 + 500), pts }] }));
  const trail = store.find('1').trail;
  assert.equal(trail[0][0], T0 + 140);
  assert.equal(trail.length, 361);
});

test('counts and the ground filter', () => {
  const store = new LiveStore();
  store.apply(message({ aircraft: [aircraft('FLR1', T0), aircraft('FLR2', T0, { flying: false }), aircraft('FLR3', T0)] }));
  assert.deepEqual(store.counts(), { flying: 2, ground: 1 });
  assert.equal(store.list({ ground: false }).length, 2);
  assert.equal(store.list().length, 3);
});

test('mergeTrail adds only what is older or newer than our own points', () => {
  const own = [[10, 1, 1, 1], [11, 1, 1, 1], [12, 1, 1, 1]];
  const theirs = [[3, 0, 0, 0], [10, 9, 9, 9], [13, 2, 2, 2]];
  assert.deepEqual(mergeTrail(own, theirs).map((p) => p[0]), [3, 10, 11, 12, 13]);
  assert.deepEqual(mergeTrail(own, theirs)[1], [10, 1, 1, 1]);
  assert.deepEqual(mergeTrail([], theirs).map((p) => p[0]), [3, 10, 13]);
});
