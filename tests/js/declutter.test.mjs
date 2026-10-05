import assert from 'node:assert/strict';
import { test } from 'node:test';
import { declutter } from '../../prack/static/js/declutter.js';

const SCREEN = { width: 800, height: 600 };
const item = (key, x, y, width = 100, priority = 0, extra = {}) => ({ key, x, y, height: 20, priority, variants: [{ id: 'full', width }], ...extra });

test('a lone label goes to the right of its marker', () => {
  const placed = declutter([item('a', 300, 300)], SCREEN);
  assert.deepEqual(placed.get('a'), { side: 'right', variant: 'full' });
});

test('of two overlapping labels the more important one stays', () => {
  const placed = declutter([item('low', 300, 300, 100, 1), item('high', 310, 305, 100, 9)], SCREEN);
  assert.ok(placed.has('high'));
  assert.ok(!placed.has('low') || placed.get('low').side === 'left');
});

test('a label that would run off the right edge goes to the left', () => {
  const placed = declutter([item('a', 760, 300)], SCREEN);
  assert.equal(placed.get('a').side, 'left');
});

test('a label that fits on neither side is hidden', () => {
  assert.equal(declutter([item('a', 100, 300, 900)], SCREEN).size, 0);
});

test('labels do not cover other markers', () => {
  const placed = declutter([item('a', 300, 300, 100, 1), item('b', 330, 300, 100, 0)], SCREEN);
  // b's marker sits where a's right-hand label would be: a flips to the left side
  assert.equal(placed.get('a').side, 'left');
});

test('its own marker never blocks a label', () => {
  assert.equal(declutter([item('a', 300, 300)], SCREEN).size, 1);
});

test('a shorter variant is used when the full text does not fit', () => {
  const wide = item('a', 150, 300, 0, 0, { variants: [{ id: 'full', width: 400 }, { id: 'name', width: 60 }] });
  const placed = declutter([wide], SCREEN);
  assert.deepEqual(placed.get('a'), { side: 'right', variant: 'full' });
  const narrow = declutter([wide], { width: 300, height: 600 });
  assert.deepEqual(narrow.get('a'), { side: 'right', variant: 'name' });
});

test('markers off the screen get no label', () => {
  assert.equal(declutter([item('a', -400, 300), item('b', 300, 2000)], SCREEN).size, 0);
});

test('a crowd: no two placed labels overlap', () => {
  const crowd = [];
  for (let i = 0; i < 60; i++) crowd.push(item(`p${i}`, 20 + ((i * 97) % 760), 20 + ((i * 53) % 560), 90 + (i % 5) * 20, i));
  const placed = declutter(crowd, SCREEN);
  const boxes = [...placed].map(([key, { side }]) => {
    const it = crowd.find((c) => c.key === key);
    const x0 = side === 'right' ? it.x + 14 : it.x - 14 - it.variants[0].width;
    return { key, x0, x1: x0 + it.variants[0].width, y0: it.y - 10, y1: it.y + 10 };
  });
  assert.ok(boxes.length > 5);
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i];
      const b = boxes[j];
      assert.ok(a.x1 <= b.x0 || b.x1 <= a.x0 || a.y1 <= b.y0 || b.y1 <= a.y0, `${a.key} overlaps ${b.key}`);
    }
  }
});
