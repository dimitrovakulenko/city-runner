import { useEffect, useRef, useState } from 'react';
import { Map as MapLibre, Marker, NavigationControl, AttributionControl, LngLatBounds, setWorkerUrl } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import type { GeoJSONSource, MapGeoJSONFeature } from 'maplibre-gl';
import type { FeatureCollection, Feature, Geometry } from 'geojson';
import type { ActivityDetail } from '../../mobile/src/api/generated';
import type { Runtime } from './runtime';
import { useStore } from './hooks';
import { Icon } from './icons';
import 'maplibre-gl/dist/maplibre-gl.css';

const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] };
export function ExploreMap({ runtime, enabled, planning, selected, focus, onStreet }: { runtime: Runtime; enabled: boolean; planning: boolean; selected: ActivityDetail | null; focus: [number, number] | null; onStreet: (feature: MapGeoJSONFeature) => void }) {
  const host = useRef<HTMLDivElement>(null); const mapRef = useRef<MapLibre | null>(null);
  const enabledRef = useRef(enabled); enabledRef.current = enabled;
  const planningRef = useRef(planning); planningRef.current = planning;
  const streetRef = useRef(onStreet); streetRef.current = onStreet;
  const [ready, setReady] = useState(false); const [error, setError] = useState<string | null>(null);
  const [showTracks, setShowTracks] = useState(true); const [showMissing, setShowMissing] = useState(false);
  const state = useStore(runtime.explore);
  const planner = useStore(runtime.planner);
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
      for (const id of ['streets', 'tracks', 'missing', 'selected', 'plan']) map.addSource(id, { type: 'geojson', data: EMPTY });
      map.addLayer({ id: 'street-outline', type: 'line', source: 'streets', paint: { 'line-color': '#fff', 'line-width': 4, 'line-opacity': 0.55 } });
      map.addLayer({ id: 'street-coverage', type: 'line', source: 'streets', paint: { 'line-color': ['case', ['==', ['get', 'complete'], true], '#21856e', ['==', ['get', 'known'], false], '#9ca89d', '#dda257'], 'line-width': ['case', ['==', ['get', 'complete'], true], 3, 1.8], 'line-opacity': ['case', ['==', ['get', 'complete'], true], 0.9, 0.55] } });
      map.addLayer({ id: 'activity-track-outline', type: 'line', source: 'tracks', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#fff', 'line-width': 8, 'line-opacity': 0.95 } });
      map.addLayer({ id: 'activity-tracks', type: 'line', source: 'tracks', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#7c3aed', 'line-width': 5, 'line-opacity': 1 } });
      map.addLayer({ id: 'missing-nodes', type: 'circle', source: 'missing', minzoom: 17, layout: { visibility: 'none' }, paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 17, 2, 19, 3.5], 'circle-color': '#e29f4b', 'circle-stroke-color': '#fff', 'circle-stroke-width': 1 } });
      map.addLayer({ id: 'selected-track-outline', type: 'line', source: 'selected', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#fff', 'line-width': 11 } });
      map.addLayer({ id: 'selected-track', type: 'line', source: 'selected', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#4c1d95', 'line-width': 7 } });
      map.addLayer({ id: 'planned-route-outline', type: 'line', source: 'plan', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#fff', 'line-width': 10 } });
      map.addLayer({ id: 'planned-route', type: 'line', source: 'plan', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#176bb3', 'line-width': 6 } });
      map.on('click', 'street-coverage', (event) => { if (enabledRef.current && !planningRef.current && event.features?.[0]) streetRef.current(event.features[0]); });
      map.on('click', (event) => { if (enabledRef.current && planningRef.current) runtime.planner.addWaypoint([event.lngLat.lng, event.lngLat.lat]); });
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
    const map = mapRef.current; if (!ready || !map) return;
    (map.getSource('plan') as GeoJSONSource).setData(enabled && planning && planner.preview ? { type: 'FeatureCollection', features: [{ type: 'Feature', properties: {}, geometry: planner.preview.geometry }] } : EMPTY);
  }, [planner.preview, planning, ready, enabled]);
  useEffect(() => {
    const map = mapRef.current; if (!ready || !map || !enabled || !planning) return;
    const markers = planner.waypoints.map((point, index) => {
      const element = document.createElement('button'); element.type = 'button'; element.className = 'planner-marker'; element.textContent = String(index + 1); element.setAttribute('aria-label', `Waypoint ${index + 1}`); element.title = `Drag waypoint ${index + 1}`;
      element.addEventListener('click', (event) => event.stopPropagation());
      const marker = new Marker({ element, draggable: true }).setLngLat(point).addTo(map);
      marker.on('dragend', () => { const next = marker.getLngLat(); runtime.planner.moveWaypoint(index, [next.lng, next.lat]); });
      return marker;
    });
    return () => { for (const marker of markers) marker.remove(); };
  }, [planner.waypoints, planning, ready, enabled, runtime]);
  useEffect(() => {
    const map = mapRef.current; if (!ready || !map || !enabled || !planning || !planner.selectedRoute) return;
    const bounds = new LngLatBounds(); for (const point of planner.selectedRoute.geometry.coordinates) bounds.extend(point);
    map.fitBounds(bounds, { padding: 80, maxZoom: 17, duration: 700 });
  }, [planner.selectedRoute, ready, enabled, planning]);
  useEffect(() => {
    const map = mapRef.current; if (!ready || !map || !enabled) return;
    const b = map.getBounds(); void runtime.explore.refreshViewport({ bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], zoom: map.getZoom() });
  }, [enabled, ready, runtime]);
  useEffect(() => { const map = mapRef.current; if (ready && map) { for (const layer of ['activity-track-outline', 'activity-tracks']) map.setLayoutProperty(layer, 'visibility', showTracks ? 'visible' : 'none'); map.setLayoutProperty('missing-nodes', 'visibility', showMissing ? 'visible' : 'none'); } }, [ready, showTracks, showMissing]);
  useEffect(() => { if (ready && enabled && focus) { setShowMissing(true); mapRef.current?.flyTo({ center: focus, zoom: 18, duration: 700 }); } }, [ready, enabled, focus]);
  return <section className="map-area" aria-label="Exploration map" data-ready={ready}><div ref={host} className="map-canvas" />
    <div className="map-heading"><span className="live-dot" /><span>{planning && enabled ? 'Click the map to add waypoints' : 'Your exploration map'}</span><span className="map-heading-divider" />{enabled ? planning ? 'Walking route planner' : 'Lifetime coverage' : 'A new street is a new story'}</div>
    {enabled && <div className="map-layers"><Icon name="layers" size={17} /><label><input type="checkbox" checked={showTracks} onChange={(event) => setShowTracks(event.target.checked)} />Activity tracks</label><label title="Zoom close to inspect individual missing GPS nodes"><input type="checkbox" checked={showMissing} onChange={(event) => setShowMissing(event.target.checked)} />Missing nodes</label></div>}
    <div className="map-legend"><span><i className="legend-line completed" />Completed</span><span><i className="legend-line remaining" />Remaining</span><span><i className="legend-line track" />Activity</span>{planning && enabled && <span><i className="legend-line plan" />Planned</span>}</div>
    {enabled && <div className="map-message" role="status">{state.mapStatus === 'loading' ? 'Updating this view…' : state.mapError ?? (state.map?.geography_state === 'geography_pending' ? 'Street coverage is not available in this area yet.' : showMissing && (state.viewport?.zoom ?? 0) < 17 ? 'Zoom closer to see individual missing nodes.' : 'Coverage uses your original GPS samples.')}{state.map && (state.map.dataset_truncated || Object.entries(state.map.limits).some(([layer, limit]) => (layer !== 'missing_nodes' || showMissing && (state.viewport?.zoom ?? 0) >= 17) && limit.truncated)) && ' Some results are limited; zoom in.'}</div>}
    {error && <div className="map-error" role="alert">{error}<button aria-label="Dismiss map message" onClick={() => setError(null)}><Icon name="close" size={15} /></button></div>}
  </section>;
}
