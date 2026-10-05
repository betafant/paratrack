// The calendar popover: a month grid in which every day is shaded by how many flights it has. Keyboard: arrows move by
// day and week, Home/End go to the start/end of the week, Page Up/Down by month, Escape closes.

import {
  addDays, addMonths, compareDays, daysInMonth, formatDay, fromParts, monthGrid, monthRange, monthTitle, partsOf,
  shadeLevel, weekdayNames,
} from './dates.js';
import { h, icon } from './dom.js';
import { S } from './strings.js';

/**
 * host: where to put the popover; anchor: the button that opens it; loadDays(first, last) -> [{date, total}];
 * onPick(day); today(): the current day in the region's time zone.
 */
export function createCalendar({ host, anchor, loadDays, onPick, today }) {
  let selected = null;
  let view = { year: 2026, month: 0 };
  let focused = null;
  let counts = new Map();
  let token = 0;
  let isOpen = false;

  const title = h('div', { class: 'cal-title', 'aria-live': 'polite' });
  const prev = h('button', { type: 'button', class: 'btn icon-only', 'aria-label': S.calendar.prevMonth }, icon('left'));
  const next = h('button', { type: 'button', class: 'btn icon-only', 'aria-label': S.calendar.nextMonth }, icon('right'));
  const weekdays = h('div', { class: 'cal-week', 'aria-hidden': 'true' }, ...weekdayNames().map((name) => h('span', { text: name })));
  const grid = h('div', { class: 'cal-grid', role: 'grid', 'aria-label': S.calendar.title });
  const root = h(
    'div',
    { class: 'cal glass', role: 'dialog', 'aria-label': S.calendar.title, hidden: true },
    h('div', { class: 'cal-head' }, prev, title, next),
    weekdays,
    grid,
  );
  host.append(root);

  const buttons = () => [...grid.querySelectorAll('button[data-day]')];
  const button = (day) => grid.querySelector(`button[data-day="${day}"]`);

  function label(day) {
    const n = counts.get(day) ?? 0;
    return `${formatDay(day, { long: true })}, ${n ? S.calendar.flights(n) : S.calendar.none}`;
  }

  /** Shading and labels, in place: a button that has the focus must stay the same element. */
  function paint() {
    const [first, last] = monthRange(view.year, view.month);
    let max = 0;
    for (const [day, n] of counts) if (day >= first && day <= last) max = Math.max(max, n);
    const now = today();
    for (const b of buttons()) {
      const day = b.dataset.day;
      b.dataset.level = String(shadeLevel(counts.get(day) ?? 0, max));
      b.setAttribute('aria-label', label(day));
      b.title = counts.has(day) && counts.get(day) ? S.calendar.flights(counts.get(day)) : '';
      b.disabled = compareDays(day, now) > 0;
      if (day === now) b.setAttribute('aria-current', 'date');
      else b.removeAttribute('aria-current');
      b.setAttribute('aria-pressed', String(day === selected));
      b.tabIndex = day === focused ? 0 : -1;
    }
    title.textContent = monthTitle(view.year, view.month);
    const nowParts = partsOf(now);
    next.disabled = view.year * 12 + view.month >= nowParts.year * 12 + nowParts.month;
  }

  function build() {
    grid.replaceChildren();
    for (const week of monthGrid(view.year, view.month)) {
      const row = h('div', { class: 'cal-row', role: 'row' });
      for (const day of week) {
        row.append(
          day === null
            ? h('span', { class: 'cal-blank', role: 'gridcell' })
            : h('button', { type: 'button', role: 'gridcell', class: 'cal-day', 'data-day': day, text: String(partsOf(day).day) }),
        );
      }
      grid.append(row);
    }
    paint();
  }

  async function loadMonth() {
    const mine = ++token;
    const [first, last] = monthRange(view.year, view.month);
    try {
      const rows = await loadDays(first, last);
      if (mine !== token) return;
      for (const [day] of counts) if (day >= first && day <= last) counts.delete(day);
      for (const row of rows) counts.set(row.date, row.total);
      paint();
    } catch {
      /* no shading, but the days can still be picked */
    }
  }

  function showMonth(year, month) {
    view = { year, month };
    build();
    loadMonth();
  }

  function focusDay(day) {
    const now = today();
    if (compareDays(day, now) > 0) day = now;
    const { year, month } = partsOf(day);
    if (year !== view.year || month !== view.month) showMonth(year, month);
    focused = day;
    paint();
    button(day)?.focus();
  }

  function open(day) {
    selected = day;
    const { year, month } = partsOf(day);
    focused = day;
    isOpen = true;
    root.hidden = false;
    anchor.setAttribute('aria-expanded', 'true');
    const r = anchor.getBoundingClientRect();
    const below = (anchor.closest('.bar') ?? anchor).getBoundingClientRect().bottom; // under the whole bar, not just the button
    root.style.top = `${Math.round(below + 8)}px`;
    root.style.left = `${Math.round(Math.max(12, Math.min(r.left, window.innerWidth - root.offsetWidth - 12)))}px`;
    showMonth(year, month);
    button(day)?.focus();
  }

  function close({ restoreFocus = false } = {}) {
    if (!isOpen) return;
    isOpen = false;
    root.hidden = true;
    anchor.setAttribute('aria-expanded', 'false');
    token += 1;
    if (restoreFocus) anchor.focus();
  }

  anchor.setAttribute('aria-haspopup', 'dialog');
  anchor.setAttribute('aria-expanded', 'false');
  anchor.addEventListener('click', () => (isOpen ? close() : open(selected ?? today())));
  prev.addEventListener('click', () => {
    const m = addMonths(view.year, view.month, -1);
    focused = fromParts(m.year, m.month, 1);
    showMonth(m.year, m.month);
  });
  next.addEventListener('click', () => {
    const m = addMonths(view.year, view.month, 1);
    focused = fromParts(m.year, m.month, 1);
    showMonth(m.year, m.month);
  });
  grid.addEventListener('click', (event) => {
    const b = event.target.closest('button[data-day]');
    if (!b || b.disabled) return;
    selected = b.dataset.day;
    close({ restoreFocus: true });
    onPick(selected);
  });
  /** The same day of the month before or after (the last day when that month is shorter). */
  function shiftMonth(day, by) {
    const p = partsOf(day);
    const m = addMonths(p.year, p.month, by);
    return fromParts(m.year, m.month, Math.min(p.day, daysInMonth(m.year, m.month)));
  }

  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      close({ restoreFocus: true });
      return;
    }
    const b = event.target.closest?.('button[data-day]');
    if (!b) return;
    const day = b.dataset.day;
    const weekday = (new Date(`${day}T00:00:00Z`).getUTCDay() + 6) % 7; // Monday = 0
    const moves = {
      ArrowLeft: () => addDays(day, -1),
      ArrowRight: () => addDays(day, 1),
      ArrowUp: () => addDays(day, -7),
      ArrowDown: () => addDays(day, 7),
      Home: () => addDays(day, -weekday),
      End: () => addDays(day, 6 - weekday),
      PageUp: () => shiftMonth(day, -1),
      PageDown: () => shiftMonth(day, 1),
    };
    if (moves[event.key]) {
      event.preventDefault();
      focusDay(moves[event.key]());
    }
  });
  document.addEventListener('pointerdown', (event) => {
    if (isOpen && !root.contains(event.target) && !anchor.contains(event.target)) close();
  });
  window.addEventListener('resize', () => close());

  return {
    open,
    close,
    isOpen: () => isOpen,
    setSelected(day) {
      selected = day;
      if (isOpen) paint();
    },
  };
}
