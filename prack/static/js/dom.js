// Tiny DOM helpers shared by the controls: an element builder and the icons (inline SVG, no image files).

const SVG = 'http://www.w3.org/2000/svg';

const ICONS = {
  layers: { d: 'M12 3 3 8l9 5 9-5-9-5Zm-7.6 9.2L3 13l9 5 9-5-1.4-.8L12 16 4.4 12.2Zm0 4L3 17l9 5 9-5-1.4-.8L12 20l-7.6-3.8Z', fill: true },
  moon: { d: 'M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11Z', fill: true },
  sun: { d: 'M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10Zm0-5h0v3m0 14v3M4.2 4.2l2.1 2.1m11.4 11.4 2.1 2.1M2 12h3m14 0h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1' },
  close: { d: 'M6 6l12 12M18 6 6 18' },
  fit: { d: 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5' },
  calendar: { d: 'M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm-1 5h16M8 3v4m8-4v4' },
  left: { d: 'm14.5 6-6 6 6 6' },
  right: { d: 'm9.5 6 6 6-6 6' },
};

export function icon(name, size = 20) {
  const { d, fill } = ICONS[name];
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const path = document.createElementNS(SVG, 'path');
  path.setAttribute('d', d);
  if (fill) {
    path.setAttribute('fill', 'currentColor');
  } else {
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '1.8');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
  }
  svg.append(path);
  return svg;
}

/** h('button', { class: 'btn', type: 'button', text: 'OK' }, child, ...): attributes, text and children. */
export function h(tag, attrs = {}, ...children) {
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
