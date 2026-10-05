// The server: JSON requests and the live stream. URLs are relative, so the app also works below a path prefix
// behind a reverse proxy.

export async function getJson(path, { signal } = {}) {
  const response = await fetch(path, { signal, headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

export const getConfig = () => getJson('api/config');
export const getStatus = () => getJson('api/status');
export const getTrack = (flightId, options) => getJson(`api/flights/${encodeURIComponent(flightId)}/track`, options);

/**
 * A server-relative path like "/api/dem/{z}/{x}/{y}.png" as an absolute URL (map workers cannot resolve relative
 * ones). The {z}/{x}/{y} placeholders stay as they are: URL() would escape the braces.
 */
export function absoluteUrl(path) {
  const text = String(path);
  const at = text.indexOf('{');
  const head = at < 0 ? text : text.slice(0, at);
  const base = new URL(document.baseURI);
  base.username = ''; // a page opened as http://user:password@host/ must not put them into every tile URL
  base.password = '';
  return new URL(head.replace(/^\/+/, ''), base).href + (at < 0 ? '' : text.slice(at));
}

/**
 * The server-sent event stream with a safety net: EventSource reconnects by itself after a dropped connection, but
 * not after an HTTP error, and a proxy can leave a connection open without sending anything.
 */
export class LiveStream {
  constructor({ url = 'api/live/stream', onMessage, onState, stallSeconds = 15, retrySeconds = 3 }) {
    this.url = url;
    this.onMessage = onMessage;
    this.onState = onState ?? (() => {});
    this.stallSeconds = stallSeconds;
    this.retrySeconds = retrySeconds;
    this.source = null;
    this.timer = null;
    this.lastMessage = 0;
    this.stopped = true;
  }

  start() {
    this.stopped = false;
    this._open();
    this.watchdog = setInterval(() => {
      if (!this.stopped && this.source?.readyState === EventSource.OPEN && Date.now() - this.lastMessage > this.stallSeconds * 1000) {
        this._open();
      }
    }, 2000);
  }

  stop() {
    this.stopped = true;
    clearInterval(this.watchdog);
    clearTimeout(this.timer);
    this.source?.close();
    this.source = null;
  }

  _open() {
    this.source?.close();
    clearTimeout(this.timer);
    this.lastMessage = Date.now();
    const source = new EventSource(this.url);
    this.source = source;
    source.onopen = () => this.onState('open');
    source.onmessage = (event) => {
      this.lastMessage = Date.now();
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return; // a damaged message is not worth a reconnect
      }
      this.onMessage(message);
    };
    source.onerror = () => {
      this.onState('retrying');
      if (source.readyState === EventSource.CLOSED && !this.stopped) {
        this.timer = setTimeout(() => this._open(), this.retrySeconds * 1000);
      }
    };
  }
}
