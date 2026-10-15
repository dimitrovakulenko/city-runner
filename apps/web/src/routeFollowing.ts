import type { RoutePreviewResponse } from '../../mobile/src/api/generated';

type Point = [number, number];
type GeolocationApi = Pick<Geolocation, 'watchPosition' | 'clearWatch'>;
export type FollowingState = {
  status: 'idle' | 'searching' | 'following' | 'lost' | 'paused' | 'denied' | 'error';
  position: { coordinate: Point; accuracy: number } | null;
  distance: number | null;
  course: 'on-route' | 'off-route' | 'uncertain' | null;
  message: string;
  centered: boolean;
};
const INITIAL: FollowingState = { status: 'idle', position: null, distance: null, course: null, message: '', centered: true };
const FRESH_MS = 15_000;
const radians = (degrees: number) => degrees * Math.PI / 180;
const clamp = (value: number) => Math.max(-1, Math.min(1, value));
const validPoint = (point: Point) => point.length === 2 && point.every(Number.isFinite) && Math.abs(point[0]) <= 180 && Math.abs(point[1]) <= 85;

function angle(a: Point, b: Point): number {
  const h = Math.sin(radians(b[1] - a[1]) / 2) ** 2 + Math.cos(radians(a[1])) * Math.cos(radians(b[1])) * Math.sin(radians(b[0] - a[0]) / 2) ** 2;
  return 2 * Math.asin(Math.sqrt(Math.max(0, Math.min(1, h))));
}
function bearing(a: Point, b: Point): number {
  const longitude = radians(b[0] - a[0]);
  return Math.atan2(Math.sin(longitude) * Math.cos(radians(b[1])), Math.cos(radians(a[1])) * Math.sin(radians(b[1])) - Math.sin(radians(a[1])) * Math.cos(radians(b[1])) * Math.cos(longitude));
}

/** Distance to the bounded great-circle segments, including their endpoints. */
export function distanceFromRoute(point: Point, route: Point[]): number {
  let nearest = Infinity;
  for (let index = 1; index < route.length; index++) {
    const a = route[index - 1], b = route[index];
    const length = angle(a, b), start = angle(a, point), end = angle(b, point);
    let distance = Math.min(start, end);
    if (length > 0 && length < Math.PI - 1e-6) {
      const direction = bearing(a, point) - bearing(a, b);
      const along = Math.atan2(Math.sin(start) * Math.cos(direction), Math.cos(start));
      if (along >= 0 && along <= length) distance = Math.abs(Math.asin(clamp(Math.sin(start) * Math.sin(direction))));
    }
    nearest = Math.min(nearest, distance);
  }
  return nearest * 6_371_008.8;
}

/** Foreground-only guidance. Positions are ephemeral and never become activities. */
export class RouteFollowingStore {
  private state = INITIAL;
  private route: RoutePreviewResponse | null = null;
  private coordinates: Point[] = [];
  private generation = 0;
  private watch: number | null = null;
  private geolocation: GeolocationApi | null = null;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private lastTimestamp = -Infinity;
  private listeners = new Set<(state: FollowingState) => void>();

  constructor(private readonly getGeolocation: () => GeolocationApi | undefined = () => navigator.geolocation, private readonly now = Date.now) {}
  getState(): FollowingState { return this.state; }
  subscribe(listener: (state: FollowingState) => void): () => void { this.listeners.add(listener); return () => this.listeners.delete(listener); }
  private update(patch: Partial<FollowingState>): void { this.state = { ...this.state, ...patch }; for (const listener of this.listeners) listener(this.state); }

  setRoute(route: RoutePreviewResponse | null): void {
    if (route === this.route) return;
    this.stop(); this.route = route;
    this.coordinates = route?.geometry.coordinates.map((point) => [...point] as Point) ?? [];
  }
  reset(): void { this.stop(); this.route = null; this.coordinates = []; }
  stop(): void { this.clearWatch(); this.update({ ...INITIAL }); }
  pause(): void {
    if (this.state.status === 'idle') return;
    this.clearWatch();
    this.update({ status: 'paused', position: null, distance: null, course: null, message: 'Following paused. Resume when this page is visible.' });
  }
  setCentered(centered: boolean): void { this.update({ centered }); }

  start(): void {
    if (!this.route || typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    this.clearWatch(); this.lastTimestamp = -Infinity;
    this.update({ ...INITIAL, status: 'searching', message: 'Waiting for GPS…' });
    if (this.coordinates.length < 2 || this.coordinates.length > 10_000 || !this.coordinates.every(validPoint)) {
      this.update({ status: 'error', message: 'This route is outside the supported map area.' }); return;
    }
    const generation = this.generation;
    const current = () => generation === this.generation && (typeof document === 'undefined' || document.visibilityState === 'visible');
    const lost = (message: string) => { clearTimeout(this.timer); this.update({ status: 'lost', position: null, distance: null, course: null, message }); };
    this.timer = setTimeout(() => { if (current()) lost('GPS update overdue. Waiting for a fresh position.'); }, FRESH_MS);
    try {
      const geo = this.getGeolocation();
      if (!geo) throw new Error('Unavailable');
      this.geolocation = geo;
      const watch = geo.watchPosition((position) => {
        if (!current()) return;
        const age = this.now() - position.timestamp;
        if (!Number.isFinite(age) || age < -1000 || age >= FRESH_MS || position.timestamp <= this.lastTimestamp) return;
        this.lastTimestamp = position.timestamp;
        const { longitude, latitude, accuracy } = position.coords;
        const coordinate: Point = [longitude, latitude];
        if (!validPoint(coordinate) || !Number.isFinite(accuracy) || accuracy < 0) { lost('GPS returned an invalid position. Waiting for a fresh position.'); return; }
        if (accuracy > 50) { lost(`GPS accuracy is low (±${Math.round(accuracy)} m). Waiting for a better position.`); return; }
        const distance = distanceFromRoute(coordinate, this.coordinates);
        const course = distance - accuracy > 50 ? 'off-route' : distance + accuracy <= 50 ? 'on-route' : 'uncertain';
        this.update({ status: 'following', position: { coordinate, accuracy }, distance, course,
          message: course === 'off-route' ? `Off route · ${Math.round(distance)} m from route` : course === 'on-route' ? 'On route' : 'Near route · GPS is uncertain' });
        clearTimeout(this.timer);
        this.timer = setTimeout(() => { if (current()) lost('GPS update overdue. Waiting for a fresh position.'); }, FRESH_MS - Math.max(0, age));
      }, (error) => {
        if (!current()) return;
        if (error.code === 1) {
          this.clearWatch(); this.update({ status: 'denied', position: null, distance: null, course: null, message: 'Location permission denied. Allow location in your browser settings, then retry.' });
        } else lost(error.code === 3 ? 'GPS timed out. Waiting for a fresh position.' : 'GPS is unavailable. Waiting for a fresh position.');
      }, { enableHighAccuracy: true, maximumAge: 0, timeout: 10_000 });
      if (current()) this.watch = watch;
      else geo.clearWatch(watch);
    } catch {
      if (current()) { this.clearWatch(); this.update({ status: 'error', position: null, distance: null, course: null, message: 'Location is unavailable. Use HTTPS or localhost and a browser with location support.' }); }
    }
  }

  private clearWatch(): void {
    this.generation++; clearTimeout(this.timer);
    if (this.watch !== null) this.geolocation?.clearWatch(this.watch);
    this.watch = null; this.geolocation = null;
  }
}
