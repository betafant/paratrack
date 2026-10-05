import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fmtAlt, fmtDistance, fmtDuration, fmtHeading, fmtSpeed, fmtVario, haversine, labelText, thousands, timeFormatter } from '../../prack/static/js/format.js';

const THIN = ' ';

test('altitudes get a thin space as thousands separator', () => {
  assert.equal(fmtAlt(1587), `1${THIN}587${THIN}m`);
  assert.equal(fmtAlt(412.4), `412${THIN}m`);
  assert.equal(fmtAlt(null), '–');
  assert.equal(thousands(-1234567), `−1${THIN}234${THIN}567`);
});

test('vario: one decimal, a real minus, a plus for climbing', () => {
  assert.equal(fmtVario(-1.94), '−1.9');
  assert.equal(fmtVario(0.8), '+0.8');
  assert.equal(fmtVario(0.02), '0.0');
  assert.equal(fmtVario(-0.02), '0.0');
  assert.equal(fmtVario(null), '–');
});

test('speed, heading, duration, distance', () => {
  assert.equal(fmtSpeed(37.6), `38${THIN}km/h`);
  assert.equal(fmtHeading(270), `270°${THIN}W`);
  assert.equal(fmtHeading(359), `359°${THIN}N`);
  assert.equal(fmtDuration(45), `45${THIN}s`);
  assert.equal(fmtDuration(12 * 60 + 5), `12${THIN}min`);
  assert.equal(fmtDuration(3600 + 23 * 60), `1${THIN}h 23${THIN}min`);
  assert.equal(fmtDuration(3600 + 5 * 60), `1${THIN}h 05${THIN}min`);
  assert.equal(fmtDistance(850), `850${THIN}m`);
  assert.equal(fmtDistance(12345), `12.3${THIN}km`);
  assert.equal(fmtDistance(123456), `123${THIN}km`);
});

test('the map label: name alone, or name, altitude and vario', () => {
  const a = { name: 'Mia', alt: 1587, vs: -1.9 };
  assert.equal(labelText(a, 'name'), 'Mia');
  assert.equal(labelText(a, 'full'), `Mia · 1${THIN}587${THIN}m · −1.9`);
});

test('times are shown in the region time zone', () => {
  const zurich = timeFormatter('Europe/Zurich');
  const utc = timeFormatter('UTC');
  const epoch = Date.UTC(2026, 6, 15, 9, 42) / 1000; // summer: Zurich is UTC+2
  assert.equal(zurich(epoch), '11:42');
  assert.equal(utc(epoch), '09:42');
  assert.equal(zurich(null), '–');
  assert.equal(timeFormatter('No/Such_Zone')(epoch).length, 5); // an unknown zone must not break the page
});

test('haversine', () => {
  assert.ok(Math.abs(haversine(46.8, 8.2, 46.8, 8.201) - 76.4) < 1);
  assert.equal(haversine(47, 8, 47, 8), 0);
});
