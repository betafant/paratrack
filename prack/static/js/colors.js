// Colours for the map layers (deck.gl takes [r, g, b, a] in 0..255). The CSS has its own copy of the accent.

export const ACCENT = { dark: [139, 124, 255], light: [91, 75, 255] };

// Altitude ramp for tracks, metres above sea level: low and cool, high and warm.
const RAMP = [
  [400, [45, 212, 191]],
  [1100, [74, 222, 128]],
  [1800, [250, 204, 21]],
  [2600, [251, 146, 60]],
  [3400, [244, 63, 94]],
  [4200, [192, 132, 252]],
];

export function altitudeColor(alt, alpha = 255) {
  if (alt == null || Number.isNaN(alt)) return [150, 150, 160, alpha];
  if (alt <= RAMP[0][0]) return [...RAMP[0][1], alpha];
  for (let i = 1; i < RAMP.length; i++) {
    const [a1, c1] = RAMP[i];
    if (alt <= a1) {
      const [a0, c0] = RAMP[i - 1];
      const k = (alt - a0) / (a1 - a0);
      return [
        Math.round(c0[0] + (c1[0] - c0[0]) * k),
        Math.round(c0[1] + (c1[1] - c0[1]) * k),
        Math.round(c0[2] + (c1[2] - c0[2]) * k),
        alpha,
      ];
    }
  }
  return [...RAMP[RAMP.length - 1][1], alpha];
}

export const palette = (theme) => ({
  accent: ACCENT[theme] ?? ACCENT.dark,
  ground: theme === 'light' ? [110, 114, 125] : [160, 164, 175],
  halo: theme === 'light' ? [20, 22, 30] : [255, 255, 255],
  labelText: theme === 'light' ? [20, 22, 30, 255] : [245, 246, 250, 255],
  labelBackground: theme === 'light' ? [255, 255, 255, 215] : [22, 23, 28, 200],
  labelBorder: theme === 'light' ? [0, 0, 0, 30] : [255, 255, 255, 38],
  curtain: theme === 'light' ? [20, 22, 30, 38] : [255, 255, 255, 34],
});
