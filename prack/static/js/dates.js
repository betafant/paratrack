// Calendar days as "YYYY-MM-DD" strings, the local day of a flight (the API's `date`). Pure functions: day arithmetic
// is done in UTC so it never depends on the browser's time zone; only `todayIn` looks at a time zone, the region's.

const DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

export function isDay(text) {
  const m = DAY.exec(text ?? '');
  if (!m) return false;
  const [y, mo, d] = m.slice(1).map(Number);
  const t = new Date(Date.UTC(y, mo - 1, d));
  return y >= 2000 && y <= 2100 && t.getUTCFullYear() === y && t.getUTCMonth() === mo - 1 && t.getUTCDate() === d;
}

const toDate = (day) => {
  const [y, m, d] = day.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d));
};

const pad = (n, width = 2) => String(n).padStart(width, '0');

export const fromParts = (year, month, day) => `${pad(year, 4)}-${pad(month + 1)}-${pad(day)}`; // month: 0-11

export function addDays(day, n) {
  const t = toDate(day);
  t.setUTCDate(t.getUTCDate() + n);
  return fromParts(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate());
}

export const compareDays = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

export function partsOf(day) {
  const [year, month, d] = day.split('-').map(Number);
  return { year, month: month - 1, day: d };
}

export function addMonths(year, month, n) {
  const index = year * 12 + month + n;
  return { year: Math.floor(index / 12), month: ((index % 12) + 12) % 12 };
}

export const daysInMonth = (year, month) => new Date(Date.UTC(year, month + 1, 0)).getUTCDate();

/** First and last day of a month, for the `days` request that shades the calendar. */
export const monthRange = (year, month) => [fromParts(year, month, 1), fromParts(year, month, daysInMonth(year, month))];

/** Weeks of a month, Monday first: arrays of seven entries, a day string or null for the blanks around it. */
export function monthGrid(year, month) {
  const lead = (new Date(Date.UTC(year, month, 1)).getUTCDay() + 6) % 7; // Monday = 0
  const cells = [];
  for (let i = 0; i < lead; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth(year, month); d++) cells.push(fromParts(year, month, d));
  while (cells.length % 7) cells.push(null);
  const weeks = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
  return weeks;
}

/** Today's date in a time zone (an IANA name); the browser's own zone when the name is not known. */
export function todayIn(timeZone, now = new Date()) {
  let f;
  try {
    f = new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' });
  } catch {
    f = new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit' });
  }
  return f.format(now); // en-CA writes 2026-07-15
}

const labels = new Map();
function formatter(key, options) {
  if (!labels.has(key)) labels.set(key, new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', ...options }));
  return labels.get(key);
}

/** "Wed 15 Jul", or with `long` "Wednesday 15 July 2026". */
export function formatDay(day, { long = false } = {}) {
  const f = long
    ? formatter('long', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })
    : formatter('short', { weekday: 'short', day: 'numeric', month: 'short' });
  return f.format(toDate(day)).replace(',', '');
}

export const monthTitle = (year, month) =>
  formatter('month', { month: 'long', year: 'numeric' }).format(new Date(Date.UTC(year, month, 1)));

/** Monday ... Sunday, abbreviated. */
export const weekdayNames = () =>
  Array.from({ length: 7 }, (_, i) => formatter('weekday', { weekday: 'short' }).format(new Date(Date.UTC(2024, 0, 1 + i))));

/** 0 for no flights, 1 to 4 for more and more: square-root scaled against the busiest day shown. */
export function shadeLevel(count, max) {
  if (!count || count <= 0) return 0;
  return Math.min(4, 1 + Math.floor(3.999 * Math.sqrt(count / Math.max(max, count))));
}
