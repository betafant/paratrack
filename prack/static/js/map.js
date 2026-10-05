// The map: MapLibre GL JS (5.x) with deck.gl (9.4) drawing into the same WebGL context ("interleaved").
// maplibregl and deck are the globals of the vendored files.

import { absoluteUrl } from './api.js';

const PHOTO = /aerial|satellite|imagery|ortho|photo|image/i;
const MAX_3D_ZOOM = 12; // a track fitted in 3D must not end with the camera inside a mountain (zoom, see fitBounds)
const PITCH_3D = 60;
const PICK_RADIUS = 8; // thin lines and small markers are easier to hit

const skyFor = (theme) =>
  theme === 'light'
    ? { 'sky-color': '#9fc4ee', 'horizon-color': '#e9f1fa', 'fog-color': '#e9f1fa', 'sky-horizon-blend': 0.6, 'horizon-fog-blend': 0.6, 'fog-ground-blend': 0.35 }
    : { 'sky-color': '#0d1220', 'horizon-color': '#2a3350', 'fog-color': '#1b2236', 'sky-horizon-blend': 0.6, 'horizon-fog-blend': 0.6, 'fog-ground-blend': 0.35 };

/** How a base map is toned for a theme. In the dark theme map drawings are inverted (light lines on a dark ground). */
export function rasterPaint(theme, basemap) {
  if (theme !== 'dark') return {};
  if (PHOTO.test(`${basemap.id} ${basemap.name}`)) return { 'raster-brightness-max': 0.78, 'raster-contrast': 0.08 };
  return {
    'raster-brightness-min': 0.8,
    'raster-brightness-max': 0.07,
    'raster-hue-rotate': 180,
    'raster-saturation': 0.1,
    'raster-contrast': 0.05,
  };
}

const backgroundFor = (theme) => (theme === 'light' ? '#e8e9ec' : '#14151a');

export function buildStyle(config, theme, baseId) {
  const region = config.regions[0];
  const sources = {};
  const layers = [{ id: 'background', type: 'background', paint: { 'background-color': backgroundFor(theme) } }];
  for (const b of region.basemaps) {
    sources[`base-${b.id}`] = {
      type: 'raster',
      tiles: b.tiles,
      tileSize: 256,
      maxzoom: b.max_zoom ?? 18,
      attribution: b.attribution ?? '',
    };
    layers.push({
      id: `base-${b.id}`,
      type: 'raster',
      source: `base-${b.id}`,
      layout: { visibility: b.id === baseId ? 'visible' : 'none' },
      paint: rasterPaint(theme, b),
    });
  }
  if (config.terrain?.enabled) {
    sources.dem = {
      type: 'raster-dem',
      tiles: [absoluteUrl(config.terrain.url)],
      encoding: config.terrain.encoding ?? 'terrarium',
      tileSize: config.terrain.tile_size ?? 256,
      maxzoom: config.terrain.max_zoom ?? 12,
      ...(config.terrain.bounds ? { bounds: config.terrain.bounds } : {}), // no requests the server would refuse
    };
  }
  return { version: 8, sources, layers, sky: skyFor(theme) };
}

export class MapView {
  constructor({ container, config, theme, baseId, camera, reducedMotion, onClick, onHover }) {
    this.config = config;
    this.theme = theme;
    this.baseId = baseId;
    this.reducedMotion = reducedMotion;
    this.mode3d = false;
    this.terrain = Boolean(config.terrain?.enabled);
    const region = config.regions[0];

    this.map = new maplibregl.Map({
      container,
      style: buildStyle(config, theme, baseId),
      center: camera?.center ?? region.center,
      zoom: camera?.zoom ?? region.zoom,
      pitch: 0,
      maxPitch: 80,
      minZoom: 3,
      attributionControl: false,
      dragRotate: true,
      pitchWithRotate: true,
      canvasContextAttributes: { antialias: true },
    });
    this.map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');
    this.map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'bottom-right');

    this.overlay = new deck.MapboxOverlay({
      interleaved: true,
      layers: [],
      pickingRadius: PICK_RADIUS,
      onHover: (info) => {
        this.map.getCanvas().style.cursor = info?.picked ? 'pointer' : '';
        onHover?.(info);
      },
    });
    this.map.addControl(this.overlay);

    // Clicks are picked here, at the click, not by deck.gl's own handler: that one picks on the mouse button going
    // down, asynchronously, and drops the click when it comes straight after the pointer arrived.
    this.map.on('click', (event) => {
      let info = null;
      try {
        info = this.overlay.pickObject({ x: event.point.x, y: event.point.y, radius: PICK_RADIUS });
      } catch {
        /* nothing drawn yet */
      }
      onClick?.(info ?? { picked: false });
    });

    // A message from the stream must not move the map while a finger or the mouse is on it.
    this.interacting = false;
    for (const event of ['mousedown', 'touchstart']) this.map.on(event, () => (this.interacting = true));
    for (const event of ['mouseup', 'touchend', 'touchcancel']) this.map.on(event, () => (this.interacting = false));
    // The style is inline, so it is ready at once; waiting for 'load' would wait for every first tile as well.
    this.ready = new Promise((resolve) => this.map.once('style.load', resolve));
    this._fixFramebuffer();
  }

  /** deck.gl 9 in interleaved mode keeps the size of its default framebuffer from the first frame; after the container
   * is resized it draws shifted. Keep it in step with the canvas. */
  _fixFramebuffer() {
    const sync = () => {
      const canvas = this.map.getCanvas();
      const device = this.overlay._deck?.device;
      const framebuffer = device?.canvasContext?.getCurrentFramebuffer?.() ?? device?.getDefaultCanvasContext?.()?.getCurrentFramebuffer?.();
      if (framebuffer && (framebuffer.width !== canvas.width || framebuffer.height !== canvas.height)) {
        framebuffer.resize([canvas.width, canvas.height]);
      }
    };
    this.map.on('resize', sync);
    this.map.on('render', sync);
  }

  setLayers(layers) {
    this.overlay.setProps({ layers });
  }

  get zoom() {
    return this.map.getZoom();
  }

  centerLat() {
    return this.map.getCenter().lat;
  }

  get bearing() {
    return this.map.getBearing();
  }

  /** Screen position in CSS pixels; in the terrain view the point is projected at its altitude, like the markers. */
  project(lon, lat, alt = 0) {
    const viewport = this.mode3d ? this.overlay._deck?.getViewports?.()?.[0] : null;
    if (viewport) {
      const p = viewport.project([lon, lat, alt]);
      return [p[0], p[1]];
    }
    const p = this.map.project([lon, lat]);
    return [p.x, p.y];
  }

  size() {
    const canvas = this.map.getCanvas();
    return [canvas.clientWidth, canvas.clientHeight];
  }

  groundAt(lon, lat) {
    if (!this.mode3d || !this.terrain) return null;
    const h = this.map.queryTerrainElevation([lon, lat]);
    return typeof h === 'number' ? h : null;
  }

  setBase(baseId) {
    this.baseId = baseId;
    for (const b of this.config.regions[0].basemaps) {
      this.map.setLayoutProperty(`base-${b.id}`, 'visibility', b.id === baseId ? 'visible' : 'none');
    }
  }

  setTheme(theme) {
    this.theme = theme;
    this.map.setPaintProperty('background', 'background-color', backgroundFor(theme));
    for (const b of this.config.regions[0].basemaps) {
      const paint = rasterPaint(theme, b);
      for (const prop of ['raster-brightness-min', 'raster-brightness-max', 'raster-hue-rotate', 'raster-saturation', 'raster-contrast']) {
        this.map.setPaintProperty(`base-${b.id}`, prop, paint[prop] ?? null);
      }
    }
    this.map.setSky(skyFor(theme));
  }

  /** Switch between the flat map and the terrain view. Returns a promise that settles when the camera has arrived. */
  set3d(on, { animate = true } = {}) {
    if (on === this.mode3d) return Promise.resolve();
    this.mode3d = on;
    const duration = animate && !this.reducedMotion ? 1100 : 0;
    if (on) {
      if (this.terrain) this.map.setTerrain({ source: 'dem', exaggeration: 1 });
      return this._ease({ pitch: PITCH_3D, duration });
    }
    return this._ease({ pitch: 0, bearing: 0, duration: duration * 0.8 }).then(() => {
      if (!this.mode3d) this.map.setTerrain(null); // after the camera is back above the map, so it does not jump
    });
  }

  _ease(options) {
    return new Promise((resolve) => {
      if (!options.duration) {
        this.map.jumpTo(options);
        resolve();
        return;
      }
      this.map.once('moveend', () => resolve());
      this.map.easeTo({ ...options, essential: false });
    });
  }

  flyTo(lon, lat, { zoom, duration = 900 } = {}) {
    this.map.resize(); // a panel may have changed the size of the map
    const target = { center: [lon, lat], duration: this.reducedMotion ? 0 : duration };
    if (zoom !== undefined) target.zoom = zoom;
    if (this.mode3d) target.zoom = Math.min(target.zoom ?? this.zoom, MAX_3D_ZOOM + 2);
    return this._ease(target);
  }

  /** Move the centre with the aircraft: linear, one second, so the motion is continuous between messages. */
  follow(lon, lat) {
    if (this.interacting) return;
    this.map.easeTo({ center: [lon, lat], duration: this.reducedMotion ? 0 : 1000, easing: (t) => t, essential: false });
  }

  /** Fit a [west, south, east, north] box. In 3D the zoom is capped and the terrain height goes into the camera. */
  fitBounds(bounds, { padding = { top: 90, bottom: 40, left: 40, right: 40 } } = {}) {
    this.map.resize();
    const options = { padding, duration: this.reducedMotion ? 0 : 900, maxZoom: this.mode3d ? MAX_3D_ZOOM : 16 };
    if (!this.mode3d) {
      this.map.fitBounds([[bounds[0], bounds[1]], [bounds[2], bounds[3]]], options);
      return;
    }
    const camera = this.map.cameraForBounds([[bounds[0], bounds[1]], [bounds[2], bounds[3]]], { padding, maxZoom: MAX_3D_ZOOM });
    if (!camera) return;
    const elevation = this.map.queryTerrainElevation(camera.center) ?? 0;
    this.map.easeTo({
      center: camera.center,
      zoom: Math.min(camera.zoom, MAX_3D_ZOOM),
      pitch: PITCH_3D,
      bearing: this.map.getBearing(),
      elevation,
      duration: options.duration,
      essential: false,
    });
  }

  camera() {
    const c = this.map.getCenter();
    return { center: [c.lng, c.lat], zoom: this.map.getZoom() };
  }
}
