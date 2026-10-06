import { useEffect, useState } from 'react';
import type { Runtime } from './runtime';
import { useStore } from './hooks';
import { Icon } from './icons';

export function Planner({ runtime, onFocus }: { runtime: Runtime; onFocus: (point: [number, number]) => void }) {
  const store = runtime.planner; const state = useStore(store);
  const [confirmDelete, setConfirmDelete] = useState(false);
  useEffect(() => { void store.loadRoutes(); }, [store]);
  useEffect(() => { setConfirmDelete(false); }, [state.editingRouteId]);
  const download = async () => {
    const generation = store.getAccountGeneration(); const route = state.selectedRoute;
    const content = await store.exportGpx();
    if (content === null || !route || generation !== store.getAccountGeneration() || store.getState().editingRouteId !== route.id) return;
    const url = URL.createObjectURL(new Blob([content], { type: 'application/gpx+xml' }));
    const link = document.createElement('a'); link.href = url; link.download = `${route.name.replace(/[^\p{L}\p{N}._-]+/gu, '-').slice(0, 80) || 'route'}.gpx`;
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const busy = state.saving || state.deleting;
  return <>
    <div className="panel-heading"><span className="eyebrow">TAKE A FRESH DIRECTION</span><h1>Plan your next run.</h1><p>Click the map to add waypoints. Follow the remaining streets, then preview a walking route.</p></div>
    <div className="section-title"><h2>{state.editingRouteId ? 'Edit saved route' : 'New route'}</h2><button className="text-button" onClick={() => store.newDraft()} disabled={busy}>New route<Icon name="plus" size={14} /></button></div>
    <label className="field-label">Route name<input className="planner-name" aria-label="Route name" placeholder="A new corner of the city" maxLength={100} value={state.name} onChange={(event) => store.setName(event.target.value)} /></label>
    <div className="section-title"><h2>Waypoints <span className="pill">{state.waypoints.length} / 20</span></h2><button className="text-button" disabled={!state.undoAvailable} onClick={() => store.undo()}>Undo</button></div>
    {!state.waypoints.length && <div className="planner-hint"><Icon name="pin" /><p>Add your start and destination on the map. Drag numbered markers to adjust them.</p></div>}
    <ol className="waypoint-list">{state.waypoints.map((point, index) => <li key={index}>
      <div className="waypoint-row"><button className="waypoint-number" title={`Show waypoint ${index + 1}`} onClick={() => onFocus(point)}>{index + 1}</button><span className="row-copy"><strong>{index === 0 ? 'Start' : index === state.waypoints.length - 1 ? 'Destination' : 'Via point'}</strong><span>{point[1].toFixed(5)}, {point[0].toFixed(5)}</span></span><button className="icon-button" aria-label={`Move waypoint ${index + 1} up`} disabled={index === 0} onClick={() => store.reorderWaypoint(index, index - 1)}>↑</button><button className="icon-button" aria-label={`Move waypoint ${index + 1} down`} disabled={index === state.waypoints.length - 1} onClick={() => store.reorderWaypoint(index, index + 1)}>↓</button><button className="icon-button" aria-label={`Remove waypoint ${index + 1}`} onClick={() => store.removeWaypoint(index)}><Icon name="close" size={15} /></button></div>
      <WaypointCoordinates point={point} index={index} move={(next) => store.moveWaypoint(index, next)} />
    </li>)}</ol>
    <p className="note">Waypoints are sent to FOSSGIS’s public routing service and logged there. Routes follow its foot profile; check access and closures before running.</p>
    <button className="secondary full" disabled={state.waypoints.length < 2 || state.previewStatus === 'loading' || busy} onClick={() => void store.preview()}>{state.previewStatus === 'loading' ? 'Finding a walking route…' : 'Preview walking route'}<Icon name="route" size={17} /></button>
    {state.preview && <div className="route-preview" role="status"><strong>{(state.preview.distance_m / 1000).toFixed(2)} km</strong><span>Walking route · {state.waypoints.length} waypoints</span><p><a href={state.preview.attribution.url} target="_blank" rel="noreferrer">{state.preview.attribution.text}</a> · <a href={state.preview.attribution.fix_map_url} target="_blank" rel="noreferrer">Fix the map</a></p></div>}
    <button className="primary full" disabled={!state.preview || !state.name.trim() || busy} onClick={() => void store.saveRoute()}>{state.saving ? 'Saving…' : state.editingRouteId ? 'Save changes' : 'Save route'}<Icon name="check" size={16} /></button>
    {state.editingRouteId && <><div className="planner-actions"><button className="text-button" disabled={state.exporting || busy} onClick={() => void download()}>{state.exporting ? 'Preparing GPX…' : 'Export saved GPX'}</button><button className="danger text-button" disabled={busy} onClick={() => setConfirmDelete(true)}>Delete route</button></div><p className="note">Export uses the saved route. Save your edits first.</p>{confirmDelete && <div className="alert"><p>Delete this saved route?</p><div className="planner-actions"><button className="danger text-button" disabled={busy} onClick={() => void store.deleteRoute()}>Delete permanently</button><button className="text-button" disabled={busy} onClick={() => setConfirmDelete(false)}>Keep route</button></div></div>}</>}
    {[state.draftError, state.previewError, state.saveError, state.routesError, state.selectedRouteError, state.deleteError, state.exportError].filter((value, index, errors) => value && errors.indexOf(value) === index).map((error) => <div className="alert" role="alert" key={error}>{error}</div>)}
    <div className="section-title"><h2>Saved routes</h2><button className="icon-button" aria-label="Refresh saved routes" disabled={state.routesLoading} onClick={() => void store.loadRoutes()}><Icon name="refresh" size={15} /></button></div>
    {state.selectedRouteLoading && <p className="note">Opening route…</p>}
    {state.routes.map((route) => <button className={`activity-row ${state.editingRouteId === route.id ? 'selected' : ''}`} key={route.id} disabled={busy} onClick={() => void store.openRoute(route.id)}><span className="row-icon"><Icon name="route" size={18} /></span><span className="row-copy"><strong>{route.name}</strong><span>{(route.distance_m / 1000).toFixed(2)} km · saved route</span></span><Icon name="arrow" size={15} /></button>)}
    {!state.routes.length && <p className="note">{state.routesLoading ? 'Loading saved routes…' : 'Your saved routes will appear here across devices.'}</p>}
    {state.routes.length < state.routesTotal && <button className="secondary full" disabled={state.routesLoading} onClick={() => void store.loadMoreRoutes()}>Load more routes</button>}
    <p className="note">Planning a route never changes your GPS coverage.</p>
  </>;
}

function WaypointCoordinates({ point, index, move }: { point: [number, number]; index: number; move: (point: [number, number]) => void }) {
  const [longitude, setLongitude] = useState(String(point[0])); const [latitude, setLatitude] = useState(String(point[1]));
  useEffect(() => { setLongitude(String(point[0])); setLatitude(String(point[1])); }, [point]);
  return <details className="waypoint-coordinates"><summary>Edit coordinates</summary><form onSubmit={(event) => { event.preventDefault(); move([Number(longitude), Number(latitude)]); }}><label>Longitude<input type="number" step="any" min={-180} max={180} required aria-label={`Longitude for waypoint ${index + 1}`} value={longitude} onChange={(event) => setLongitude(event.target.value)} /></label><label>Latitude<input type="number" step="any" min={-90} max={90} required aria-label={`Latitude for waypoint ${index + 1}`} value={latitude} onChange={(event) => setLatitude(event.target.value)} /></label><button className="text-button">Update waypoint {index + 1}</button></form></details>;
}
