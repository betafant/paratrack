// Number and time formatting: metres, km/h, m/s, region local time. Pure functions, no DOM.

import { S } from './strings.js';

const THIN = ' '; // narrow no-break space: "1 587"
const MINUS = '−'; // a real minus sign, same width as the plus

export function thousands(n) {
  const s = String(Math.abs(Math.round(n)));
  return (n < 0 ? MINUS : '') + s.replace(/\B(?=(\d{3})+(?!\d))/g, THIN);
}

export const fmtAlt = (m) => (m == null ? '–' : `${thousands(m)}${THIN}${S.units.m}`);

export function fmtVario(v) {
  if (v == null || Number.isNaN(v)) return '–';
  const rounded = Math.round(v * 10) / 10;
  if (rounded === 0) return '0.0';
  return (rounded > 0 ? '+' : MINUS) + Math.abs(rounded).toFixed(1);
}

export const fmtSpeed = (kmh) => (kmh == null ? '–' : `${Math.round(kmh)}${THIN}${S.units.kmh}`);

export function fmtHeading(deg) {
  if (deg == null) return '–';
  const names = S.compass;
  const point = names[Math.round((((deg % 360) + 360) % 360) / 45) % 8];
  return `${Math.round(deg)}°${THIN}${point}`;
}

export function fmtDuration(seconds) {
  if (seconds == null || seconds < 0) return '–';
  const s = Math.round(seconds);
  if (s < 60) return `${s}${THIN}${S.units.s}`;
  const minutes = Math.floor(s / 60);
  if (minutes < 60) return `${minutes}${THIN}${S.units.min}`;
  return `${Math.floor(minutes / 60)}${THIN}${S.units.h} ${String(minutes % 60).padStart(2, '0')}${THIN}${S.units.min}`;
}

export function fmtDistance(metres) {
  if (metres == null) return '–';
  if (metres < 1000) return `${Math.round(metres)}${THIN}${S.units.m}`;
  return `${(metres / 1000).toFixed(metres < 100000 ? 1 : 0)}${THIN}${S.units.km}`;
}

/** A time formatter for one IANA zone ("Europe/Zurich"): epoch seconds in, "11:42" out. */
export function timeFormatter(timeZone) {
  let f;
  try {
    f = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone });
  } catch {
    f = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  }
  return (epoch) => (epoch == null ? '–' : f.format(new Date(epoch * 1000)));
}

/** The one-line map label: "Mia · 1 587 m · −1.9". */
export function labelText(aircraft, level) {
  if (level === 'name') return aircraft.name;
  return `${aircraft.name} · ${fmtAlt(aircraft.alt)} · ${fmtVario(aircraft.vs)}`;
}

/** Great-circle distance in metres between two points given in degrees. */
export function haversine(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad;
  const dLon = (lon2 - lon1) * rad;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 12742000 * Math.asin(Math.min(1, Math.sqrt(a)));
}
