import type { RouteDetail, RoutePage, RoutePreviewResponse, RouteSummary } from './api/generated';
import type { RouteApi } from './api/routes';

export type RouteWaypoint = [number, number];
export type RouteLoadState = 'idle' | 'loading' | 'ready' | 'error';

export interface RoutePlannerState {
  name: string;
  waypoints: RouteWaypoint[];
  draftRevision: number;
  undoAvailable: boolean;
  draftError: string | null;
  preview: RoutePreviewResponse | null;
  previewStatus: RouteLoadState;
  previewError: string | null;
  routes: RouteSummary[];
  routesTotal: number;
  routesOffset: number;
  routesLoading: boolean;
  routesError: string | null;
  editingRouteId: string | null;
  expectedRevision: number | null;
  selectedRoute: RouteDetail | null;
  selectedRouteLoading: boolean;
  selectedRouteError: string | null;
  saving: boolean;
  saveError: string | null;
  deleting: boolean;
  deleteError: string | null;
  exporting: boolean;
  exportError: string | null;
}

const PAGE_SIZE = 20;
const INITIAL: RoutePlannerState = {
  name: '', waypoints: [], draftRevision: 0, undoAvailable: false, draftError: null,
  preview: null, previewStatus: 'idle', previewError: null,
  routes: [], routesTotal: 0, routesOffset: 0, routesLoading: false, routesError: null,
  editingRouteId: null, expectedRevision: null, selectedRoute: null, selectedRouteLoading: false, selectedRouteError: null,
  saving: false, saveError: null, deleting: false, deleteError: null, exporting: false, exportError: null,
};
type Draft = Pick<RoutePlannerState, 'name' | 'waypoints'>;

function validWaypoint(point: RouteWaypoint): boolean {
  return Number.isFinite(point[0]) && Number.isFinite(point[1]) && point[0] >= -180 && point[0] <= 180 && point[1] >= -90 && point[1] <= 90;
}
function routePreview(route: RouteDetail, clientRevision: number): RoutePreviewResponse {
  return { client_revision: clientRevision, geometry: route.geometry, distance_m: route.distance_m,
    duration_s: route.duration_s, provider: route.provider, attribution: route.attribution };
}
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : 'Route request failed. Retry when connected.'; }

/** Shared route draft and saved-route controller. Preview calls are always explicit. */
export class RoutePlannerStore {
  private state = INITIAL;
  private accountRevision = 0;
  private draftSessionRevision = 0;
  private routeListGeneration = 0;
  private routeOpenGeneration = 0;
  private previewGeneration = 0;
  private exportGeneration = 0;
  private mutationGeneration = 0;
  private mutationOwner: number | null = null;
  private accountController = new AbortController();
  private previewController: AbortController | null = null;
  private undoSnapshot: Draft | null = null;
  private listeners = new Set<(state: RoutePlannerState) => void>();

  constructor(private readonly api: RouteApi) {}

  getState(): RoutePlannerState { return this.state; }
  getAccountGeneration(): number { return this.accountRevision; }
  subscribe(listener: (state: RoutePlannerState) => void): () => void { this.listeners.add(listener); return () => this.listeners.delete(listener); }

  private update(patch: Partial<RoutePlannerState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener(this.state);
  }

  reset(): void {
    this.accountRevision++;
    this.accountController.abort(); this.accountController = new AbortController();
    this.cancelPreview(); this.routeListGeneration++; this.routeOpenGeneration++; this.exportGeneration++; this.mutationGeneration++;
    this.mutationOwner = null; this.undoSnapshot = null; this.draftSessionRevision++;
    this.update({ ...INITIAL });
  }

  setName(name: string): void { if (name !== this.state.name) this.editDraft({ name }); }

  addWaypoint(point: RouteWaypoint, index = this.state.waypoints.length): boolean {
    if (!validWaypoint(point)) { this.update({ draftError: 'Waypoint coordinates must be finite WGS84 longitude and latitude.' }); return false; }
    if (this.state.waypoints.length >= 20) { this.update({ draftError: 'A route can contain at most 20 waypoints.' }); return false; }
    if (!Number.isInteger(index) || index < 0 || index > this.state.waypoints.length) return false;
    const waypoints = this.state.waypoints.map((item) => [...item] as RouteWaypoint); waypoints.splice(index, 0, [...point]);
    this.editDraft({ waypoints }); return true;
  }

  moveWaypoint(index: number, point: RouteWaypoint): boolean {
    if (!validWaypoint(point)) { this.update({ draftError: 'Waypoint coordinates must be finite WGS84 longitude and latitude.' }); return false; }
    if (!Number.isInteger(index) || index < 0 || index >= this.state.waypoints.length) return false;
    const waypoints = this.state.waypoints.map((item) => [...item] as RouteWaypoint); waypoints[index] = [...point];
    this.editDraft({ waypoints }); return true;
  }

  removeWaypoint(index: number): boolean {
    if (!Number.isInteger(index) || index < 0 || index >= this.state.waypoints.length) return false;
    const waypoints = this.state.waypoints.map((item) => [...item] as RouteWaypoint); waypoints.splice(index, 1);
    this.editDraft({ waypoints }); return true;
  }

  reorderWaypoint(from: number, to: number): boolean {
    if (!Number.isInteger(from) || !Number.isInteger(to) || from < 0 || to < 0 || from >= this.state.waypoints.length || to >= this.state.waypoints.length) return false;
    if (from === to) return true;
    const waypoints = this.state.waypoints.map((item) => [...item] as RouteWaypoint); const [point] = waypoints.splice(from, 1);
    if (!point) return false;
    waypoints.splice(to, 0, point); this.editDraft({ waypoints }); return true;
  }

  undo(): boolean {
    if (!this.undoSnapshot) return false;
    const draft = this.undoSnapshot; this.undoSnapshot = null;
    this.invalidatePreview();
    this.update({ ...draft, waypoints: draft.waypoints.map((point) => [...point]), draftRevision: this.state.draftRevision + 1,
      undoAvailable: false, draftError: null, preview: null, previewStatus: 'idle', previewError: null, saveError: null });
    return true;
  }

  newDraft(): void {
    this.cancelPreview(); this.routeOpenGeneration++; this.draftSessionRevision++; this.undoSnapshot = null;
    this.update({ name: '', waypoints: [], draftRevision: this.state.draftRevision + 1, undoAvailable: false, draftError: null,
      preview: null, previewStatus: 'idle', previewError: null, editingRouteId: null, expectedRevision: null, selectedRoute: null,
      selectedRouteLoading: false, selectedRouteError: null, saveError: null, deleteError: null, exportError: null });
  }

  async preview(): Promise<void> {
    const validationError = this.validateWaypoints();
    if (validationError) { this.update({ draftError: validationError, preview: null, previewStatus: 'error', previewError: validationError }); return; }
    this.cancelPreview();
    const accountRevision = this.accountRevision; const draftRevision = this.state.draftRevision;
    const generation = ++this.previewGeneration; const controller = new AbortController(); this.previewController = controller;
    const body = { client_revision: draftRevision, waypoints: this.copyWaypoints(this.state.waypoints) };
    this.update({ draftError: null, preview: null, previewStatus: 'loading', previewError: null });
    try {
      const result = await this.api.preview(body, controller.signal);
      if (accountRevision !== this.accountRevision || draftRevision !== this.state.draftRevision || generation !== this.previewGeneration || result.client_revision !== draftRevision) return;
      this.update({ preview: result, previewStatus: 'ready' });
    } catch (error) {
      if (accountRevision === this.accountRevision && draftRevision === this.state.draftRevision && generation === this.previewGeneration) {
        this.update({ previewStatus: 'error', previewError: errorMessage(error) });
      }
    } finally { if (this.previewController === controller) this.previewController = null; }
  }

  async loadRoutes(): Promise<void> { await this.readRoutes(0, false); }
  async loadMoreRoutes(): Promise<void> {
    if (this.state.routesLoading || this.state.routes.length >= this.state.routesTotal) return;
    await this.readRoutes(this.state.routesOffset, true);
  }

  async openRoute(id: string): Promise<void> {
    if (this.mutationOwner !== null) return;
    const accountRevision = this.accountRevision; const draftRevision = this.state.draftRevision;
    const generation = ++this.routeOpenGeneration; const session = this.draftSessionRevision;
    this.update({ selectedRouteLoading: true, selectedRouteError: null });
    try {
      const route = await this.api.get(id, this.accountController.signal);
      if (accountRevision !== this.accountRevision || generation !== this.routeOpenGeneration || session !== this.draftSessionRevision || draftRevision !== this.state.draftRevision) return;
      this.cancelPreview(); this.draftSessionRevision++; this.undoSnapshot = null;
      const nextRevision = this.state.draftRevision + 1;
      this.update({ name: route.name, waypoints: this.copyWaypoints(route.waypoints), draftRevision: nextRevision,
        undoAvailable: false, draftError: null, preview: routePreview(route, nextRevision), previewStatus: 'ready', previewError: null,
        editingRouteId: route.id, expectedRevision: route.revision, selectedRoute: route, selectedRouteLoading: false, selectedRouteError: null,
        saveError: null, deleteError: null, exportError: null });
    } catch (error) {
      if (accountRevision === this.accountRevision && generation === this.routeOpenGeneration) this.update({ selectedRouteLoading: false, selectedRouteError: errorMessage(error) });
    } finally {
      if (accountRevision === this.accountRevision && generation === this.routeOpenGeneration && draftRevision !== this.state.draftRevision) {
        this.update({ selectedRouteLoading: false });
      }
    }
  }

  async saveRoute(): Promise<RouteDetail | null> {
    const validationError = this.validateDraft();
    if (validationError) { this.update({ draftError: validationError, saveError: validationError }); return null; }
    if (!this.state.preview || this.state.previewStatus !== 'ready' || this.state.preview.client_revision !== this.state.draftRevision) {
      this.update({ saveError: 'Preview the current waypoints before saving.' }); return null;
    }
    if (this.mutationOwner !== null) return null;
    this.routeOpenGeneration++;
    this.update({ selectedRouteLoading: false });
    const owner = ++this.mutationGeneration; this.mutationOwner = owner;
    const accountRevision = this.accountRevision; const draftRevision = this.state.draftRevision; const session = this.draftSessionRevision;
    const routeId = this.state.editingRouteId; const expectedRevision = this.state.expectedRevision;
    const body = { name: this.state.name.trim(), waypoints: this.copyWaypoints(this.state.waypoints) };
    this.update({ saving: true, saveError: null, draftError: null });
    try {
      const route = routeId === null
        ? await this.api.create(body, this.accountController.signal)
        : await this.api.update(routeId, { ...body, expected_revision: expectedRevision! }, this.accountController.signal);
      if (accountRevision !== this.accountRevision) return null;
      this.upsertSummary(route);
      this.refreshRouteList();
      if (session === this.draftSessionRevision && this.state.editingRouteId === routeId) {
        const sameDraft = draftRevision === this.state.draftRevision;
        const revision = this.state.draftRevision;
        this.update({ editingRouteId: route.id, expectedRevision: route.revision, selectedRoute: route,
          ...(sameDraft ? { preview: routePreview(route, revision), previewStatus: 'ready' as const, previewError: null } : {}) });
      }
      return route;
    } catch (error) {
      if (accountRevision === this.accountRevision && session === this.draftSessionRevision) this.update({ saveError: errorMessage(error) });
      return null;
    } finally {
      if (this.mutationOwner === owner) this.mutationOwner = null;
      if (accountRevision === this.accountRevision) this.update({ saving: false });
    }
  }

  async deleteRoute(): Promise<boolean> {
    const id = this.state.editingRouteId; const revision = this.state.expectedRevision;
    if (!id || revision === null || this.mutationOwner !== null) return false;
    this.routeOpenGeneration++;
    this.update({ selectedRouteLoading: false });
    const owner = ++this.mutationGeneration; this.mutationOwner = owner;
    const accountRevision = this.accountRevision; const session = this.draftSessionRevision;
    this.update({ deleting: true, deleteError: null });
    try {
      await this.api.delete(id, revision, this.accountController.signal);
      if (accountRevision !== this.accountRevision) return false;
      this.update({ routes: this.state.routes.filter((route) => route.id !== id), routesTotal: Math.max(0, this.state.routesTotal - 1) });
      this.refreshRouteList();
      if (this.state.editingRouteId === id) this.newDraft();
      return true;
    } catch (error) {
      if (accountRevision === this.accountRevision && session === this.draftSessionRevision) this.update({ deleteError: errorMessage(error) });
      return false;
    } finally {
      if (this.mutationOwner === owner) this.mutationOwner = null;
      if (accountRevision === this.accountRevision) this.update({ deleting: false });
    }
  }

  async exportGpx(): Promise<string | null> {
    const id = this.state.editingRouteId; if (!id) return null;
    const accountRevision = this.accountRevision; const session = this.draftSessionRevision;
    const generation = ++this.exportGeneration;
    this.update({ exporting: true, exportError: null });
    try {
      const text = await this.api.exportGpx(id, this.accountController.signal);
      if (accountRevision !== this.accountRevision || session !== this.draftSessionRevision || generation !== this.exportGeneration || id !== this.state.editingRouteId) return null;
      return text;
    } catch (error) {
      if (accountRevision === this.accountRevision && session === this.draftSessionRevision && generation === this.exportGeneration) this.update({ exportError: errorMessage(error) });
      return null;
    } finally { if (accountRevision === this.accountRevision && generation === this.exportGeneration) this.update({ exporting: false }); }
  }

  private async readRoutes(offset: number, append: boolean): Promise<void> {
    const accountRevision = this.accountRevision; const generation = ++this.routeListGeneration;
    this.update({ routesLoading: true, routesError: null });
    try {
      const result: RoutePage = await this.api.list({ limit: PAGE_SIZE, offset }, this.accountController.signal);
      if (accountRevision !== this.accountRevision || generation !== this.routeListGeneration) return;
      const routes = append ? [...this.state.routes, ...result.items.filter((item) => !this.state.routes.some((route) => route.id === item.id))] : result.items;
      this.update({ routes, routesTotal: result.total, routesOffset: result.offset + result.items.length, routesLoading: false });
    } catch (error) {
      if (accountRevision === this.accountRevision && generation === this.routeListGeneration) this.update({ routesLoading: false, routesError: errorMessage(error) });
    }
  }

  private refreshRouteList(): void {
    this.routeListGeneration++;
    this.update({ routesLoading: false });
    void this.readRoutes(0, false);
  }

  private editDraft(next: Partial<Draft>): void {
    this.undoSnapshot = { name: this.state.name, waypoints: this.copyWaypoints(this.state.waypoints) };
    this.invalidatePreview();
    this.update({ ...next, draftRevision: this.state.draftRevision + 1, undoAvailable: true, draftError: null,
      preview: null, previewStatus: 'idle', previewError: null, saveError: null });
  }

  private invalidatePreview(): void { this.cancelPreview(); this.previewGeneration++; }
  private cancelPreview(): void { this.previewController?.abort(); this.previewController = null; }
  private validateDraft(): string | null {
    const nameError = this.validateName();
    return nameError ?? this.validateWaypoints();
  }
  private validateName(): string | null {
    const name = this.state.name.trim();
    if (!name || name.length > 100) return 'Route name must contain 1–100 characters.';
    return null;
  }
  private validateWaypoints(): string | null {
    if (this.state.waypoints.length < 2) return 'Add at least two waypoints before previewing or saving.';
    if (this.state.waypoints.length > 20 || this.state.waypoints.some((point) => !validWaypoint(point))) return 'Route waypoints must be valid WGS84 coordinates (2–20 points).';
    return null;
  }
  private copyWaypoints(points: RouteWaypoint[]): RouteWaypoint[] { return points.map((point) => [...point] as RouteWaypoint); }
  private upsertSummary(route: RouteDetail): void {
    const summary: RouteSummary = { id: route.id, name: route.name, revision: route.revision, distance_m: route.distance_m,
      created_at: route.created_at, updated_at: route.updated_at };
    const exists = this.state.routes.some((item) => item.id === route.id);
    this.update({ routes: exists ? this.state.routes.map((item) => item.id === route.id ? summary : item) : [summary, ...this.state.routes],
      routesTotal: exists ? this.state.routesTotal : this.state.routesTotal + 1 });
  }
}
