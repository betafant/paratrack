// The flights of one day: loaded from the API, with the path of each. Flights that are still in the air have no
// stored preview yet (it is made when a flight closes), so their path comes from the track endpoint, a few at a time.
// No DOM; the fetch functions are passed in, so this runs under `node --test` too.

import { simplifyColumns } from './previews.js';

export class DayView {
  constructor({ fetchDay, fetchTrack, onChange = () => {}, concurrency = 4 }) {
    this.fetchDay = fetchDay;
    this.fetchTrack = fetchTrack;
    this.onChange = onChange;
    this.concurrency = concurrency;
    this.day = null;
    this.flights = [];
    this.loading = false;
    this.failed = false;
    this.version = 0; // moves whenever the flights or one of their paths changes
    this._token = 0;
    this._queue = [];
    this._running = 0;
  }

  get(id) {
    return this.flights.find((f) => String(f.id) === String(id));
  }

  clear() {
    this._token += 1;
    this._queue = [];
    this.day = null;
    this.flights = [];
    this.loading = false;
    this.failed = false;
    this.version += 1;
  }

  /** Load a day. With `keep` the flights already shown stay until the new list arrives (a refresh, not a new day). */
  async load(day, { keep = false } = {}) {
    const token = ++this._token;
    this._queue = [];
    if (!keep || day !== this.day) {
      this.flights = [];
      this.version += 1;
    }
    this.day = day;
    this.loading = true;
    this.failed = false;
    this.onChange();
    let flights;
    try {
      flights = await this.fetchDay(day);
    } catch (error) {
      if (token !== this._token) return false;
      this.loading = false;
      this.failed = true;
      this.onChange();
      throw error;
    }
    if (token !== this._token) return false; // another day was asked for meanwhile
    const before = new Map(this.flights.map((f) => [String(f.id), f.path]));
    this.flights = flights.map((f) => ({ ...f, path: f.preview ?? before.get(String(f.id)) ?? null }));
    this.loading = false;
    this.version += 1;
    this.onChange();
    for (const f of this.flights) if (f.preview === null || f.preview === undefined) this._queue.push([token, f.id]);
    this._pump();
    return true;
  }

  _pump() {
    while (this._running < this.concurrency && this._queue.length) {
      const [token, id] = this._queue.shift();
      if (token !== this._token) continue;
      this._running += 1;
      this.fetchTrack(id)
        .then((columns) => {
          if (token !== this._token) return;
          const flight = this.get(id);
          if (flight) {
            flight.path = simplifyColumns(columns);
            this.version += 1;
            this.onChange();
          }
        })
        .catch(() => {
          /* the flight just stays without a path; the next refresh tries again */
        })
        .finally(() => {
          this._running -= 1;
          this._pump();
        });
    }
  }
}
