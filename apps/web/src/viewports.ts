export interface MapPosition { center: [number, number]; zoom: number }
export const DEFAULT_POSITION: MapPosition = { center: [3.724, 51.054], zoom: 14.8 };

function valid(value: unknown): value is MapPosition {
  const point = value as MapPosition | null;
  return Boolean(point && Array.isArray(point.center) && point.center.length === 2 &&
    point.center.every((coordinate) => typeof coordinate === 'number' && Number.isFinite(coordinate)) &&
    Math.abs(point.center[0]) <= 180 && Math.abs(point.center[1]) <= 85 &&
    typeof point.zoom === 'number' && Number.isFinite(point.zoom) && point.zoom >= 9 && point.zoom <= 19);
}

/** Only account-scoped center/zoom preferences; no tracks, sessions or progress. */
export class BrowserViewports {
  constructor(private readonly storage: Pick<Storage, 'getItem' | 'setItem'> & Partial<Pick<Storage, 'removeItem'>>) {}
  read(accountId: string): MapPosition {
    try { const value: unknown = JSON.parse(this.storage.getItem(this.key(accountId)) ?? 'null'); return valid(value) ? value : DEFAULT_POSITION; }
    catch { return DEFAULT_POSITION; }
  }
  write(accountId: string, value: MapPosition): void {
    if (!valid(value)) return;
    try { this.storage.setItem(this.key(accountId), JSON.stringify({ center: value.center, zoom: value.zoom })); } catch { /* Storage can be denied or full. */ }
  }
  forget(accountId: string): void { try { this.storage.removeItem?.(this.key(accountId)); } catch { /* Browser storage may be unavailable. */ } }
  private key(accountId: string) { return `city-runner.viewport.${encodeURIComponent(accountId)}`; }
}
