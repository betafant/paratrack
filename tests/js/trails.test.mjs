import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildTrails, thin } from '../../prack/static/js/trails.js';

const NOW = 1_800_000_000;
const flyer = (address, n = 60, extra = {}) => ({
  address, flying: true,
  trail: Array.from({ length: n }, (_, i) => [NOW - n + 1 + i, 8.2 + i * 1e-4, 46.8, 1500 + i]),
  ...extra,
});

test('one path per flying aircraft with at least two points', () => {
  const data = buildTrails([flyer('A'), flyer('B', 1), flyer('C', 60, { flying: false }), flyer('D')], { mode3d: false, now: NOW, rgb: [10, 20, 30] });
  assert.equal(data.length, 2);
  assert.deepEqual(data.startIndices, [0, 60]);
  assert.equal(data.attributes.getPath.value.length, 120 * 3);
});

test('the selected aircraft is left out: its whole track is drawn instead', () => {
  const data = buildTrails([flyer('A'), flyer('B')], { mode3d: false, now: NOW, rgb: [0, 0, 0], skip: 'A' });
  assert.equal(data.length, 1);
});

test('flat in 2D, at altitude in 3D', () => {
  const a = [flyer('A', 5)];
  assert.equal(buildTrails(a, { mode3d: false, now: NOW, rgb: [0, 0, 0] }).attributes.getPath.value[2], 0);
  assert.equal(buildTrails(a, { mode3d: true, now: NOW, rgb: [0, 0, 0] }).attributes.getPath.value[2], 1500);
});

test('the trail fades: transparent at the old end, strong at the new end, the colour is the accent', () => {
  const data = buildTrails([flyer('A', 360)], { mode3d: false, now: NOW, rgb: [10, 20, 30], seconds: 360, maxAlpha: 200 });
  const c = data.attributes.getColor.value;
  assert.deepEqual([...c.slice(0, 3)], [10, 20, 30]);
  assert.ok(c[3] <= 1, `oldest alpha ${c[3]}`);
  assert.equal(c[(359) * 4 + 3], 200);
  assert.ok(c[180 * 4 + 3] > c[90 * 4 + 3]);
});

test('thin(): keeps points about `spacing` metres apart, always the first and the last', () => {
  const trail = Array.from({ length: 101 }, (_, i) => [i, 8.2 + i * 1e-4, 46.8, 0]); // about 7.6 m apart
  const kept = thin(trail, 40);
  assert.equal(kept[0], trail[0]);
  assert.equal(kept[kept.length - 1], trail[100]);
  assert.ok(kept.length >= 15 && kept.length <= 22, `${kept.length} points`);
  assert.equal(thin(trail, 0), trail);
  assert.equal(thin(trail.slice(0, 2), 1000).length, 2);
});

test('thinning applies to every trail, so far-away views stay light', () => {
  const dense = Array.from({ length: 20 }, (_, i) => flyer('A' + i, 360));
  const all = buildTrails(dense, { mode3d: false, now: NOW, rgb: [0, 0, 0] });
  const thinned = buildTrails(dense, { mode3d: false, now: NOW, rgb: [0, 0, 0], spacing: 100 });
  assert.equal(all.attributes.getPath.value.length / 3, 20 * 360);
  assert.ok(thinned.attributes.getPath.value.length / 3 < 20 * 40);
});
