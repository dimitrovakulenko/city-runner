import { useEffect, useRef, useState } from 'react';
import { Map as MapLibre, Marker, NavigationControl, AttributionControl, LngLatBounds, setWorkerUrl } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import type { GeoJSONSource, MapGeoJSONFeature } from 'maplibre-gl';
import type { FeatureCollection, Feature, Geometry } from 'geojson';
import type { ActivityDetail, ActivityImpactStreet, MissingNode } from '../../mobile/src/api/generated';
import type { Runtime } from './runtime';
import { useStore } from './hooks';
import { Icon } from './icons';
import { BrowserViewports, DEFAULT_POSITION } from './viewports';
import 'maplibre-gl/dist/maplibre-gl.css';

const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] };
export function ExploreMap({ runtime, enabled, accountId, planning, selected, selectedStreet, streetFocusRequest, focus, onStreet, showMissing, onMissingChange, selectedNode, onNode, nodeZoomRequest }: { runtime: Runtime; enabled: boolean; accountId: string | null; planning: boolean; selected: ActivityDetail | null; selectedStreet: ActivityImpactStreet | null; streetFocusRequest: number; focus: [number, number] | null; onStreet: (feature: MapGeoJSONFeature) => void; showMissing: boolean; onMissingChange: (show: boolean) => void; selectedNode: MissingNode | null; onNode: (node: MissingNode) => void; nodeZoomRequest: number }) {
  const host = useRef<HTMLDivElement>(null); const mapRef = useRef<MapLibre | null>(null);
  const enabledRef = useRef(enabled); enabledRef.current = enabled;
  const accountRef = useRef(accountId); accountRef.current = accountId;
  const restoredAccount = useRef<string | null>(null); const positions = useRef<BrowserViewports | null>(null);
  const restoredGeneration = useRef<number | null>(null);
  const locationRequest = useRef(0); const locationTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [locating, setLocating] = useState(false); const [locationError, setLocationError] = useState<string | null>(null);
  const [location, setLocation] = useState<{ accountId: string; coordinate: [number, number]; accuracy: number | null } | null>(null);
  const planningRef = useRef(planning); planningRef.current = planning;
  const streetRef = useRef(onStreet); streetRef.current = onStreet;
  const nodeRef = useRef(onNode); nodeRef.current = onNode;
  const missingRef = useRef(showMissing); missingRef.current = showMissing;
  const [ready, setReady] = useState(false); const [error, setError] = useState<string | null>(null);
  const [showTracks, setShowTracks] = useState(true);
  const state = useStore(runtime.explore);
  const planner = useStore(runtime.planner);
  useEffect(() => {
    if (!host.current) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let map: MapLibre;
    try {
      try { positions.current = new BrowserViewports(window.localStorage); } catch { positions.current = null; }
      setWorkerUrl(workerUrl);
      map = new MapLibre({ container: host.current, style: import.meta.env.VITE_MAP_STYLE ?? 'https://tiles.openfreemap.org/styles/liberty',
        ...DEFAULT_POSITION, minZoom: 9, maxZoom: 19, renderWorldCopies: false, attributionControl: false });
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
      for (const id of ['streets', 'tracks', 'missing', 'selected-node', 'selected', 'highlighted-street', 'plan', 'current-location']) map.addSource(id, { type: 'geojson', data: EMPTY });
      map.addLayer({ id: 'street-outline', type: 'line', source: 'streets', paint: { 'line-color': '#fff', 'line-width': 4, 'line-opacity': 0.55 } });
      map.addLayer({ id: 'street-coverage', type: 'line', source: 'streets', paint: { 'line-color': ['case', ['==', ['get', 'complete'], true], '#21856e', ['==', ['get', 'known'], false], '#9ca89d', '#dda257'], 'line-width': ['case', ['==', ['get', 'complete'], true], 3, 1.8], 'line-opacity': ['case', ['==', ['get', 'complete'], true], 0.9, 0.55] } });
      map.addLayer({ id: 'activity-track-outline', type: 'line', source: 'tracks', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#fff', 'line-width': 8, 'line-opacity': 0.95 } });
      map.addLayer({ id: 'activity-tracks', type: 'line', source: 'tracks', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#7c3aed', 'line-width': 5, 'line-opacity': 1 } });
      map.addLayer({ id: 'missing-nodes', type: 'circle', source: 'missing', minzoom: 16, layout: { visibility: 'none' }, paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 16, 4, 19, 6], 'circle-color': '#c54832', 'circle-stroke-color': '#fff', 'circle-stroke-width': 2 } });
      map.addLayer({ id: 'selected-track-outline', type: 'line', source: 'selected', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#fff', 'line-width': 11 } });
      map.addLayer({ id: 'selected-track', type: 'line', source: 'selected', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#4c1d95', 'line-width': 7 } });
      map.addLayer({ id: 'highlighted-street-outline', type: 'line', source: 'highlighted-street', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#fff', 'line-width': 11 } });
      map.addLayer({ id: 'highlighted-street', type: 'line', source: 'highlighted-street', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#087f8c', 'line-width': 7 } });
      map.addLayer({ id: 'planned-route-outline', type: 'line', source: 'plan', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#fff', 'line-width': 10 } });
      map.addLayer({ id: 'planned-route', type: 'line', source: 'plan', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#176bb3', 'line-width': 6 } });
      map.addLayer({ id: 'current-location', type: 'circle', source: 'current-location', paint: { 'circle-radius': 8, 'circle-color': '#176bb3', 'circle-stroke-color': '#fff', 'circle-stroke-width': 3 } });
      map.addLayer({ id: 'selected-missing-node', type: 'circle', source: 'selected-node', minzoom: 16, paint: { 'circle-radius': 10, 'circle-color': '#c54832', 'circle-stroke-color': '#fff', 'circle-stroke-width': 3 } });
      map.on('click', (event) => {
        if (!enabledRef.current) return;
        const snapshot = runtime.explore.getState(); if (!['ready', 'empty'].includes(snapshot.mapStatus) || !snapshot.map) return;
        const missing = missingRef.current ? map.queryRenderedFeatures(event.point, { layers: ['selected-missing-node', 'missing-nodes'] })[0] : null;
        if (missing) {
          const node = snapshot.map.missing_nodes.find((item) => item.node_id === missing.properties?.node_id && item.dataset_id === missing.properties?.dataset_id);
          if (node) nodeRef.current(node);
          return;
        }
        if (planningRef.current) runtime.planner.addWaypoint([event.lngLat.lng, event.lngLat.lat]);
        else { const street = map.queryRenderedFeatures(event.point, { layers: ['street-coverage'] })[0]; if (street && snapshot.map.streets.some((item) => item.street_id === street.properties?.id && item.dataset_id === street.properties?.dataset_id && item.city_id === street.properties?.city_id)) streetRef.current(street); }
      });
      map.on('mouseenter', 'street-coverage', () => { map.getCanvas().style.cursor = 'pointer'; });
      map.on('mouseleave', 'street-coverage', () => { map.getCanvas().style.cursor = ''; });
      map.on('mouseenter', 'missing-nodes', () => { map.getCanvas().style.cursor = 'pointer'; });
      map.on('mouseleave', 'missing-nodes', () => { map.getCanvas().style.cursor = ''; });
      setReady(true); refresh();
    });
    map.on('movestart', () => { if (enabledRef.current) runtime.explore.invalidateViewport(); });
    map.on('moveend', () => {
      const account = accountRef.current;
      if (enabledRef.current && account && restoredAccount.current === account && restoredGeneration.current === runtime.imports.getAccountGeneration()) { const center = map.getCenter(); positions.current?.write(account, { center: [center.lng, center.lat], zoom: map.getZoom() }); }
      clearTimeout(timer); timer = setTimeout(refresh, 150);
    });
    map.on('error', () => setError('Some map tiles could not load. Check your connection.'));
    return () => { clearTimeout(timer); clearTimeout(locationTimer.current); locationRequest.current++; map.remove(); mapRef.current = null; };
  }, [runtime]);
  useEffect(() => {
    locationRequest.current++; clearTimeout(locationTimer.current); setLocating(false); setLocation(null); setLocationError(null);
    const map = mapRef.current; if (!ready || !map) return;
    restoredAccount.current = accountId;
    restoredGeneration.current = runtime.imports.getAccountGeneration();
    map.jumpTo(accountId ? positions.current?.read(accountId) ?? DEFAULT_POSITION : DEFAULT_POSITION);
  }, [accountId, ready]);
  useEffect(() => {
    const map = mapRef.current; if (!ready || !map) return;
    (map.getSource('current-location') as GeoJSONSource).setData(enabled && location?.accountId === accountId ? { type: 'FeatureCollection', features: [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: location.coordinate } }] } : EMPTY);
  }, [enabled, accountId, location, ready]);
  useEffect(() => {
    const map = mapRef.current; if (!ready || !map) return;
    const snapshot = enabled && (state.mapStatus === 'ready' || state.mapStatus === 'empty') ? state.map : null;
    const feature = (geometry: Record<string, unknown>, properties: Record<string, unknown>): Feature => ({ type: 'Feature', geometry: geometry as unknown as Geometry, properties });
    const set = (id: string, features: Feature[]) => (map.getSource(id) as GeoJSONSource).setData({ type: 'FeatureCollection', features });
    set('streets', snapshot?.streets.map((street) => feature(street.geometry, { id: street.street_id, city_id: street.city_id, dataset_id: street.dataset_id, name: street.name, complete: street.effective_completed, known: street.effective_completed !== null })) ?? []);
    set('highlighted-street', snapshot?.streets.filter((street) => street.street_id === selectedStreet?.street_id && street.dataset_id === selectedStreet.dataset_id).map((street) => feature(street.geometry, { id: street.street_id, dataset_id: street.dataset_id })) ?? []);
    set('tracks', snapshot?.tracks.map((track) => feature(track.geometry, { id: track.activity_id })) ?? []);
    set('missing', snapshot?.missing_nodes.map((node) => ({ type: 'Feature', properties: { node_id: node.node_id, dataset_id: node.dataset_id }, geometry: { type: 'Point', coordinates: [node.longitude, node.latitude] } })) ?? []);
  }, [state.map, state.mapStatus, ready, enabled, selectedStreet]);
  useEffect(() => { const map = mapRef.current; if (ready && map) (map.getSource('selected-node') as GeoJSONSource).setData(enabled && showMissing && selectedNode ? { type: 'FeatureCollection', features: [{ type: 'Feature', properties: { node_id: selectedNode.node_id, dataset_id: selectedNode.dataset_id }, geometry: { type: 'Point', coordinates: [selectedNode.longitude, selectedNode.latitude] } }] } : EMPTY); }, [selectedNode, ready, enabled, showMissing]);
  useEffect(() => {
    const map = mapRef.current; if (!ready || !map) return;
    (map.getSource('selected') as GeoJSONSource).setData(enabled && selected ? { type: 'FeatureCollection', features: selected.tracks.filter((segment) => segment.length > 1).map((segment) => ({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: segment } })) } : EMPTY);
    if (enabled && !selectedStreet && selected?.bounds) map.fitBounds(new LngLatBounds(selected.bounds), { padding: 80, maxZoom: 17, duration: 700 });
  }, [selected, selectedStreet, ready, enabled]);
  useEffect(() => {
    const map = mapRef.current; if (!ready || !map || !enabled || !selectedStreet) return;
    const [west, south, east, north] = selectedStreet.bounds;
    map.fitBounds([[west, south], [east, north]], { padding: 80, maxZoom: 18, duration: 700 });
  }, [selectedStreet, streetFocusRequest, ready, enabled]);
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
  useEffect(() => { if (ready && enabled && focus) { onMissingChange(true); mapRef.current?.flyTo({ center: focus, zoom: 18, duration: 700 }); } }, [ready, enabled, focus, onMissingChange]);
  useEffect(() => { if (ready && enabled && nodeZoomRequest > 0 && mapRef.current) mapRef.current.flyTo({ zoom: Math.max(16, mapRef.current.getZoom()), duration: 700 }); }, [ready, enabled, nodeZoomRequest]);
  const cancelLocation = () => { locationRequest.current++; clearTimeout(locationTimer.current); setLocating(false); setLocationError(null); };
  const locate = () => {
    const account = accountRef.current; if (!enabled || !account || !mapRef.current || !ready) return;
    cancelLocation(); setLocation(null);
    if (!navigator.geolocation) { setLocationError('Current location is unavailable in this browser.'); return; }
    const request = ++locationRequest.current; setLocating(true);
    const current = () => request === locationRequest.current && account === accountRef.current && enabledRef.current && Boolean(mapRef.current);
    const fail = (message: string) => { if (!current()) return; locationRequest.current++; clearTimeout(locationTimer.current); setLocating(false); setLocationError(message); };
    locationTimer.current = setTimeout(() => fail('Location timed out. Try again when GPS is available.'), 10_000);
    try { navigator.geolocation.getCurrentPosition((result) => {
      if (!current()) return;
      const { longitude, latitude, accuracy } = result.coords;
      if (!Number.isFinite(longitude) || !Number.isFinite(latitude) || Math.abs(longitude) > 180 || Math.abs(latitude) > 85) { fail('Your location is outside the supported map area.'); return; }
      clearTimeout(locationTimer.current); setLocating(false); setLocationError(null);
      setLocation({ accountId: account, coordinate: [longitude, latitude], accuracy: Number.isFinite(accuracy) && accuracy >= 0 ? accuracy : null });
      mapRef.current?.flyTo({ center: [longitude, latitude], zoom: 17, duration: 700 });
    }, (error) => fail(error.code === 1 ? 'Location permission denied. Allow location in your browser settings and try again.' : error.code === 3 ? 'Location timed out. Try again when GPS is available.' : 'Your location could not be found. Try again.'), { enableHighAccuracy: true, timeout: 10_000, maximumAge: 30_000 }); } catch { fail('Current location is unavailable in this browser.'); }
  };
  return <section className="map-area" aria-label="Exploration map" data-ready={ready} data-located={Boolean(enabled && location?.accountId === accountId)}><div ref={host} className="map-canvas" />
    <div className="map-heading"><span className="live-dot" /><span>{planning && enabled ? 'Click the map to add waypoints' : 'Your exploration map'}</span><span className="map-heading-divider" />{enabled ? planning ? 'Walking route planner' : state.coverageScope === 'filtered' ? 'Selected activities’ GPS coverage' : 'Lifetime GPS coverage' : 'A new street is a new story'}</div>
    {enabled && <div className="map-layers"><Icon name="layers" size={17} /><label><input type="checkbox" checked={showTracks} onChange={(event) => setShowTracks(event.target.checked)} />Activity tracks</label><label title="Node Hunter: select missing GPS nodes and add them to your route"><input type="checkbox" checked={showMissing} onChange={(event) => onMissingChange(event.target.checked)} />Missing nodes</label></div>}
    {enabled && <div className="map-location"><button className="secondary" aria-label="Show current location" disabled={!ready || locating} onClick={locate}><Icon name="pin" size={16} />{locating ? 'Locating…' : 'My location'}</button>{locating && <button className="text-button" onClick={cancelLocation}>Cancel location</button>}{location?.accountId === accountId && <span role="status">Your location{location.accuracy !== null && ` · ±${Math.round(location.accuracy)} m`}</span>}{locationError && <span role="alert">{locationError}</span>}</div>}
    <div className="map-legend"><span><i className="legend-line completed" />Completed</span><span><i className="legend-line remaining" />Remaining</span><span><i className="legend-line track" />Activity</span>{selectedStreet && enabled && <span><i className="legend-line highlight" />Selected street</span>}{planning && enabled && <span><i className="legend-line plan" />Planned</span>}</div>
    {enabled && <div className="map-message" role="status">{state.mapStatus === 'offline' ? 'Map coverage is unavailable offline. Reconnect to refresh this view.' : state.mapStatus === 'loading' ? 'Updating this view…' : state.mapError ?? (selectedStreet ? state.map?.streets.some((street) => street.street_id === selectedStreet.street_id && street.dataset_id === selectedStreet.dataset_id) ? `Highlighted ${selectedStreet.name}. Display geometry is limited to this view.` : `${selectedStreet.name}: display geometry is unavailable in this view. Street details remain available.` : state.map?.geography_state === 'geography_pending' ? 'Street coverage is not available in this area yet.' : showMissing && (state.viewport?.zoom ?? 0) < 16 ? 'Zoom closer to see individual missing nodes.' : 'Coverage uses your original GPS samples.')}{(state.mapStatus === 'ready' || state.mapStatus === 'empty') && state.map && (state.map.dataset_truncated || Object.entries(state.map.limits).some(([layer, limit]) => (layer !== 'missing_nodes' || showMissing && (state.viewport?.zoom ?? 0) >= 16) && limit.truncated)) && ' Some results are limited; zoom in.'}</div>}
    {error && <div className="map-error" role="alert">{error}<button aria-label="Dismiss map message" onClick={() => setError(null)}><Icon name="close" size={15} /></button></div>}
  </section>;
}
