import assert from 'node:assert/strict';
import test from 'node:test';
import type { RouteDetail, RoutePage, RoutePreviewResponse, RouteSummary } from '../src/api/generated';
import type { RouteApi } from '../src/api/routes';
import { RoutePlannerStore } from '../src/routePlannerStore';

const attribution = { text: 'OpenStreetMap contributors', url: 'https://www.openstreetmap.org/copyright', fix_map_url: 'https://www.openstreetmap.org', waypoints_notice: 'Planned route' };
function detail(id = 'route-1', revision = 1, name = 'Loop'): RouteDetail {
  return { id, name, revision, distance_m: 1200, created_at: '2026-10-06T00:00:00Z', updated_at: '2026-10-06T00:00:00Z',
    waypoints: [[4.3, 50.8], [4.31, 50.81]], geometry: { type: 'LineString', coordinates: [[4.3, 50.8], [4.31, 50.81]] },
    duration_s: 600, provider: 'OSRM', attribution };
}
const summary = (route: RouteDetail): RouteSummary => ({ id: route.id, name: route.name, revision: route.revision, distance_m: route.distance_m, created_at: route.created_at, updated_at: route.updated_at });
const tick = () => new Promise<void>((resolve) => setImmediate(resolve));
function fakeApi() {
  const calls: string[] = [];
  let saved = [summary(detail())];
  let preview = async (body: { client_revision: number }): Promise<RoutePreviewResponse> => {
    calls.push(`preview:${body.client_revision}`); const route = detail(); return { client_revision: body.client_revision, geometry: route.geometry, distance_m: route.distance_m, duration_s: route.duration_s, provider: route.provider, attribution };
  };
  let get = async (_id: string) => detail();
  let create = async (_body: { name: string }) => { const result = detail('created', 1, 'New'); saved = [summary(result), ...saved]; return result; };
  let update = async (id: string, body: { expected_revision: number }) => { const result = detail(id, body.expected_revision + 1); saved = [summary(result), ...saved.filter((row) => row.id !== id)]; return result; };
  let remove = async (id: string, _revision: number) => { saved = saved.filter((row) => row.id !== id); };
  let list = async ({ limit, offset }: { limit: number; offset: number }): Promise<RoutePage> => ({ items: saved.slice(offset, offset + limit), total: saved.length, limit, offset });
  let exportGpx = async (_id: string) => '<gpx />';
  const api: RouteApi = {
    preview: (body) => preview(body), list: (input) => list(input), get: (id) => get(id), create: (body) => create(body),
    update: (id, body) => update(id, body), delete: (id, revision) => remove(id, revision), exportGpx: (id) => exportGpx(id),
  };
  return { api, calls, setPreview(fn: typeof preview) { preview = fn; }, setGet(fn: typeof get) { get = fn; }, setCreate(fn: typeof create) { create = fn; },
    setUpdate(fn: typeof update) { update = fn; }, setRemove(fn: typeof remove) { remove = fn; }, setList(fn: typeof list) { list = fn; }, setExport(fn: typeof exportGpx) { exportGpx = fn; } };
}
function validDraft(store: RoutePlannerStore): void {
  store.setName('  Evening loop  '); store.addWaypoint([4.3, 50.8]); store.addWaypoint([4.31, 50.81]);
}

test('edits are bounded, preview is explicit and any edit invalidates its result; one-level undo restores draft', async () => {
  const fake = fakeApi(); const store = new RoutePlannerStore(fake.api);
  store.setName('Plan'); store.addWaypoint([4, 50]); store.addWaypoint([4.1, 50.1]);
  assert.equal(fake.calls.length, 0); await store.preview(); assert.deepEqual(fake.calls, ['preview:3']);
  assert.equal(store.getState().previewStatus, 'ready');
  store.moveWaypoint(0, [4.2, 50.2]); assert.equal(store.getState().preview, null); assert.equal(store.getState().undoAvailable, true);
  assert.equal(store.undo(), true); assert.deepEqual(store.getState().waypoints, [[4, 50], [4.1, 50.1]]);
  assert.equal(store.undo(), false);
  assert.equal(store.addWaypoint([181, 0]), false); assert.match(store.getState().draftError ?? '', /WGS84/);
  for (let i = 2; i < 20; i++) assert.equal(store.addWaypoint([i, 0]), true);
  assert.equal(store.addWaypoint([0, 0]), false);
});

test('nameless waypoint draft can preview; save still requires a valid name', async () => {
  const fake = fakeApi(); const store = new RoutePlannerStore(fake.api);
  store.addWaypoint([4.3, 50.8]); store.addWaypoint([4.31, 50.81]);
  await store.preview(); assert.equal(store.getState().previewStatus, 'ready');
  assert.equal(await store.saveRoute(), null); assert.match(store.getState().saveError ?? '', /name/);
});

test('preview rejects stale draft and account responses', async () => {
  const fake = fakeApi(); let finish!: (value: RoutePreviewResponse) => void;
  fake.setPreview(async (body) => new Promise((resolve) => { finish = resolve; }));
  const store = new RoutePlannerStore(fake.api); validDraft(store); const pending = store.preview(); await tick();
  store.setName('changed'); finish!({ client_revision: 3, geometry: detail().geometry, distance_m: 1, duration_s: 1, provider: 'x', attribution }); await pending;
  assert.equal(store.getState().preview, null); assert.equal(store.getState().previewStatus, 'idle');

  const second = new RoutePlannerStore(fake.api); validDraft(second); let finishAccount!: (value: RoutePreviewResponse) => void;
  fake.setPreview(async () => new Promise((resolve) => { finishAccount = resolve; })); const pendingAccount = second.preview(); await tick();
  second.reset(); finishAccount({ client_revision: 3, geometry: detail().geometry, distance_m: 1, duration_s: 1, provider: 'x', attribution }); await pendingAccount;
  assert.equal(second.getState().preview, null); assert.equal(second.getState().name, '');
});

test('open route seeds preview and save adopts server revision without replacing intervening edits', async () => {
  const fake = fakeApi(); const store = new RoutePlannerStore(fake.api); await store.openRoute('route-1');
  assert.equal(store.getState().expectedRevision, 1); assert.equal(store.getState().preview?.client_revision, store.getState().draftRevision);
  let finish!: (value: RouteDetail) => void;
  fake.setUpdate(async (_id, body) => new Promise((resolve) => { finish = () => resolve(detail('route-1', body.expected_revision + 1, 'Saved')); }));
  const saving = store.saveRoute(); await tick(); store.setName('Unsaved rename'); finish!(detail('route-1', 2, 'Saved')); await saving;
  assert.equal(store.getState().name, 'Unsaved rename'); assert.equal(store.getState().expectedRevision, 2); assert.equal(store.getState().preview, null);
});

test('delayed open cannot replace a newer draft and clears its loading state', async () => {
  const fake = fakeApi(); let finish!: (route: RouteDetail) => void;
  fake.setGet(async () => new Promise((resolve) => { finish = resolve; })); const store = new RoutePlannerStore(fake.api);
  const opening = store.openRoute('route-1'); await tick(); store.setName('New draft'); finish(detail()); await opening;
  assert.equal(store.getState().name, 'New draft'); assert.equal(store.getState().selectedRoute, null); assert.equal(store.getState().selectedRouteLoading, false);
});

test('save fences an older route open so an old detail cannot overwrite the saved revision', async () => {
  const fake = fakeApi(); const store = new RoutePlannerStore(fake.api); await store.openRoute('route-1');
  let finishOpen!: (route: RouteDetail) => void;
  fake.setGet(async () => new Promise((resolve) => { finishOpen = resolve; }));
  const opening = store.openRoute('route-1'); await tick();
  fake.setUpdate(async (id, body) => detail(id, body.expected_revision + 1, 'Saved'));
  await store.saveRoute(); finishOpen(detail('route-1', 1, 'Stale')); await opening;
  assert.equal(store.getState().expectedRevision, 2); assert.equal(store.getState().selectedRoute?.name, 'Saved');
  assert.equal(store.getState().selectedRouteLoading, false);
});

test('delete fences an older route open and clears the deleted selection after acknowledgement', async () => {
  const fake = fakeApi(); const store = new RoutePlannerStore(fake.api); await store.openRoute('route-1');
  let finishOpen!: (route: RouteDetail) => void; let finishDelete!: () => void;
  fake.setGet(async () => new Promise((resolve) => { finishOpen = resolve; }));
  const opening = store.openRoute('route-1'); await tick();
  fake.setRemove(async () => new Promise<void>((resolve) => { finishDelete = resolve; }));
  const deleting = store.deleteRoute(); await tick(); finishOpen(detail('route-1', 1, 'Stale')); await opening;
  assert.equal(store.getState().selectedRoute?.name, 'Loop');
  finishDelete(); assert.equal(await deleting, true);
  assert.equal(store.getState().editingRouteId, null); assert.equal(store.getState().selectedRoute, null);
});

test('create, paginated list, delete and GPX export use selected route revision', async () => {
  const fake = fakeApi(); const store = new RoutePlannerStore(fake.api); validDraft(store); await store.preview();
  const created = await store.saveRoute(); assert.equal(created?.id, 'created'); assert.equal(store.getState().routesTotal, 2);
  await tick(); assert.equal(store.getState().routesTotal, 2);
  assert.equal(await store.exportGpx(), '<gpx />'); assert.equal(await store.deleteRoute(), true);
  assert.equal(store.getState().editingRouteId, null); await tick(); assert.equal(store.getState().routes.length, 1);
  await store.loadRoutes(); assert.equal(store.getState().routes[0]?.id, 'route-1');
  await store.loadMoreRoutes(); assert.equal(store.getState().routesOffset, 1);
});

test('save invalidates a pending page, reloads authoritative offsets and cannot leave loading stuck', async () => {
  const fake = fakeApi(); const store = new RoutePlannerStore(fake.api); let finishOld!: (page: RoutePage) => void; let calls = 0;
  fake.setList(async ({ limit, offset }) => {
    calls++;
    if (calls === 1) return new Promise((resolve) => { finishOld = resolve; });
    return { items: [summary(detail('created', 1, 'New')), summary(detail())], total: 2, limit, offset };
  });
  const stalePage = store.loadRoutes(); await tick(); validDraft(store); await store.preview(); await store.saveRoute(); await tick();
  assert.equal(store.getState().routesLoading, false); assert.equal(store.getState().routesTotal, 2);
  finishOld({ items: [], total: 0, limit: 20, offset: 0 }); await stalePage;
  assert.equal(store.getState().routesLoading, false); assert.equal(store.getState().routesTotal, 2);
});

test('account reset fences delayed save and open responses', async () => {
  const fake = fakeApi(); const store = new RoutePlannerStore(fake.api); let finish!: (value: RouteDetail) => void;
  validDraft(store); await store.preview(); fake.setCreate(async () => new Promise((resolve) => { finish = resolve; }));
  const saving = store.saveRoute(); await tick(); store.reset(); finish(detail('private-a')); await saving;
  assert.equal(store.getState().routes.length, 0); assert.equal(store.getState().editingRouteId, null);
  const opening = store.openRoute('a'); store.reset(); await opening; assert.equal(store.getState().selectedRoute, null);
});

test('old account delete cannot clear a new account mutation or selection', async () => {
  const fake = fakeApi(); const store = new RoutePlannerStore(fake.api); await store.openRoute('route-1');
  let finishOld!: () => void; let finishNew!: () => void; let removals = 0;
  fake.setRemove(async () => new Promise<void>((resolve) => { if (removals++ === 0) finishOld = resolve; else finishNew = resolve; }));
  const oldDelete = store.deleteRoute(); await tick(); store.reset(); await store.openRoute('route-1');
  const newDelete = store.deleteRoute(); await tick(); finishOld(); await oldDelete;
  assert.equal(store.getState().deleting, true); assert.equal(store.getState().editingRouteId, 'route-1');
  finishNew(); assert.equal(await newDelete, true); assert.equal(store.getState().deleting, false);
  assert.equal(store.getState().editingRouteId, null);
});

test('old account GPX response cannot return private text or clear new export progress', async () => {
  const fake = fakeApi(); const store = new RoutePlannerStore(fake.api); await store.openRoute('route-1');
  let finishOld!: (text: string) => void; let finishNew!: (text: string) => void; let exports = 0;
  fake.setExport(async () => new Promise((resolve) => { if (exports++ === 0) finishOld = resolve; else finishNew = resolve; }));
  const oldExport = store.exportGpx(); await tick(); store.reset(); await store.openRoute('route-1');
  const newExport = store.exportGpx(); await tick(); finishOld('<gpx>private A</gpx>');
  assert.equal(await oldExport, null); assert.equal(store.getState().exporting, true);
  finishNew('<gpx>account B</gpx>'); assert.equal(await newExport, '<gpx>account B</gpx>');
  assert.equal(store.getState().exporting, false);
});

test('delete and export failures remain visible and do not silently mutate the draft', async () => {
  const fake = fakeApi(); const store = new RoutePlannerStore(fake.api); await store.openRoute('route-1');
  fake.setExport(async () => { throw new Error('network down'); }); assert.equal(await store.exportGpx(), null);
  assert.equal(store.getState().exportError, 'network down');
  fake.setRemove(async () => { throw new Error('revision conflict'); }); assert.equal(await store.deleteRoute(), false);
  assert.equal(store.getState().deleteError, 'revision conflict'); assert.equal(store.getState().editingRouteId, 'route-1');
});
