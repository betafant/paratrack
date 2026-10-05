// The URL hash is the state of the view, so a view can be bookmarked and the back button works:
//   #/live                          all paragliders in the air
//   #/live?sel=112880&view=3d       one selected (by device address), terrain view
//   #/live?ll=46.80,8.23&z=7.4      camera position (written while moving, never makes history entries)

const ROUTES = ['live'];
const ADDRESS = /^[A-Za-z0-9_-]{1,32}$/;

export const DEFAULT_STATE = Object.freeze({ route: 'live', sel: null, view: '2d', ll: null, z: null });

function number(text, min, max) {
  const n = Number(text);
  return text !== '' && Number.isFinite(n) && n >= min && n <= max ? n : null;
}

export function parseHash(hash = '') {
  const raw = String(hash).replace(/^#/, '');
  const [path, query = ''] = raw.split('?');
  const params = new URLSearchParams(query);
  const state = { ...DEFAULT_STATE };

  const route = path.replace(/^\/+/, '').split('/')[0];
  if (ROUTES.includes(route)) state.route = route;

  const sel = params.get('sel');
  if (sel && ADDRESS.test(sel)) state.sel = sel;
  if (params.get('view') === '3d') state.view = '3d';

  const [lat, lon] = (params.get('ll') ?? '').split(',');
  if (lat !== undefined && lon !== undefined) {
    const la = number(lat ?? '', -90, 90);
    const lo = number(lon ?? '', -180, 180);
    if (la !== null && lo !== null) state.ll = [la, lo];
  }
  const z = number(params.get('z') ?? '', 0, 24);
  if (z !== null) state.z = z;
  return state;
}

export function formatHash(state, { camera = true } = {}) {
  const params = new URLSearchParams();
  if (state.sel) params.set('sel', state.sel);
  if (state.view === '3d') params.set('view', '3d');
  if (camera && state.ll) params.set('ll', `${state.ll[0].toFixed(5)},${state.ll[1].toFixed(5)}`);
  if (camera && state.z !== null && state.z !== undefined) params.set('z', state.z.toFixed(2));
  const query = params.toString().replace(/%2C/g, ',');
  return `#/${state.route}${query ? `?${query}` : ''}`;
}

/** The part of the state that makes a new history entry when it changes (the camera does not). */
export const navigationKey = (state) => formatHash(state, { camera: false });
