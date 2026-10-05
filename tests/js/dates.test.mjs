import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  addDays, addMonths, compareDays, daysInMonth, formatDay, fromParts, isDay, monthGrid, monthRange, monthTitle,
  partsOf, shadeLevel, todayIn, weekdayNames,
} from '../../prack/static/js/dates.js';

test('isDay accepts real dates only', () => {
  for (const ok of ['2026-07-15', '2024-02-29', '2000-01-01']) assert.equal(isDay(ok), true, ok);
  for (const bad of ['2026-02-29', '2026-13-01', '2026-00-10', '2026-7-15', '15-07-2026', '', null, undefined, '1999-12-31', '2026-07-15x']) {
    assert.equal(isDay(bad), false, String(bad));
  }
});

test('adding days crosses month and year borders and leap days', () => {
  assert.equal(addDays('2026-07-31', 1), '2026-08-01');
  assert.equal(addDays('2026-01-01', -1), '2025-12-31');
  assert.equal(addDays('2024-02-28', 1), '2024-02-29');
  assert.equal(addDays('2026-03-29', 1), '2026-03-30'); // the clocks change here in Zurich: days do not care
  assert.equal(addDays('2026-07-15', 0), '2026-07-15');
  assert.equal(addDays('2026-07-15', 400), '2027-08-19');
});

test('months: arithmetic, length and range', () => {
  assert.deepEqual(addMonths(2026, 11, 1), { year: 2027, month: 0 });
  assert.deepEqual(addMonths(2026, 0, -1), { year: 2025, month: 11 });
  assert.deepEqual(addMonths(2026, 5, 18), { year: 2027, month: 11 });
  assert.equal(daysInMonth(2026, 1), 28);
  assert.equal(daysInMonth(2024, 1), 29);
  assert.deepEqual(monthRange(2026, 6), ['2026-07-01', '2026-07-31']);
  assert.deepEqual(partsOf('2026-07-15'), { year: 2026, month: 6, day: 15 });
  assert.equal(fromParts(2026, 6, 5), '2026-07-05');
});

test('the month grid starts on Monday and pads with blanks', () => {
  const weeks = monthGrid(2026, 6); // July 2026 starts on a Wednesday
  assert.equal(weeks.length, 5);
  assert.ok(weeks.every((w) => w.length === 7));
  assert.deepEqual(weeks[0].slice(0, 3), [null, null, '2026-07-01']);
  assert.equal(weeks[4][4], '2026-07-31'); // a Friday
  assert.deepEqual(weeks[4].slice(5), [null, null]);
  assert.equal(weeks.flat().filter(Boolean).length, 31);
  assert.equal(monthGrid(2026, 5)[0][0], '2026-06-01'); // June 2026 starts on a Monday
  assert.equal(monthGrid(2027, 1).length, 4); // February 2027 starts on a Monday and has 28 days: four weeks
  assert.equal(monthGrid(2026, 1)[0][6], '2026-02-01'); // February 2026 starts on a Sunday
});

test('names for the calendar', () => {
  assert.deepEqual(weekdayNames(), ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']);
  assert.equal(monthTitle(2026, 6), 'July 2026');
  assert.equal(formatDay('2026-07-15'), 'Wed 15 Jul');
  assert.equal(formatDay('2026-07-15', { long: true }), 'Wednesday 15 July 2026');
});

test('today depends on the region, not on the browser', () => {
  const instant = new Date(Date.UTC(2026, 6, 15, 22, 30)); // 22:30 UTC
  assert.equal(todayIn('Europe/Zurich', instant), '2026-07-16'); // already after midnight in Zurich (UTC+2)
  assert.equal(todayIn('UTC', instant), '2026-07-15');
  assert.equal(todayIn('Pacific/Auckland', instant), '2026-07-16');
  assert.ok(isDay(todayIn('No/Such_Zone', instant)));
});

test('days compare as strings', () => {
  assert.equal(compareDays('2026-07-15', '2026-07-16'), -1);
  assert.equal(compareDays('2026-07-15', '2026-07-15'), 0);
  assert.equal(compareDays('2027-01-01', '2026-12-31'), 1);
});

test('shading: nothing is nothing, the busiest day is the darkest, in between is square-root scaled', () => {
  assert.equal(shadeLevel(0, 50), 0);
  assert.equal(shadeLevel(undefined, 50), 0);
  assert.equal(shadeLevel(1, 50), 1);
  assert.equal(shadeLevel(50, 50), 4);
  assert.equal(shadeLevel(12, 50), 2);
  assert.equal(shadeLevel(30, 50), 4); // sqrt(0.6) = 0.77
  const levels = [1, 5, 12, 25, 50].map((n) => shadeLevel(n, 50));
  assert.deepEqual(levels, [...levels].sort((a, b) => a - b)); // never darker for fewer flights
  assert.equal(shadeLevel(7, 3), 4); // a count above the given maximum cannot exceed the scale
});
