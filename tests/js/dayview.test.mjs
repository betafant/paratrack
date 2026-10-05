import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DayView } from '../../prack/static/js/dayview.js';

const columns = (n) => ({
  t: Array.from({ length: n }, (_, i) => i),
  lon: Array.from({ length: n }, (_, i) => 8 + i * 1e-4),
  lat: Array.from({ length: n }, () => 46.8),
  alt: Array.from({ length: n }, () => 1500),
});
const closed = (id) => ({ id, date: '2026-07-15', live: false, preview: [[8, 46.8, 1000], [8.1, 46.8, 1500]], bbox: [8, 46.8, 8.1, 46.8] });
const open = (id) => ({ id, date: '2026-07-15', live: true, preview: null, bbox: [8, 46.8, 8.1, 46.8] });
const settle = () => new Promise((resolve) => setTimeout(resolve, 5));

function make(days, tracks = {}) {
  const calls = { days: [], tracks: [], running: 0, peak: 0 };
  const view = new DayView({
    fetchDay: async (day) => {
      calls.days.push(day);
      if (days[day] instanceof Error) throw days[day];
      return days[day] ?? [];
    },
    fetchTrack: async (id) => {
      calls.tracks.push(id);
      calls.running += 1;
      calls.peak = Math.max(calls.peak, calls.running);
      await settle();
      calls.running -= 1;
      if (tracks[id] instanceof Error) throw tracks[id];
      return tracks[id] ?? columns(1200);
    },
    concurrency: 2,
  });
  return { view, calls };
}

test('a day is loaded with the previews the server stored', async () => {
  const { view, calls } = make({ '2026-07-15': [closed(1), closed(2)] });
  assert.equal(await view.load('2026-07-15'), true);
  assert.equal(view.day, '2026-07-15');
  assert.deepEqual(view.flights.map((f) => f.id), [1, 2]);
  assert.equal(view.flights[0].path.length, 2);
  assert.equal(view.loading, false);
  assert.deepEqual(calls.tracks, []); // nothing to fetch
  assert.equal(view.get('2').id, 2); // ids compare as text
});

test('flights still in the air get their path from the track endpoint, a few at a time', async () => {
  const { view, calls } = make({ '2026-07-15': [open(1), open(2), open(3), closed(4)] });
  await view.load('2026-07-15');
  assert.equal(view.flights[0].path, null);
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.deepEqual(calls.tracks.sort(), [1, 2, 3]);
  assert.equal(calls.peak, 2); // the concurrency limit
  for (const id of [1, 2, 3]) {
    const path = view.get(id).path;
    assert.ok(path.length > 100 && path.length <= 501);
  }
});

test('asking for another day drops the work of the first', async () => {
  const { view } = make({ '2026-07-15': [open(1), open(2), open(3)], '2026-07-14': [closed(9)] });
  const first = view.load('2026-07-15');
  const second = view.load('2026-07-14');
  assert.equal(await first, false); // overtaken
  assert.equal(await second, true);
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.deepEqual(view.flights.map((f) => f.id), [9]);
  assert.equal(view.day, '2026-07-14');
});

test('a refresh keeps what is shown and the paths already fetched until the new list arrives', async () => {
  const { view } = make({ '2026-07-15': [open(1)] });
  await view.load('2026-07-15');
  await new Promise((resolve) => setTimeout(resolve, 30));
  const path = view.get(1).path;
  assert.ok(path);
  const refreshed = view.load('2026-07-15', { keep: true });
  assert.equal(view.flights.length, 1); // still there while loading
  await refreshed;
  assert.equal(view.get(1).path, path); // carried over, no flicker
});

test('a day that cannot be loaded is reported, and the view is empty and not loading', async () => {
  const { view } = make({ '2026-07-15': new Error('HTTP 500') });
  await assert.rejects(view.load('2026-07-15'), /500/);
  assert.equal(view.failed, true);
  assert.equal(view.loading, false);
  assert.deepEqual(view.flights, []);
});

test('a track that cannot be fetched leaves its flight without a path, the others are fine', async () => {
  const { view } = make({ '2026-07-15': [open(1), open(2)] }, { 1: new Error('gone') });
  await view.load('2026-07-15');
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.equal(view.get(1).path, null);
  assert.ok(view.get(2).path.length > 100);
});

test('onChange is told about every step, and clear() empties the view', async () => {
  const { view } = make({ '2026-07-15': [closed(1)] });
  let changes = 0;
  view.onChange = () => (changes += 1);
  await view.load('2026-07-15');
  assert.ok(changes >= 2); // loading, loaded
  const version = view.version;
  view.clear();
  assert.deepEqual(view.flights, []);
  assert.equal(view.day, null);
  assert.ok(view.version > version);
});
