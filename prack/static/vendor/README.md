# Vendored libraries

Pinned and served locally, so the app needs no CDN and no Node to run. Do not upgrade casually:
MapLibre GL JS 6 removed `map.transform`, which deck.gl 9.4 reads.

| File | Package | Version | Licence |
|---|---|---|---|
| `maplibre-gl.js`, `maplibre-gl.css` | [maplibre-gl](https://www.npmjs.com/package/maplibre-gl) `dist/` | 5.24.0 | BSD-3-Clause (`LICENSE-maplibre-gl.txt`) |
| `deck.gl.min.js` | [deck.gl](https://www.npmjs.com/package/deck.gl) `dist.min.js` (global `deck`) | 9.4.0 | MIT (`LICENSE-deck.gl.txt`) |

To refresh: `npm pack maplibre-gl@5.24.0 deck.gl@9.4.0`, unpack the tarballs and copy the files named above.
