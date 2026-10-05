// The small floating controls: top bar, the card of the selected paraglider, a toast. Built here (not in the HTML)
// so every string comes from strings.js.

import { S } from './strings.js';

const SVG = 'http://www.w3.org/2000/svg';

const ICONS = {
  layers: 'M12 3 3 8l9 5 9-5-9-5Zm-7.6 9.2L3 13l9 5 9-5-1.4-.8L12 16 4.4 12.2Zm0 4L3 17l9 5 9-5-1.4-.8L12 20l-7.6-3.8Z',
  sun: 'M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10Zm0-5h0v3m0 14v3M4.2 4.2l2.1 2.1m11.4 11.4 2.1 2.1M2 12h3m14 0h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1',
  moon: 'M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11Z',
  close: 'M6 6l12 12M18 6 6 18',
  fit: 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5',
};

function icon(name, { stroke = true } = {}) {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '20');
  svg.setAttribute('height', '20');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const path = document.createElementNS(SVG, 'path');
  path.setAttribute('d', ICONS[name]);
  if (stroke && name !== 'layers' && name !== 'moon') {
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '1.8');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
  } else {
    path.setAttribute('fill', 'currentColor');
  }
  svg.append(path);
  return svg;
}

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children) if (child) el.append(child);
  return el;
}

const STATS = [
  ['alt', 'altitude'],
  ['agl', 'agl'],
  ['spd', 'speed'],
  ['vs', 'vario'],
  ['hdg', 'heading'],
];
const FLIGHT_STATS = [
  ['takeoff', 'takeoff'],
  ['duration', 'duration'],
  ['distance', 'distance'],
];

export function createUi(host, handlers) {
  const dot = h('span', { class: 'dot', role: 'img', 'data-level': 'warn' });
  const count = h('span', { class: 'count', id: 'air-count' });

  const viewButtons = ['2d', '3d'].map((view) =>
    h('button', { type: 'button', role: 'radio', 'aria-checked': 'false', 'data-view': view, text: view === '2d' ? S.view.flat : S.view.terrain }),
  );
  const segView = h('div', { class: 'seg', role: 'radiogroup', 'aria-label': S.view.label }, ...viewButtons);

  const groundCount = h('span', { class: 'count' });
  const groundChip = h('button', { type: 'button', class: 'chip', 'aria-pressed': 'false', title: S.ground.title }, h('span', { text: S.ground.label }), groundCount);

  const baseName = h('span', { class: 'label' });
  const baseButton = h('button', { type: 'button', class: 'btn', 'aria-label': S.base.label }, icon('layers'), baseName);

  const themeButton = h('button', { type: 'button', class: 'btn icon-only' });

  const bar = h(
    'header',
    { class: 'bar glass' },
    h('div', { class: 'brand' }, dot, h('span', { class: 'word', text: S.app })),
    segView,
    groundChip,
    baseButton,
    themeButton,
  );
  const status = h('div', { class: 'status glass', role: 'status' }, count);

  // ---- the card of the selected paraglider
  const values = {};
  const cell = (key, label) => {
    const dd = h('dd', { 'data-k': key });
    values[key] = dd;
    return h('div', { class: 'cell', 'data-cell': key }, h('dt', { text: label }), dd);
  };
  const name = h('h2', { class: 'name' });
  const source = h('span', { class: 'tag' });
  const who = h('span', { class: 'who' });
  const closeButton = h('button', { type: 'button', class: 'btn icon-only close', 'aria-label': S.card.close }, icon('close'));
  const follow = h('button', { type: 'button', class: 'toggle', 'aria-pressed': 'false', title: S.card.followHint }, h('span', { class: 'knob' }), h('span', { text: S.card.follow }));
  const fit = h('button', { type: 'button', class: 'btn icon-only', 'aria-label': S.card.fitTrack }, icon('fit'));
  const note = h('p', { class: 'note' });
  const card = h(
    'aside',
    { class: 'card glass', hidden: true, 'aria-label': S.card.title },
    h('div', { class: 'head' }, h('div', { class: 'titles' }, name, h('p', { class: 'sub' }, source, who)), closeButton),
    h('dl', { class: 'grid' }, ...STATS.map(([k, label]) => cell(k, S.card[label]))),
    h('dl', { class: 'grid flight' }, ...FLIGHT_STATS.map(([k, label]) => cell(k, S.card[label]))),
    note,
    h('div', { class: 'foot' }, follow, fit),
  );
  const toastBox = h('div', { class: 'toast glass', role: 'status', hidden: true });

  host.append(bar, status, card, toastBox);

  // ---- behaviour
  viewButtons.forEach((button) => {
    button.addEventListener('click', () => handlers.onView?.(button.dataset.view));
    button.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
      event.preventDefault();
      const other = viewButtons.find((b) => b !== button);
      if (!other.disabled) {
        handlers.onView?.(other.dataset.view);
        other.focus();
      }
    });
  });
  groundChip.addEventListener('click', () => handlers.onGround?.(groundChip.getAttribute('aria-pressed') !== 'true'));
  baseButton.addEventListener('click', () => handlers.onBase?.());
  themeButton.addEventListener('click', () => handlers.onTheme?.());
  closeButton.addEventListener('click', () => handlers.onClose?.());
  follow.addEventListener('click', () => handlers.onFollow?.(follow.getAttribute('aria-pressed') !== 'true'));
  fit.addEventListener('click', () => handlers.onFit?.());

  let toastTimer = null;
  const api = {
    setTheme(theme) {
      const next = theme === 'dark' ? 'toLight' : 'toDark';
      themeButton.replaceChildren(icon(theme === 'dark' ? 'sun' : 'moon'));
      themeButton.setAttribute('aria-label', S.theme[next]);
      themeButton.title = S.theme[next];
    },
    setView(view, { terrain }) {
      for (const b of viewButtons) {
        const on = b.dataset.view === view;
        b.setAttribute('aria-checked', String(on));
        b.tabIndex = on ? 0 : -1;
      }
      const b3 = viewButtons[1];
      b3.disabled = !terrain;
      b3.title = terrain ? '' : S.view.noTerrain;
    },
    setGround(on, n) {
      groundChip.setAttribute('aria-pressed', String(on));
      groundCount.textContent = String(n);
      groundCount.hidden = n === 0;
    },
    setBase(text) {
      baseName.textContent = text;
      baseButton.title = S.base.cycle(text);
      baseButton.setAttribute('aria-label', S.base.cycle(text));
    },
    setLink(level, text) {
      dot.dataset.level = level;
      dot.setAttribute('aria-label', text);
      dot.title = text;
    },
    setCount(n) {
      const text = n === 0 ? S.empty : S.count(n);
      if (count.textContent !== text) count.textContent = text; // a status region: only announce real changes
    },
    showCard() {
      card.hidden = false;
    },
    hideCard() {
      card.hidden = true;
    },
    cardVisible: () => !card.hidden,
    updateCard(m) {
      name.textContent = m.name;
      source.textContent = m.source ?? '';
      source.hidden = !m.source;
      who.textContent = m.who ?? '';
      who.hidden = !m.who;
      for (const [key] of [...STATS, ...FLIGHT_STATS]) {
        if (key in m.values) values[key].textContent = m.values[key];
      }
      values.vs.dataset.sign = m.vsSign ?? '';
      for (const key of ['agl', 'spd', 'vs', 'hdg']) card.querySelector(`[data-cell="${key}"]`).hidden = !m.flying && key !== 'agl';
      card.querySelector('.grid.flight').hidden = !m.flying;
      card.classList.toggle('grounded', !m.flying);
      note.textContent = m.note ?? '';
      note.hidden = !m.note;
      follow.setAttribute('aria-pressed', String(m.follow));
      follow.disabled = !m.live;
      fit.disabled = !m.canFit;
    },
    toast(text, ms = 3500) {
      toastBox.textContent = text;
      toastBox.hidden = false;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => {
        toastBox.hidden = true;
      }, ms);
    },
    focusCard() {
      closeButton.focus();
    },
  };
  return api;
}
