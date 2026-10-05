// Which labels fit on the screen. Greedy: the most important label first, and a label is only shown when it covers
// neither a label already placed nor another aircraft's marker. Pure geometry, so it runs under `node --test` too.

const CELL = 64; // px, the size of the grid cells used to find neighbours quickly

class Grid {
  constructor() {
    this.cells = new Map();
  }

  _keys(box) {
    const keys = [];
    for (let cx = Math.floor(box.x0 / CELL); cx <= Math.floor(box.x1 / CELL); cx++) {
      for (let cy = Math.floor(box.y0 / CELL); cy <= Math.floor(box.y1 / CELL); cy++) keys.push(`${cx}:${cy}`);
    }
    return keys;
  }

  add(box) {
    for (const k of this._keys(box)) {
      if (!this.cells.has(k)) this.cells.set(k, []);
      this.cells.get(k).push(box);
    }
  }

  hits(box, owner) {
    for (const k of this._keys(box)) {
      for (const other of this.cells.get(k) ?? []) {
        if (other.owner === owner) continue;
        if (box.x0 < other.x1 && box.x1 > other.x0 && box.y0 < other.y1 && box.y1 > other.y0) return true;
      }
    }
    return false;
  }
}

/**
 * items: [{ key, x, y, height, priority, variants: [{ id, width }, ...] }] with screen pixels. A label may come in
 * several widths (the full text, then the name alone): the first one that fits on one side is taken.
 * Returns Map key -> { side: 'right' | 'left', variant: id } for the labels that fit; the others are absent.
 */
export function declutter(items, { width, height, gap = 14, markerRadius = 11, margin = 8 } = {}) {
  const grid = new Grid();
  const visible = items.filter((i) => i.x > -50 && i.y > -50 && i.x < width + 50 && i.y < height + 50);
  for (const i of visible) {
    grid.add({ x0: i.x - markerRadius, y0: i.y - markerRadius, x1: i.x + markerRadius, y1: i.y + markerRadius, owner: i.key });
  }
  const placed = new Map();
  for (const item of [...visible].sort((a, b) => b.priority - a.priority)) {
    search: for (const variant of item.variants ?? []) {
      for (const side of ['right', 'left']) {
        const x0 = side === 'right' ? item.x + gap : item.x - gap - variant.width;
        const box = { x0, y0: item.y - item.height / 2, x1: x0 + variant.width, y1: item.y + item.height / 2, owner: item.key };
        const inside = box.x0 >= margin && box.x1 <= width - margin && box.y0 >= margin && box.y1 <= height - margin;
        if (!inside || grid.hits(box, item.key)) continue;
        grid.add(box);
        placed.set(item.key, { side, variant: variant.id });
        break search;
      }
    }
  }
  return placed;
}
