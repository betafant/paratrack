// The small floating controls: top bar, the card of the selected paraglider or flight, the strip of a day's flights,
// a hint and a toast. Built here (not in the HTML) so every string comes from strings.js.

import { h, icon } from './dom.js';
import { S } from './strings.js';

export function createUi(host, handlers) {
  // ---- top bar
  const dot = h('span', { class: 'dot', role: 'img', 'data-level': 'warn' });

  const radio = (value, text) => h('button', { type: 'button', role: 'radio', 'aria-checked': 'false', 'data-value': value, text });
  const segGroup = (label, buttons, onChoose) => {
    const el = h('div', { class: 'seg', role: 'radiogroup', 'aria-label': label }, ...buttons);
    buttons.forEach((button) => {
      button.addEventListener('click', () => onChoose(button.dataset.value));
      button.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
        event.preventDefault();
        const other = buttons.find((b) => b !== button);
        if (!other.disabled) {
          onChoose(other.dataset.value);
          other.focus();
        }
      });
    });
    return el;
  };

  const modeButtons = [radio('live', S.mode.live), radio('history', S.mode.history)];
  const segMode = segGroup(S.mode.label, modeButtons, (value) => handlers.onMode?.(value));

  const dateText = h('span', { class: 'label date' });
  const calendarButton = h('button', { type: 'button', class: 'btn date-btn' }, icon('calendar'), dateText);
  const prevDay = h('button', { type: 'button', class: 'btn icon-only', 'aria-label': S.day.prev }, icon('left'));
  const nextDay = h('button', { type: 'button', class: 'btn icon-only', 'aria-label': S.day.next }, icon('right'));
  const todayButton = h('button', { type: 'button', class: 'btn today-btn', text: S.day.today });
  const dayNav = h('div', { class: 'daynav' }, prevDay, nextDay, todayButton);

  const viewButtons = [radio('2d', S.view.flat), radio('3d', S.view.terrain)];
  const segView = segGroup(S.view.label, viewButtons, (value) => handlers.onView?.(value));

  const groundCount = h('span', { class: 'count' });
  const groundChip = h('button', { type: 'button', class: 'chip', 'aria-pressed': 'false', title: S.ground.title }, h('span', { text: S.ground.label }), groundCount);

  const baseName = h('span', { class: 'label' });
  const baseButton = h('button', { type: 'button', class: 'btn', 'aria-label': S.base.label }, icon('layers'), baseName);
  const themeButton = h('button', { type: 'button', class: 'btn icon-only' });

  const bar = h(
    'header',
    { class: 'bar glass' },
    h('div', { class: 'brand' }, dot, h('span', { class: 'word', text: S.app })),
    segMode,
    calendarButton,
    dayNav,
    segView,
    h('div', { class: 'group tools' }, groundChip, baseButton, themeButton),
  );

  // ---- bottom: the count, and in History the strip of the day's flights
  const count = h('span', { class: 'count', id: 'air-count' });
  const status = h('div', { class: 'status glass', role: 'status' }, count);
  const strip = h('div', { class: 'flights glass', role: 'group', 'aria-label': S.flights.list, hidden: true });
  const dock = h('div', { class: 'dock' }, status, strip);

  // ---- the card of the selected paraglider or flight
  const name = h('h2', { class: 'name' });
  const source = h('span', { class: 'tag' });
  const who = h('span', { class: 'who' });
  const closeButton = h('button', { type: 'button', class: 'btn icon-only close', 'aria-label': S.card.close }, icon('close'));
  const primary = h('dl', { class: 'grid' });
  const secondary = h('dl', { class: 'grid second' });
  const follow = h('button', { type: 'button', class: 'toggle', 'aria-pressed': 'false', title: S.card.followHint }, h('span', { class: 'knob' }), h('span', { text: S.card.follow }));
  const fit = h('button', { type: 'button', class: 'btn icon-only', 'aria-label': S.card.fitTrack }, icon('fit'));
  const note = h('p', { class: 'note' });
  const card = h(
    'aside',
    { class: 'card glass', hidden: true, 'aria-label': S.card.title },
    h('div', { class: 'head' }, h('div', { class: 'titles' }, name, h('p', { class: 'sub' }, source, who)), closeButton),
    primary,
    secondary,
    note,
    h('div', { class: 'foot' }, follow, fit),
  );
  const hint = h('div', { class: 'hint glass', hidden: true });
  const toastBox = h('div', { class: 'toast glass', role: 'status', hidden: true });

  host.append(bar, dock, card, hint, toastBox);
  // The card sits under the bar, which wraps onto a second row on narrow screens.
  const fitBar = () => document.documentElement.style.setProperty('--bar-h', `${bar.offsetHeight}px`);
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(fitBar).observe(bar);
  fitBar();

  // ---- behaviour
  groundChip.addEventListener('click', () => handlers.onGround?.(groundChip.getAttribute('aria-pressed') !== 'true'));
  baseButton.addEventListener('click', () => handlers.onBase?.());
  themeButton.addEventListener('click', () => handlers.onTheme?.());
  closeButton.addEventListener('click', () => handlers.onClose?.());
  follow.addEventListener('click', () => handlers.onFollow?.(follow.getAttribute('aria-pressed') !== 'true'));
  fit.addEventListener('click', () => handlers.onFit?.());
  prevDay.addEventListener('click', () => handlers.onDay?.('prev'));
  nextDay.addEventListener('click', () => handlers.onDay?.('next'));
  todayButton.addEventListener('click', () => handlers.onDay?.('today'));
  strip.addEventListener('click', (event) => {
    const chip = event.target.closest('button[data-id]');
    if (chip) handlers.onPickFlight?.(chip.dataset.id);
  });

  /** A grid of label/value cells. The cells are rebuilt only when the set of cells changes, otherwise updated in place. */
  function renderGrid(dl, cells) {
    dl.hidden = cells.length === 0;
    const signature = cells.map((c) => c.key).join('|');
    if (dl.dataset.signature !== signature) {
      dl.dataset.signature = signature;
      dl.replaceChildren(
        ...cells.map((c) => h('div', { class: 'cell', 'data-cell': c.key }, h('dt', { text: c.label }), h('dd', { 'data-k': c.key }))),
      );
    }
    for (const c of cells) {
      const dd = dl.querySelector(`[data-k="${c.key}"]`);
      if (dd.textContent !== c.value) dd.textContent = c.value;
      dd.dataset.sign = c.sign ?? '';
    }
  }

  const setChecked = (buttons, value) => {
    for (const b of buttons) {
      const on = b.dataset.value === value;
      b.setAttribute('aria-checked', String(on));
      b.tabIndex = on ? 0 : -1;
    }
  };

  let toastTimer = null;
  let stripSignature = '';
  return {
    root: host,
    calendarButton,

    setTheme(theme) {
      const next = theme === 'dark' ? 'toLight' : 'toDark';
      themeButton.replaceChildren(icon(theme === 'dark' ? 'sun' : 'moon'));
      themeButton.setAttribute('aria-label', S.theme[next]);
      themeButton.title = S.theme[next];
    },
    setView(view, { terrain }) {
      setChecked(viewButtons, view);
      viewButtons[1].disabled = !terrain;
      viewButtons[1].title = terrain ? '' : S.view.noTerrain;
    },
    /** 'live' or 'history': what the date controls and the Ground chip show. */
    setMode(mode) {
      setChecked(modeButtons, mode);
      dayNav.hidden = mode !== 'history';
      groundChip.hidden = mode !== 'live';
      document.body.dataset.mode = mode;
    },
    /** The day shown on the calendar button; `canNext`: there is a later day to go to. */
    setDay({ text, isToday, canNext }) {
      dateText.textContent = text;
      calendarButton.setAttribute('aria-label', S.day.open(text));
      nextDay.disabled = !canNext;
      todayButton.disabled = isToday;
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
    setCount(text) {
      if (count.textContent !== text) count.textContent = text; // a status region: only announce real changes
    },
    /** The strip of a day's flights: [{ id, name, time }]; null hides it (Live). */
    setFlights(items, selectedId = null) {
      strip.hidden = items === null;
      if (items === null) return;
      const signature = items.map((i) => `${i.id}:${i.name}:${i.time}`).join('|');
      if (signature !== stripSignature) {
        stripSignature = signature;
        strip.replaceChildren(
          ...items.map((i) =>
            h('button', { type: 'button', class: 'flight', 'data-id': String(i.id), 'aria-pressed': 'false' }, h('span', { class: 'who', text: i.name }), h('span', { class: 'when', text: i.time })),
          ),
        );
      }
      for (const chip of strip.children) chip.setAttribute('aria-pressed', String(chip.dataset.id === String(selectedId)));
    },
    scrollToFlight(id) {
      strip.querySelector(`button[data-id="${id}"]`)?.scrollIntoView({ block: 'nearest', inline: 'center' });
    },

    showCard() {
      card.hidden = false;
    },
    hideCard() {
      card.hidden = true;
    },
    cardVisible: () => !card.hidden,
    /**
     * model: { name, source, who, note, grids: [cells, cells], follow: { show, pressed, enabled }, canFit }
     * with cells = [{ key, label, value, sign }].
     */
    updateCard(m) {
      name.textContent = m.name;
      source.textContent = m.source ?? '';
      source.hidden = !m.source;
      who.textContent = m.who ?? '';
      who.hidden = !m.who;
      renderGrid(primary, m.grids[0] ?? []);
      renderGrid(secondary, m.grids[1] ?? []);
      note.textContent = m.note ?? '';
      note.hidden = !m.note;
      follow.hidden = !m.follow.show;
      follow.setAttribute('aria-pressed', String(m.follow.pressed));
      follow.disabled = !m.follow.enabled;
      fit.disabled = !m.canFit;
    },

    /** A short label that follows the pointer (the flight under it), or null to remove it. */
    hint(text, x = 0, y = 0) {
      if (!text) {
        hint.hidden = true;
        return;
      }
      hint.textContent = text;
      hint.hidden = false;
      hint.style.transform = `translate(${Math.round(x + 14)}px, ${Math.round(y + 14)}px)`;
    },
    toast(text, ms = 3500) {
      toastBox.textContent = text;
      toastBox.hidden = false;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => {
        toastBox.hidden = true;
      }, ms);
    },
  };
}
