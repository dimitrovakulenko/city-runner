import { useEffect, useRef, useState } from 'react';
import { Map as MapLibre, NavigationControl, AttributionControl, LngLatBounds, setWorkerUrl } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import type { GeoJSONSource, MapGeoJSONFeature } from 'maplibre-gl';
import type { FeatureCollection, Feature, Geometry } from 'geojson';
import type { ActivityDetail } from '../../mobile/src/api/generated';
import type { Runtime } from './runtime';
import { useStore } from './hooks';
import { Icon } from './icons';
import 'maplibre-gl/dist/maplibre-gl.css';

const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] };
export function ExploreMap({ runtime, enabled, selected, focus, onStreet }: { runtime: Runtime; enabled: boolean; selected: ActivityDetail | null; focus: [number, number] | null; onStreet: (feature: MapGeoJSONFeature) => void }) {
  const host = useRef<HTMLDivElement>(null); const mapRef = useRef<MapLibre | null>(null);
  const enabledRef = useRef(enabled); enabledRef.current = enabled;
  const streetRef = useRef(onStreet); streetRef.current = onStreet;
  const [ready, setReady] = useState(false); const [error, setError] = useState<string | null>(null);
  const [showTracks, setShowTracks] = useState(true); const [showMissing, setShowMissing] = useState(false);
  const state = useStore(runtime.explore);
  useEffect(() => {
    if (!host.current) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let map: MapLibre;
    try {
      setWorkerUrl(workerUrl);
      map = new MapLibre({ container: host.current, style: import.meta.env.VITE_MAP_STYLE ?? 'https://tiles.openfreemap.org/styles/liberty',
        center: [3.724, 51.054], zoom: 14.8, minZoom: 9, maxZoom: 19, renderWorldCopies: false, attributionControl: false });
    } catch (error) { if (import.meta.env.DEV) console.warn('Map initialization failed:', error); setError('Your browser could not start the map. Try a browser with WebGL enabled.'); return; }
    mapRef.current = map;
    map.addControl(new NavigationControl({ showCompass: false }), 'bottom-right');
    map.addControl(new AttributionControl({ compact: false }), 'bottom-right');
    const refresh = () => {
      if (!enabledRef.current) return;
      const b = map.getBounds();
      void runtime.explore.refreshViewport({ bbox: [Math.max(-180, b.getWest()), Math.max(-90, b.getSouth()), Math.min(180, b.getEast()), Math.min(90, b.getNorth())], zoom: map.getZoom() });
    };
    map.on('load', () => {
      for (const id of ['streets', 'tracks', 'missing', 'selected']) map.addSource(id, { type: 'geojson', data: EMPTY });
      map.addLayer({ id: 'street-outline', type: 'line', source: 'streets', paint: { 'line-color': '#fff', 'line-width': 4, 'line-opacity': 0.55 } });
      map.addLayer({ id: 'street-coverage', type: 'line', source: 'streets', paint: { 'line-color': ['case', ['==', ['get', 'complete'], true], '#21856e', ['==', ['get', 'known'], false], '#9ca89d', '#dda257'], 'line-width': ['case', ['==', ['get', 'complete'], true], 3, 1.8], 'line-opacity': ['case', ['==', ['get', 'complete'], true], 0.9, 0.55] } });
      map.addLayer({ id: 'activity-track-outline', type: 'line', source: 'tracks', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#fff', 'line-width': 8, 'line-opacity': 0.95 } });
      map.addLayer({ id: 'activity-tracks', type: 'line', source: 'tracks', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#7c3aed', 'line-width': 5, 'line-opacity': 1 } });
      map.addLayer({ id: 'missing-nodes', type: 'circle', source: 'missing', minzoom: 17, layout: { visibility: 'none' }, paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 17, 2, 19, 3.5], 'circle-color': '#e29f4b', 'circle-stroke-color': '#fff', 'circle-stroke-width': 1 } });
      map.addLayer({ id: 'selected-track-outline', type: 'line', source: 'selected', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#fff', 'line-width': 11 } });
      map.addLayer({ id: 'selected-track', type: 'line', source: 'selected', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#4c1d95', 'line-width': 7 } });
      map.on('click', 'street-coverage', (event) => { if (enabledRef.current && event.features?.[0]) streetRef.current(event.features[0]); });
      map.on('mouseenter', 'street-coverage', () => { map.getCanvas().style.cursor = 'pointer'; });
      map.on('mouseleave', 'street-coverage', () => { map.getCanvas().style.cursor = ''; });
      setReady(true); refresh();
    });
    map.on('movestart', () => { if (enabledRef.current) runtime.explore.invalidateViewport(); });
    map.on('moveend', () => { clearTimeout(timer); timer = setTimeout(refresh, 150); });
    map.on('error', () => setError('Some map tiles could not load. Check your connection.'));
    return () => { clearTimeout(timer); map.remove(); mapRef.current = null; };
  }, [runtime]);
  useEffect(() => {
    const map = mapRef.current; if (!ready || !map) return;
    const snapshot = enabled ? state.map : null;
    const feature = (geometry: Record<string, unknown>, properties: Record<string, unknown>): Feature => ({ type: 'Feature', geometry: geometry as unknown as Geometry, properties });
    const set = (id: string, features: Feature[]) => (map.getSource(id) as GeoJSONSource).setData({ type: 'FeatureCollection', features });
    set('streets', snapshot?.streets.map((street) => feature(street.geometry, { id: street.street_id, city_id: street.city_id, dataset_id: street.dataset_id, name: street.name, complete: street.effective_completed, known: street.effective_completed !== null })) ?? []);
    set('tracks', snapshot?.tracks.map((track) => feature(track.geometry, { id: track.activity_id })) ?? []);
    set('missing', snapshot?.missing_nodes.map((node) => ({ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [node.longitude, node.latitude] } })) ?? []);
  }, [state.map, ready, enabled]);
  useEffect(() => {
    const map = mapRef.current; if (!ready || !map) return;
    (map.getSource('selected') as GeoJSONSource).setData(enabled && selected ? { type: 'FeatureCollection', features: selected.tracks.filter((segment) => segment.length > 1).map((segment) => ({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: segment } })) } : EMPTY);
    if (enabled && selected?.bounds) map.fitBounds(new LngLatBounds(selected.bounds), { padding: 80, maxZoom: 17, duration: 700 });
  }, [selected, ready, enabled]);
  useEffect(() => {
    const map = mapRef.current; if (!ready || !map || !enabled) return;
    const b = map.getBounds(); void runtime.explore.refreshViewport({ bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], zoom: map.getZoom() });
  }, [enabled, ready, runtime]);
  useEffect(() => { const map = mapRef.current; if (ready && map) { for (const layer of ['activity-track-outline', 'activity-tracks']) map.setLayoutProperty(layer, 'visibility', showTracks ? 'visible' : 'none'); map.setLayoutProperty('missing-nodes', 'visibility', showMissing ? 'visible' : 'none'); } }, [ready, showTracks, showMissing]);
  useEffect(() => { if (ready && enabled && focus) { setShowMissing(true); mapRef.current?.flyTo({ center: focus, zoom: 18, duration: 700 }); } }, [ready, enabled, focus]);
  return <section className="map-area" aria-label="Exploration map" data-ready={ready}><div ref={host} className="map-canvas" />
    <div className="map-heading"><span className="live-dot" /><span>Your exploration map</span><span className="map-heading-divider" />{enabled ? 'Lifetime coverage' : 'A new street is a new story'}</div>
    {enabled && <div className="map-layers"><Icon name="layers" size={17} /><label><input type="checkbox" checked={showTracks} onChange={(event) => setShowTracks(event.target.checked)} />Activity tracks</label><label title="Zoom close to inspect individual missing GPS nodes"><input type="checkbox" checked={showMissing} onChange={(event) => setShowMissing(event.target.checked)} />Missing nodes</label></div>}
    <div className="map-legend"><span><i className="legend-line completed" />Completed</span><span><i className="legend-line remaining" />Remaining</span><span><i className="legend-line track" />Activity</span></div>
    {enabled && <div className="map-message" role="status">{state.mapStatus === 'loading' ? 'Updating this view…' : state.mapError ?? (state.map?.geography_state === 'geography_pending' ? 'Street coverage is not available in this area yet.' : showMissing && (state.viewport?.zoom ?? 0) < 17 ? 'Zoom closer to see individual missing nodes.' : 'Coverage uses your original GPS samples.')}{state.map && (state.map.dataset_truncated || Object.entries(state.map.limits).some(([layer, limit]) => (layer !== 'missing_nodes' || showMissing && (state.viewport?.zoom ?? 0) >= 17) && limit.truncated)) && ' Some results are limited; zoom in.'}</div>}
    {error && <div className="map-error" role="alert">{error}<button aria-label="Dismiss map message" onClick={() => setError(null)}><Icon name="close" size={15} /></button></div>}
  </section>;
}
