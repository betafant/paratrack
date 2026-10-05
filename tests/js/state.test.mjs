import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DEFAULT_STATE, formatHash, navigationKey, parseHash } from '../../prack/static/js/state.js';

test('an empty or unknown hash is the live view', () => {
  for (const hash of ['', '#', '#/', '#/live', '#/nowhere', '#garbage?x=1']) assert.deepEqual(parseHash(hash), DEFAULT_STATE, hash);
});

test('selection and view are read from the query', () => {
  const s = parseHash('#/live?sel=112880&view=3d');
  assert.equal(s.sel, '112880');
  assert.equal(s.view, '3d');
});

test('camera: latitude first, validated', () => {
  const s = parseHash('#/live?ll=46.8,8.23&z=7.4');
  assert.deepEqual(s.ll, [46.8, 8.23]);
  assert.equal(s.z, 7.4);
  for (const bad of ['ll=91,8', 'll=46.8', 'll=a,b', 'll=,', 'z=99', 'z=', 'z=x']) {
    const t = parseHash(`#/live?${bad}`);
    assert.ok(t.ll === null || bad.startsWith('z'), bad);
    assert.ok(t.z === null || !bad.startsWith('z'), bad);
  }
});

test('hostile or odd selection values are ignored', () => {
  for (const sel of ['<script>', 'a b', '../../x', 'x'.repeat(40), '']) {
    assert.equal(parseHash(`#/live?sel=${encodeURIComponent(sel)}`).sel, null, sel);
  }
  assert.equal(parseHash('#/live?view=4d').view, '2d');
});

test('formatHash writes only what differs from the default and round-trips', () => {
  assert.equal(formatHash(DEFAULT_STATE), '#/live');
  const state = { route: 'live', day: null, sel: 'D00001', view: '3d', ll: [46.8, 8.23], z: 9.5 };
  const hash = formatHash(state);
  assert.equal(hash, '#/live?sel=D00001&view=3d&ll=46.80000,8.23000&z=9.50');
  assert.deepEqual(parseHash(hash), state);
});

test('the navigation key ignores the camera, so panning does not fill the history', () => {
  const a = { ...DEFAULT_STATE, sel: 'X1', ll: [1, 2], z: 5 };
  const b = { ...a, ll: [3, 4], z: 9 };
  assert.equal(navigationKey(a), navigationKey(b));
  assert.notEqual(navigationKey(a), navigationKey({ ...a, view: '3d' }));
  assert.notEqual(navigationKey(a), navigationKey({ ...a, sel: null }));
});

test('the day route carries a date and a flight id', () => {
  const s = parseHash('#/day/2026-07-15?sel=1234&view=3d');
  assert.deepEqual([s.route, s.day, s.sel, s.view], ['day', '2026-07-15', '1234', '3d']);
  assert.equal(formatHash(s), '#/day/2026-07-15?sel=1234&view=3d');
  assert.deepEqual(parseHash(formatHash(s)), s);
});

test('a day that is not a date falls back to the live view', () => {
  for (const hash of ['#/day', '#/day/', '#/day/2026-13-01', '#/day/2026-02-30', '#/day/yesterday', '#/day/20260715']) {
    assert.equal(parseHash(hash).route, 'live', hash);
    assert.equal(parseHash(hash).day, null, hash);
  }
});

test('changing the day makes a history entry, moving the camera does not', () => {
  const a = parseHash('#/day/2026-07-15?ll=46.8,8.2&z=9');
  assert.notEqual(navigationKey(a), navigationKey({ ...a, day: '2026-07-16' }));
  assert.equal(navigationKey(a), navigationKey({ ...a, ll: [1, 1], z: 3 }));
  assert.notEqual(navigationKey(a), navigationKey({ ...a, route: 'live', day: null }));
});
