import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildHighlight, buildPreviews, simplifyColumns, unionBounds } from '../../prack/static/js/previews.js';

const path = (n, lon0 = 8) => Array.from({ length: n }, (_, i) => [lon0 + i * 0.001, 46.8, 1000 + i * 10]);
const flight = (id, p) => ({ id, path: p, bbox: [8, 46.8, 8.1, 46.9] });

test('one path per flight with a path, and the index of its flight', () => {
  const flights = [flight(1, path(5)), flight(2, null), flight(3, path(1)), flight(4, path(3))];
  const { data, owners } = buildPreviews(flights, { mode3d: false });
  assert.equal(data.length, 2);
  assert.deepEqual(data.startIndices, [0, 5]);
  assert.deepEqual(owners, [0, 3]); // flights 2 and 3 have nothing to draw
  assert.equal(data.attributes.getPath.value.length, 8 * 3);
});

test('flat in 2D, at GPS altitude in 3D, coloured by altitude', () => {
  const flights = [flight(1, path(3))];
  const flat = buildPreviews(flights, { mode3d: false }).data.attributes;
  const high = buildPreviews(flights, { mode3d: true }).data.attributes;
  assert.equal(flat.getPath.value[2], 0);
  assert.equal(high.getPath.value[2], 1000);
  assert.equal(high.getPath.value[5], 1010);
  assert.equal(high.getColor.value.length, 12); // one RGBA per vertex
  const low = buildPreviews([flight(1, [[8, 46, 500], [8.1, 46, 500]])], { mode3d: false }).data.attributes.getColor.value;
  const top = buildPreviews([flight(1, [[8, 46, 3500], [8.1, 46, 3500]])], { mode3d: false }).data.attributes.getColor.value;
  assert.notDeepEqual([...low.slice(0, 3)], [...top.slice(0, 3)]);
});

test('with a selection the others are dimmed and the selected one is left out', () => {
  const flights = [flight(1, path(3)), flight(2, path(3)), flight(3, path(3))];
  const all = buildPreviews(flights, { mode3d: false });
  const some = buildPreviews(flights, { mode3d: false, selectedId: '2' }); // ids compare as text
  assert.equal(some.data.length, 2);
  assert.deepEqual(some.owners, [0, 2]);
  assert.ok(some.data.attributes.getColor.value[3] < all.data.attributes.getColor.value[3]);
});

test('highlight: one bold flight, or nothing', () => {
  assert.equal(buildHighlight(flight(1, path(4)), { mode3d: false }).length, 1);
  assert.equal(buildHighlight(flight(1, path(4)), { mode3d: false }).attributes.getColor.value[3], 255);
  assert.equal(buildHighlight(flight(1, null), { mode3d: false }), null);
  assert.equal(buildHighlight(undefined, { mode3d: false }), null);
});

test('simplifyColumns keeps about maxPoints points, the first and the last', () => {
  const n = 2345;
  const cols = {
    t: Array.from({ length: n }, (_, i) => i),
    lon: Array.from({ length: n }, (_, i) => 8 + i * 1e-4),
    lat: Array.from({ length: n }, () => 46.8),
    alt: Array.from({ length: n }, (_, i) => 1000 + i),
  };
  const out = simplifyColumns(cols, 500);
  assert.ok(out.length >= 400 && out.length <= 501, `${out.length}`);
  assert.deepEqual(out[0], [8, 46.8, 1000]);
  assert.deepEqual(out[out.length - 1], [8 + (n - 1) * 1e-4, 46.8, 1000 + n - 1]);
  assert.deepEqual(simplifyColumns({ t: [] }), []);
  assert.equal(simplifyColumns({ t: [1], lon: [8], lat: [46], alt: [1] }).length, 1);
});

test('unionBounds: the box around all flights, ignoring flights without one', () => {
  const flights = [
    { bbox: [8, 46.5, 8.2, 46.7] },
    { bbox: [7.9, 46.6, 8.1, 46.9] },
    { bbox: [null, null, null, null] },
    {},
  ];
  assert.deepEqual(unionBounds(flights), [7.9, 46.5, 8.2, 46.9]);
  assert.equal(unionBounds([]), null);
  assert.equal(unionBounds([{ bbox: [null, 1, 2, 3] }]), null);
});
