import test from 'node:test';
import assert from 'node:assert/strict';
import type { RoutePreviewResponse } from '../../mobile/src/api/generated';
import { distanceFromRoute, RouteFollowingStore } from '../src/routeFollowing';

const route = (coordinates: [number, number][] = [[3.72, 51.05], [3.73, 51.05]]): RoutePreviewResponse => ({
  client_revision: 1, geometry: { type: 'LineString', coordinates }, distance_m: 700, duration_s: 500, provider: 'synthetic',
  attribution: { text: 'Synthetic', url: '', fix_map_url: '', waypoints_notice: '' },
});
function harness() {
  let now = 1_000_000, next = 0;
  const callbacks = new Map<number, { success: PositionCallback; fail: PositionErrorCallback }>();
  const active = new Set<number>();
  const geo = {
    watchPosition(success: PositionCallback, fail: PositionErrorCallback | null, options?: PositionOptions) {
      assert.deepEqual(options, { enableHighAccuracy: true, maximumAge: 0, timeout: 10_000 });
      callbacks.set(++next, { success, fail: fail! }); active.add(next); return next;
    },
    clearWatch(id: number) { active.delete(id); },
  };
  const store = new RouteFollowingStore(() => geo, () => now);
  store.setRoute(route());
  return { store, active, advance(ms: number) { now += ms; },
    point(id: number, longitude = 3.725, latitude = 51.05, accuracy = 5, timestamp = now) {
      callbacks.get(id)!.success({ timestamp, coords: { longitude, latitude, accuracy } } as GeolocationPosition);
    },
    error(id: number, code: number) { callbacks.get(id)!.fail({ code } as GeolocationPositionError); },
  };
}

test('nearest route distance respects segment interiors, endpoints, duplicates and the antimeridian', () => {
  assert.ok(distanceFromRoute([0.5, 0], [[0, 0], [1, 0]]) < .001);
  assert.ok(Math.abs(distanceFromRoute([0.5, .001], [[0, 0], [1, 0]]) - 111.195) < .01);
  assert.ok(distanceFromRoute([-1, 0], [[0, 0], [1, 0]]) > 111_000);
  assert.ok(distanceFromRoute([2, 0], [[0, 0], [1, 0]]) > 111_000);
  assert.ok(distanceFromRoute([0, .001], [[0, 0], [0, 0]]) > 111);
  assert.ok(distanceFromRoute([180, 0], [[179.9, 0], [-179.9, 0]]) < .001);
  assert.ok(distanceFromRoute([0, 0], [[179.9, 0], [-179.9, 0]]) > 19_000_000);
  assert.ok(distanceFromRoute([20, 80], [[20, 80], [21, 80]]) < .001);
});

test('fresh fixes distinguish on/off/uncertain positions using reported accuracy and recover from GPS errors', () => {
  const h = harness(); h.store.start();
  h.point(1); assert.equal(h.store.getState().course, 'on-route');
  h.advance(1); h.point(1, 3.725, 51.051); assert.equal(h.store.getState().course, 'off-route');
  h.advance(1); h.point(1, 3.725, 51.05045, 15); assert.equal(h.store.getState().course, 'uncertain');
  h.error(1, 2); assert.equal(h.store.getState().status, 'lost'); assert.equal(h.store.getState().position, null);
  h.advance(1); h.point(1); assert.equal(h.store.getState().status, 'following');
  h.advance(1); h.point(1, 3.725, 51.05, 80); assert.equal(h.store.getState().position, null);
  assert.match(h.store.getState().message, /accuracy is low/);
  h.store.stop();
});

test('stale, out-of-order and invalid fixes cannot refresh the GPS deadline or publish guidance', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const h = harness(); h.store.start(); h.point(1);
  h.advance(10_000); t.mock.timers.tick(10_000);
  h.point(1, 3.725, 51.051, 5, 1_000_000); // duplicate timestamp
  h.point(1, 3.725, 51.051, 5, 999_000); // older timestamp
  h.point(1, 3.725, 51.051, 5, 2_000_000); // future timestamp
  h.point(1, 3.725, 51.051, 5, 900_000); // stale cached position
  assert.equal(h.store.getState().course, 'on-route');
  h.advance(5000); t.mock.timers.tick(5000);
  assert.equal(h.store.getState().status, 'lost'); assert.equal(h.store.getState().position, null);
  h.advance(1); h.point(1); assert.equal(h.store.getState().status, 'following');
  h.advance(1); h.point(1, NaN); assert.equal(h.store.getState().status, 'lost');
  h.advance(1); h.point(1, 3.725, 51.05, -1); assert.equal(h.store.getState().position, null);
  h.store.stop();
});

test('a delayed but acceptable fix expires at its own timestamp, and no first fix also expires', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const h = harness(); h.store.start(); h.point(1, 3.725, 51.05, 5, 995_000);
  h.advance(10_000); t.mock.timers.tick(10_000); assert.equal(h.store.getState().status, 'lost');
  h.store.start(); h.advance(15_000); t.mock.timers.tick(15_000);
  assert.equal(h.store.getState().status, 'lost'); h.store.stop();
});

test('stop, route replacement, hidden-page pause, retry and account reset fence old callbacks and clear watches', () => {
  const h = harness(); h.store.start(); h.point(1);
  h.store.setCentered(false); assert.equal(h.store.getState().centered, false);
  h.store.pause(); assert.equal(h.active.size, 0); assert.equal(h.store.getState().position, null);
  h.advance(1); h.point(1); assert.equal(h.store.getState().status, 'paused');
  h.store.start(); h.point(2); assert.equal(h.store.getState().centered, true);
  h.store.start(); assert.deepEqual([...h.active], [3]); h.point(2); assert.equal(h.store.getState().position, null);
  h.store.setRoute(route([[4, 50], [4.01, 50]])); assert.equal(h.active.size, 0); h.point(3); assert.equal(h.store.getState().status, 'idle');
  h.store.start(); h.store.reset(); h.point(4); assert.equal(h.store.getState().position, null); assert.equal(h.active.size, 0);
  h.store.start(); assert.equal(h.active.size, 0); // Reset also forgets the previous owner's route.
});

test('permission revocation clears the watch and a synchronous denial cannot leak a newly returned watch ID', () => {
  const h = harness(); h.store.start(); h.point(1); h.error(1, 1);
  assert.equal(h.active.size, 0); assert.equal(h.store.getState().status, 'denied'); assert.equal(h.store.getState().position, null);
  h.advance(1); h.point(1); assert.equal(h.store.getState().status, 'denied');
  const cleared: number[] = [];
  const synchronous = new RouteFollowingStore(() => ({ watchPosition(_success, fail) { fail!({ code: 1 } as GeolocationPositionError); return 7; }, clearWatch(id) { cleared.push(id); } }));
  synchronous.setRoute(route()); synchronous.start();
  assert.equal(synchronous.getState().status, 'denied'); assert.deepEqual(cleared, [7]);
});

test('unavailable browsers and invalid or oversized routes fail without requesting GPS', () => {
  const unsupported = new RouteFollowingStore(() => undefined); unsupported.setRoute(route()); unsupported.start(); assert.equal(unsupported.getState().status, 'error');
  let requests = 0;
  const store = new RouteFollowingStore(() => { requests++; throw Error(); });
  for (const points of [[], [[0, 0]], [[0, 0], [181, 50]], [[0, 0], [0, 86]], Array.from({ length: 10_001 }, () => [0, 0])] as [number, number][][]) {
    store.setRoute(route(points)); store.start(); assert.equal(store.getState().status, 'error');
  }
  assert.equal(requests, 0);
});
