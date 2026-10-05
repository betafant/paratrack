// The deck.gl layers: trails, markers and labels of the live aircraft, and the selected flight's track.
// deck.gl is the global `deck` of vendor/deck.gl.min.js.

import { altitudeColor } from './colors.js';

export const FONT = 'Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
export const LABEL_PX = 12;
export const LABEL_OFFSET = 16;

const ICON_PX = 128;
const MARKER_PX = 26;
const MARKER_SELECTED_PX = 36;
const GROUND_PX = 11;

/** The icon sheet, drawn once: an arrowhead (flying, rotated by heading) and a dot (on the ground). */
export function makeIconAtlas() {
  const canvas = document.createElement('canvas');
  canvas.width = ICON_PX * 2;
  canvas.height = ICON_PX;
  const g = canvas.getContext('2d');
  g.fillStyle = '#fff';
  g.beginPath(); // arrowhead pointing up: tip, right wing, notch, left wing
  g.moveTo(64, 10);
  g.lineTo(108, 112);
  g.lineTo(64, 88);
  g.lineTo(20, 112);
  g.closePath();
  g.lineJoin = 'round';
  g.lineWidth = 8;
  g.strokeStyle = '#fff';
  g.stroke();
  g.fill();
  g.beginPath();
  g.arc(ICON_PX + 64, 64, 46, 0, Math.PI * 2);
  g.fill();
  return {
    url: canvas.toDataURL('image/png'),
    mapping: {
      arrow: { x: 0, y: 0, width: ICON_PX, height: ICON_PX, mask: true },
      dot: { x: ICON_PX, y: 0, width: ICON_PX, height: ICON_PX, mask: true },
    },
  };
}

const NO_PATHS = {
  length: 0,
  startIndices: [],
  attributes: { getPath: { value: new Float64Array(0), size: 3 }, getColor: { value: new Uint8Array(0), size: 4, normalized: true } },
};

const position = (mode3d) => (mode3d ? (d) => [d.lon, d.lat, d.alt] : (d) => [d.lon, d.lat, 0]);

/**
 * ctx: {
 *   atlas, palette, mode3d, bearing, selected (address | null), aircraft (shown), labels (Map id -> {text, side}),
 *   trails (binary PathLayer data), track ({path, curtain} | null), trailVersion
 * }
 */
export function buildLayers(ctx) {
  const { IconLayer, TextLayer, PathLayer, ScatterplotLayer, LineLayer } = globalThis.deck;
  const { palette, mode3d, bearing, selected, atlas } = ctx;
  const pos = position(mode3d);
  const isSelected = (d) => d.address === selected;
  const flat = mode3d ? {} : { parameters: { depthCompare: 'always' } }; // flat map: draw in order, no depth test
  const layers = [];

  if (ctx.trails?.length) {
    layers.push(
      new PathLayer({
        id: 'trails',
        data: ctx.trails,
        _pathType: 'open',
        widthUnits: 'pixels',
        getWidth: 2.5,
        capRounded: true,
        jointRounded: true,
        pickable: false,
        ...flat,
      }),
    );
  }

  if (mode3d && ctx.track?.curtain?.length) {
    layers.push(
      new LineLayer({
        id: 'track-curtain',
        data: ctx.track.curtain,
        getColor: palette.curtain,
        widthUnits: 'pixels',
        getWidth: 1,
        pickable: false,
      }),
    );
  }

  if (mode3d) {
    const stems = ctx.aircraft.filter((a) => a.flying && a.gnd !== null && !isSelected(a));
    layers.push(
      new LineLayer({
        id: 'stems',
        data: stems,
        getSourcePosition: (d) => [d.lon, d.lat, d.alt],
        getTargetPosition: (d) => [d.lon, d.lat, d.gnd],
        getColor: palette.curtain,
        widthUnits: 'pixels',
        getWidth: 1,
        pickable: false,
        updateTriggers: { getSourcePosition: ctx.version, getTargetPosition: ctx.version },
      }),
    );
  }

  if (ctx.track?.path?.length) {
    layers.push(
      new PathLayer({
        id: 'track',
        data: ctx.track.path,
        _pathType: 'open',
        widthUnits: 'pixels',
        getWidth: 4,
        capRounded: true,
        jointRounded: true,
        pickable: false,
        ...flat,
      }),
    );
  }

  const markerColor = (d) => (d.flying ? [...palette.accent, 255] : [...palette.ground, 200]);
  const markerSize = (d) => (isSelected(d) ? MARKER_SELECTED_PX : d.flying ? MARKER_PX : GROUND_PX);
  const markerIcon = (d) => (d.flying && d.hdg !== null ? 'arrow' : 'dot');
  const angle = (d) => bearing - (d.hdg ?? 0); // deck turns counter-clockwise, headings run clockwise
  const smooth = { getPosition: ctx.transition, getAngle: ctx.transition };
  const iconCommon = {
    data: ctx.aircraft,
    iconAtlas: atlas.url,
    iconMapping: atlas.mapping,
    getIcon: markerIcon,
    getPosition: pos,
    getAngle: angle,
    billboard: true,
    sizeUnits: 'pixels',
    transitions: ctx.transition ? smooth : undefined,
    updateTriggers: {
      getPosition: [mode3d, ctx.version],
      getAngle: [bearing, ctx.version],
      getIcon: ctx.version,
      getSize: [selected, ctx.version],
      getColor: [selected, ctx.version, palette.accent],
    },
    ...flat,
  };
  layers.push(
    new IconLayer({
      ...iconCommon,
      id: 'marker-outline',
      getSize: (d) => markerSize(d) + 7,
      getColor: palette.labelBackground,
      pickable: false,
    }),
    new IconLayer({
      ...iconCommon,
      id: 'markers',
      getSize: markerSize,
      getColor: markerColor,
      pickable: true,
    }),
  );

  const chosen = ctx.aircraft.find(isSelected);
  if (chosen) {
    layers.push(
      new ScatterplotLayer({
        id: 'selection-ring',
        data: [chosen],
        getPosition: pos,
        radiusUnits: 'pixels',
        getRadius: 24,
        stroked: true,
        filled: false,
        lineWidthUnits: 'pixels',
        getLineWidth: 2,
        getLineColor: [...palette.halo, 235],
        pickable: false,
        updateTriggers: { getPosition: [mode3d, ctx.version] },
        ...flat,
      }),
    );
  }

  const labelled = ctx.aircraft.filter((a) => ctx.labels.has(a.id));
  if (labelled.length === 0) return layers; // an empty TextLayer makes deck.gl upload a 0x0 font atlas (a WebGL warning)
  layers.push(
    new TextLayer({
      id: 'labels',
      data: labelled,
      getPosition: pos,
      getText: (d) => ctx.labels.get(d.id).text,
      getSize: LABEL_PX,
      sizeUnits: 'pixels',
      fontFamily: FONT,
      fontWeight: 600,
      characterSet: 'auto',
      getColor: palette.labelText,
      getTextAnchor: (d) => (ctx.labels.get(d.id).side === 'left' ? 'end' : 'start'),
      getAlignmentBaseline: 'center',
      getPixelOffset: (d) => [ctx.labels.get(d.id).side === 'left' ? -LABEL_OFFSET : LABEL_OFFSET, 0],
      billboard: true,
      background: true,
      getBackgroundColor: palette.labelBackground,
      getBorderColor: palette.labelBorder,
      getBorderWidth: 1,
      backgroundPadding: [6, 3, 6, 3],
      backgroundBorderRadius: 6,
      pickable: true,
      transitions: ctx.transition ? { getPosition: ctx.transition } : undefined,
      updateTriggers: {
        getText: ctx.labelVersion,
        getPosition: [mode3d, ctx.version],
        getTextAnchor: ctx.labelVersion,
        getPixelOffset: ctx.labelVersion,
        getColor: palette.labelText,
        getBackgroundColor: palette.labelBackground,
        getBorderColor: palette.labelBorder,
      },
      ...flat,
    }),
  );
  return layers;
}

/**
 * The day view. ctx: { palette, mode3d, previews ({data} of buildPreviews) | null, highlight (binary data) | null,
 * track ({path, curtain}) | null, ends ([{ pos, color }]) | null }. The previews are pickable: deck.gl reports the
 * number of the path under the pointer, which `owners` of buildPreviews maps back to a flight.
 */
export function buildDayLayers(ctx) {
  const { PathLayer, LineLayer, ScatterplotLayer } = globalThis.deck;
  const { palette, mode3d } = ctx;
  const flat = mode3d ? {} : { parameters: { depthCompare: 'always' } };
  const path = (id, data, width, extra = {}) =>
    new PathLayer({
      id,
      data,
      _pathType: 'open',
      widthUnits: 'pixels',
      getWidth: width,
      capRounded: true,
      jointRounded: true,
      pickable: false,
      ...flat,
      ...extra,
    });
  const layers = [];
  if (ctx.previews?.data.length) layers.push(path('previews', ctx.previews.data, 2, { pickable: true, widthMinPixels: 1.5 }));
  // always in the stack, so the layers do not change when the pointer moves over a path: a click right after a hover
  // would otherwise be picked against layers deck.gl has not drawn yet
  layers.push(path('preview-hover', ctx.highlight ?? NO_PATHS, 4.5));
  if (mode3d && ctx.track?.curtain?.length) {
    layers.push(
      new LineLayer({
        id: 'track-curtain',
        data: ctx.track.curtain,
        getColor: palette.curtain,
        widthUnits: 'pixels',
        getWidth: 1,
        pickable: false,
      }),
    );
  }
  if (ctx.track?.path?.length) layers.push(path('track', ctx.track.path, 4));
  if (ctx.ends?.length) {
    layers.push(
      new ScatterplotLayer({
        id: 'track-ends',
        data: ctx.ends,
        getPosition: (d) => d.pos,
        getFillColor: (d) => d.color,
        getLineColor: [...palette.halo, 240],
        stroked: true,
        radiusUnits: 'pixels',
        getRadius: 6,
        lineWidthUnits: 'pixels',
        getLineWidth: 2,
        pickable: false,
        ...flat,
      }),
    );
  }
  return layers;
}

export { altitudeColor };
